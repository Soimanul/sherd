from pathlib import Path

import pytest

from scripts import pii_scan


def email() -> str:
    return "person@" + "private.org"


def phone() -> str:
    return "+44 " + "7700 900123"


def iban() -> str:
    return "GB82" + "WEST12345698765432"


@pytest.mark.parametrize(
    ("value", "kind"),
    [(email(), "email"), (phone(), "phone"), ("07" + "12 345 678", "phone"), (iban(), "iban")],
)
def test_positive_rules(value: str, kind: str) -> None:
    assert list(pii_scan.findings(value)) == [(kind, value)]


@pytest.mark.parametrize(
    "value",
    [
        "a@example.com",
        "a@sub.example.org",
        "a@example.net",
        "a@demo.test",
        "a@demo.invalid",
        "a@demo.example",
        "a@demo.localhost",
        "+1 555 0100",
        "+1 (555) 0199",
        "+5550101",
        "5550101",
        "XX82" + "WEST12345698765432",
        "GB00" + "WEST12345698765432",
        "+12",
        "+1234567890123456",
    ],
)
def test_negative_rules(value: str) -> None:
    assert not list(pii_scan.findings(value))


@pytest.mark.parametrize("domain", ["example.com.evil.org", "fakeexample.com"])
def test_domain_must_be_reserved(domain: str) -> None:
    assert list(pii_scan.findings("a@" + domain))


@pytest.mark.parametrize("separator", ["", " ", ".", "-"])
def test_romanian_separators(separator: str) -> None:
    value = separator.join(["07" + "12", "345", "678"])
    assert list(pii_scan.findings(value)) == [("phone", value)]


def test_grouped_iban() -> None:
    value = iban()
    grouped = " ".join(value[index : index + 4] for index in range(0, len(value), 4))
    assert list(pii_scan.findings(grouped)) == [("iban", grouped)]


def test_masking_and_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("safe\n" + email() + "\n")
    reports = list(pii_scan.scan_file(path, set()))
    assert reports == [f"{path}:2: email pe***"]
    assert email() not in "".join(reports)


def test_exact_allowlist(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text(email())
    allowlist = tmp_path / ".pii-allowlist"
    allowlist.write_text("# header\n" + email() + "\n")
    assert not list(pii_scan.scan_file(path, pii_scan.load_allowlist(allowlist)))
    assert list(pii_scan.scan_file(path, {"person"}))
    assert pii_scan.load_allowlist(tmp_path / "missing") == set()


def test_binary_skip(tmp_path: Path) -> None:
    path = tmp_path / "binary"
    path.write_bytes(b"\0" + email().encode())
    assert not list(pii_scan.scan_file(path, set()))


def test_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pii_scan, "ROOT", tmp_path)
    path = tmp_path / "sample.txt"
    path.write_text("safe")
    assert pii_scan.main([str(path)]) == 0
    path.write_text(email())
    assert pii_scan.main([str(path)]) == 1
    assert capsys.readouterr().out == f"{path}:1: email pe***\n"


def test_all_tracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "tracked sample.txt"
    path.write_text(email())
    subprocess.run(["git", "-C", str(tmp_path), "add", path.name], check=True)
    (tmp_path / "untracked.txt").write_text(phone())
    monkeypatch.setattr(pii_scan, "ROOT", tmp_path)
    assert pii_scan.main(["--all"]) == 1
    assert capsys.readouterr().out == f"{path}:1: email pe***\n"


@pytest.mark.parametrize("value", [phone(), iban()])
def test_other_values_are_masked(tmp_path: Path, value: str) -> None:
    path = tmp_path / "sample.txt"
    path.write_text(value)
    reports = list(pii_scan.scan_file(path, set()))
    assert len(reports) == 1
    assert reports[0].endswith(value[:2] + "***")
    assert value not in reports[0]


def test_parenthesized_phone_exact_allowlist(tmp_path: Path) -> None:
    value = "+44 (" + "7700) (900123)"
    assert list(pii_scan.findings(value)) == [("phone", value)]
    path = tmp_path / "sample.txt"
    path.write_text(value)
    assert not list(pii_scan.scan_file(path, {value}))


def test_reserved_phone_range() -> None:
    for index in range(100):
        assert not list(pii_scan.findings(f"+1 555 01{index:02d}"))
    assert list(pii_scan.findings("+1 555 " + "0200"))


@pytest.mark.parametrize("digits", [8, 15])
def test_international_length_boundaries(digits: int) -> None:
    value = "+" + "4" * digits
    assert list(pii_scan.findings(value)) == [("phone", value)]
