import time
from collections.abc import Callable, Iterator

from sherd_core import Message, Store, UpsertStats

N = 100_000
BATCH = 5000


def test_upsert_streams_100k_messages(store: Store, make_message: Callable[..., Message]) -> None:
    checked: list[int] = []

    def messages() -> Iterator[Message]:
        for i in range(N):
            if i % BATCH == 1:
                # Everything before the current batch must already be written: the store holds
                # at most one batch per table, it never collects the whole iterable.
                written = store.query("SELECT count(*) AS n FROM messages").column("n")[0]
                assert written.as_py() == i - 1
                checked.append(i)
            yield make_message(i)

    import_id = store.begin_import("whatsapp", "1", "h", "UTC")
    start = time.perf_counter()
    first = store.upsert(import_id, "whatsapp", messages(), batch_size=BATCH)
    first_seconds = time.perf_counter() - start
    assert first == UpsertStats(N, N)
    assert len(checked) == N // BATCH

    checked.clear()
    import_id = store.begin_import("whatsapp", "1", "h", "UTC")
    start = time.perf_counter()
    again = store.upsert(
        import_id, "whatsapp", (make_message(i) for i in range(N)), batch_size=BATCH
    )
    again_seconds = time.perf_counter() - start
    assert again == UpsertStats(N, 0)
    assert store.table_counts()["messages"] == N

    assert first_seconds < 5, f"first upsert took {first_seconds:.2f}s"
    assert again_seconds < 5, f"re-upsert took {again_seconds:.2f}s"
