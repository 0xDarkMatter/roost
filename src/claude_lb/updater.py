"""`claude-lb update [--check]` — self-update check (Forma §23).

v0.1 scope: assume the package was installed via
`uv tool install --editable <path>`. We don't fetch from PyPI or a release
feed yet — that's deferred until a publish pipeline exists. What we CAN do:

    1. Report the currently-installed version.
    2. If the install dir is a git working copy, compare local HEAD to the
       configured upstream remote (if reachable). Report ahead/behind counts.
    3. Emit a one-line upgrade instruction.

This is enough to satisfy the Forma compliance checklist and gives users a
concrete answer to "am I on the latest?"
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import __version__


@dataclass
class UpdateStatus:
    current_version: str
    install_dir: str | None
    is_git_repo: bool
    local_commit: str | None
    upstream_commit: str | None
    ahead: int | None
    behind: int | None
    upgrade_hint: str


def _package_install_dir() -> Path | None:
    spec = importlib.util.find_spec("claude_lb")
    if spec is None or spec.origin is None:
        return None
    # spec.origin is path to __init__.py; parent is the package; parent.parent
    # is the source root if editable-installed into `src/claude_lb`.
    pkg_dir = Path(spec.origin).parent
    src_root = pkg_dir.parent
    candidate = src_root.parent if src_root.name == "src" else src_root
    return candidate


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def _git_status(install_dir: Path) -> tuple[bool, str | None, str | None, int | None, int | None]:
    """Return (is_git, local_sha, upstream_sha, ahead, behind)."""
    if shutil.which("git") is None:
        return False, None, None, None, None
    if not (install_dir / ".git").exists():
        return False, None, None, None, None

    rc, local_sha = _run(["git", "rev-parse", "HEAD"], install_dir)
    if rc != 0:
        return False, None, None, None, None

    # Try to detect an upstream. Silent failure = no upstream configured.
    rc, upstream_ref = _run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        install_dir,
    )
    if rc != 0 or not upstream_ref:
        return True, local_sha, None, None, None

    # fetch to get up-to-date upstream (quick; offline will fail cleanly)
    _run(["git", "fetch", "--quiet"], install_dir)

    rc, upstream_sha = _run(["git", "rev-parse", upstream_ref], install_dir)
    if rc != 0:
        return True, local_sha, None, None, None

    rc, counts = _run(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream_ref}"],
        install_dir,
    )
    ahead: int | None = None
    behind: int | None = None
    if rc == 0 and counts:
        parts = counts.split()
        if len(parts) == 2:
            try:
                ahead = int(parts[0])
                behind = int(parts[1])
            except ValueError:
                pass

    return True, local_sha, upstream_sha, ahead, behind


def _build_hint(
    install_dir: Path | None,
    is_git: bool,
    behind: int | None,
) -> str:
    if install_dir is None:
        return "Reinstall with: uv tool install --upgrade --editable ."
    if not is_git:
        return (
            "Installed from a non-git source. "
            "Reinstall from the upstream repository to upgrade."
        )
    if behind is None:
        return (
            "No upstream configured or upstream unreachable. "
            f"To reinstall after `git pull`: uv tool install --editable {install_dir}"
        )
    if behind == 0:
        return "You're up-to-date."
    return (
        f"{behind} commit(s) behind upstream. "
        f"Upgrade: git -C {install_dir} pull && "
        f"uv tool install --editable {install_dir}"
    )


def check_for_update() -> UpdateStatus:
    install_dir = _package_install_dir()
    is_git = False
    local_sha: str | None = None
    upstream_sha: str | None = None
    ahead: int | None = None
    behind: int | None = None

    if install_dir is not None:
        is_git, local_sha, upstream_sha, ahead, behind = _git_status(install_dir)

    hint = _build_hint(install_dir, is_git, behind)

    return UpdateStatus(
        current_version=__version__,
        install_dir=str(install_dir) if install_dir else None,
        is_git_repo=is_git,
        local_commit=local_sha,
        upstream_commit=upstream_sha,
        ahead=ahead,
        behind=behind,
        upgrade_hint=hint,
    )


def status_to_dict(status: UpdateStatus) -> dict[str, Any]:
    return {
        "data": asdict(status),
        "meta": {
            "update_available": status.behind is not None and status.behind > 0,
        },
    }
