from typing import BinaryIO, Literal

WallTime = tuple[int, int, int, int, int, int]
Record = tuple[
    WallTime | None,
    str | None,
    str,
    Literal["text", "system", "deleted", "media"],
    str | None,
    Literal["image", "video", "audio", "document", "sticker", "gif", "other"] | None,
    bool,
]

class RecordIterator:
    def __init__(
        self, source: str | BinaryIO, order: Literal["dm", "md"], *, prefixes: bool = False
    ) -> None: ...
    def __iter__(self) -> RecordIterator: ...
    def __next__(self) -> list[Record]: ...

def detect_date_order(source: str | BinaryIO) -> Literal["dm", "md"]: ...
