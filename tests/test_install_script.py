"""Offline checks for the POSIX install script."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install.sh"


def run_script(
    tmp_path: Path, *, os_name: str = "Darwin", uv_present: bool = True, **settings: str
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uname = bin_dir / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '%s\\n' '{os_name}'\n")
    uname.chmod(0o755)
    uv = bin_dir / "uv"
    if uv_present:
        uv.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SHERD_TEST_LOG"\n')
        uv.chmod(0o755)
    env = (
        os.environ
        | {
            "PATH": f"{bin_dir}:{os.environ['PATH'] if uv_present else '/usr/bin:/bin'}",
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
    assert "sherd demo && sherd web --demo" in result.stdout
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


def test_missing_uv_download_failure_is_clear_and_offline(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" > "$SHERD_TEST_LOG"\nexit 22\n')
    curl.chmod(0o755)
    result = run_script(tmp_path, uv_present=False)
    assert result.returncode == 1
    assert "Failed to download the uv installer" in result.stderr
    assert (tmp_path / "uv.log").read_text().startswith("-fLsS https://astral.sh/uv/install.sh -o ")


def test_missing_uv_installer_runs_and_sets_path_hint(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(r"""#!/bin/sh
while [ "$1" != "-o" ]; do shift; done
cat > "$2" <<'INSTALLER'
#!/bin/sh
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/uv" <<'UV'
#!/bin/sh
printf '%s\n' "$*" >> "$SHERD_TEST_LOG"
UV
chmod +x "$HOME/.local/bin/uv"
INSTALLER
""")
    curl.chmod(0o755)
    result = run_script(tmp_path, uv_present=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "uv.log").read_text().strip() == "tool install sherd-cli"
    assert "uv tool update-shell" in result.stdout
    assert "sherd demo && sherd web --demo" in result.stdout
