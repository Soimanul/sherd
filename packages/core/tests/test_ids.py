import hashlib
from pathlib import Path

import pytest
from sherd_core.ids import content_hash, path_hash, row_id


def test_row_id_follows_contract() -> None:
    expected = hashlib.sha256(b"whatsapp\x1fabc").hexdigest()[:32]
    assert row_id("whatsapp", "abc") == expected
    assert len(expected) == 32
    assert row_id("whatsapp", "abc") != row_id("spotify", "abc")


def test_content_hash_stringifies_fields_and_maps_none_to_empty() -> None:
    expected = hashlib.sha256(b"a\x1f\x1f3\x1fTrue").hexdigest()[:32]
    assert content_hash("a", None, 3, True) == expected
    assert content_hash("a", "") == content_hash("a", None)
    assert content_hash("ab", "c") != content_hash("a", "bc")


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_path_hash_of_directory(tmp_path: Path) -> None:
    write(tmp_path / "b.txt", b"12")
    write(tmp_path / "sub" / "a.json", b"123")
    listing = "b.txt\x1f2\x1esub/a.json\x1f3\x1e"
    assert path_hash(tmp_path) == hashlib.sha256(listing.encode()).hexdigest()


def test_path_hash_ignores_content_and_location_but_not_size_or_names(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    write(one / "chat.txt", b"aaaa")
    write(two / "chat.txt", b"bbbb")
    assert path_hash(one) == path_hash(two)
    write(two / "chat.txt", b"bbbbb")
    assert path_hash(one) != path_hash(two)
    (two / "chat.txt").rename(two / "other.txt")
    write(two / "other.txt", b"bbbb")
    assert path_hash(one) != path_hash(two)


def test_path_hash_of_single_file(tmp_path: Path) -> None:
    write(tmp_path / "x" / "export.zip", b"zip")
    expected = hashlib.sha256(b"export.zip\x1f3\x1e").hexdigest()
    assert path_hash(tmp_path / "x" / "export.zip") == expected


def test_path_hash_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        path_hash(tmp_path / "missing")
