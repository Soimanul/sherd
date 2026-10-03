"""Offline checks for the POSIX install script."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install.sh"


def run_script(
    tmp_path: Path, *, os_name: str = "Darwin", **settings: str
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uname = bin_dir / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '%s\\n' '{os_name}'\n")
    uname.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SHERD_TEST_LOG"\n')
    uv.chmod(0o755)
    env = (
        os.environ
        | {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "SHERD_TEST_LOG": str(tmp_path / "uv.log"),
        }
        | settings
    )
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def test_dry_run_prints_commands_without_running_uv(tmp_path: Path) -> None:
    result = run_script(tmp_path, SHERD_DRY_RUN="1")
    assert result.returncode == 0
    assert "uv tool install sherd-cli" in result.stdout
    assert "sherd demo && sherd web" in result.stdout
    assert not (tmp_path / "uv.log").exists()


def test_unsupported_os_fails_clearly(tmp_path: Path) -> None:
    result = run_script(tmp_path, os_name="Windows_NT")
    assert result.returncode == 1
    assert "macOS and Linux only" in result.stderr
    assert not (tmp_path / "uv.log").exists()


def test_version_find_links_and_existing_uv(tmp_path: Path) -> None:
    result = run_script(tmp_path, SHERD_VERSION="0.1.0", SHERD_FIND_LINKS="/tmp/local wheels")
    assert result.returncode == 0
    assert "installing it" not in result.stdout
    assert (tmp_path / "uv.log").read_text().strip() == (
        "tool install sherd-cli==0.1.0 --find-links /tmp/local wheels"
    )
