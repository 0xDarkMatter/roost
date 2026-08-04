"""Updater tests — mostly integration, since _git_status shells out."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_lb import updater


def test_status_returns_current_version() -> None:
    status = updater.check_for_update()
    assert status.current_version  # not empty
    assert isinstance(status.current_version, str)


def test_status_to_dict_has_meta() -> None:
    status = updater.check_for_update()
    payload = updater.status_to_dict(status)
    assert "data" in payload
    assert "meta" in payload
    assert isinstance(payload["meta"]["update_available"], bool)


def test_build_hint_no_install_dir() -> None:
    hint = updater._build_hint(None, False, None)
    assert "Reinstall" in hint


def test_build_hint_non_git(tmp_path: Path) -> None:
    hint = updater._build_hint(tmp_path, False, None)
    assert "non-git" in hint.lower()


def test_build_hint_up_to_date(tmp_path: Path) -> None:
    hint = updater._build_hint(tmp_path, True, 0)
    assert "up-to-date" in hint.lower()


def test_build_hint_behind(tmp_path: Path) -> None:
    hint = updater._build_hint(tmp_path, True, 3)
    assert "3 commit" in hint
    assert "git -C" in hint


def test_build_hint_no_upstream(tmp_path: Path) -> None:
    hint = updater._build_hint(tmp_path, True, None)
    assert "upstream" in hint.lower()


def test_run_helper_handles_missing_command(tmp_path: Path) -> None:
    rc, out = updater._run(["definitely-not-a-real-binary-xyz"], tmp_path)
    assert rc == 1
    assert out == ""


@pytest.fixture
def non_git_dir(tmp_path: Path) -> Path:
    d = tmp_path / "not-a-repo"
    d.mkdir()
    return d


def test_git_status_returns_false_for_non_git(non_git_dir: Path) -> None:
    is_git, local, upstream, ahead, behind = updater._git_status(non_git_dir)
    assert is_git is False
    assert local is None
    assert upstream is None
    assert ahead is None
    assert behind is None


def test_apply_update_uses_uv_tool_install_reinstall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_update must invoke `uv tool install --reinstall --editable <dir>`.

    We mock subprocess calls and verify the exact command line the reinstall
    path produces — this is the critical fix-the-stale-install behaviour.
    """
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], cwd: Path) -> tuple[int, str]:
        calls.append(cmd)
        if cmd[0] == "git" and cmd[1] == "pull":
            return 0, "Already up to date."
        if cmd[0] == "uv" and cmd[1:3] == ["tool", "install"]:
            return 0, "Installed"
        return 1, "unknown"

    monkeypatch.setattr(updater, "_run", _fake_run)
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/fake/bin/yes")
    # Pretend install dir is a git repo so pull is attempted.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(updater, "_package_install_dir", lambda: tmp_path)

        result = updater.apply_update(pull=True)

    assert result.applied is True
    assert result.reinstalled is True
    assert result.pulled is True
    assert result.error is None
    # Verify the critical command shape
    install_cmd = next(c for c in calls if c[0] == "uv")
    assert "--reinstall" in install_cmd
    assert "--editable" in install_cmd


def test_apply_update_no_install_dir_returns_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updater, "_package_install_dir", lambda: None)
    result = updater.apply_update()
    assert result.applied is False
    assert result.error is not None
    assert "install dir" in result.error.lower()


def test_apply_update_no_pull_flag_skips_git(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], cwd: Path) -> tuple[int, str]:
        calls.append(cmd)
        return 0, "ok"

    monkeypatch.setattr(updater, "_run", _fake_run)
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/fake/bin/yes")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(updater, "_package_install_dir", lambda: tmp_path)

        result = updater.apply_update(pull=False)

    assert result.applied is True
    assert result.pulled is False
    # git was not called
    assert not any(c[0] == "git" for c in calls)


def test_apply_update_no_uv_on_path_reports_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updater.shutil, "which", lambda exe: None if exe == "uv" else "/x")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        monkeypatch.setattr(updater, "_package_install_dir", lambda: tmp_path)
        result = updater.apply_update(pull=False)

    assert result.applied is False
    assert result.error is not None
    assert "uv" in result.error.lower()


# ---------------------------------------------------------------------------
# Windows self-lock guard
# ---------------------------------------------------------------------------


def test_would_self_lock_false_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater.sys, "platform", "linux")
    assert updater._would_self_lock() is False


def test_would_self_lock_true_when_running_from_uv_tool_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(
        updater.sys,
        "prefix",
        r"C:\Users\Mack\AppData\Roaming\uv\tools\roost",
    )
    assert updater._would_self_lock() is True


def test_would_self_lock_false_when_running_from_dev_venv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contributor running from a repo-local .venv must not be mis-detected."""
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.sys, "prefix", r"X:\Forge\claude-lb\.venv")
    assert updater._would_self_lock() is False


def test_apply_update_emits_clear_workaround_on_windows_self_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When self-lock is detected, apply_update must refuse cleanly with a
    copy-pasteable `uv tool install` command — not surface a raw EACCES from uv.
    """
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(
        updater.sys,
        "prefix",
        r"C:\Users\Mack\AppData\Roaming\uv\tools\roost",
    )
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/fake/uv")

    import tempfile

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], cwd: Path) -> tuple[int, str]:
        calls.append(cmd)
        return 0, "Already up to date."

    monkeypatch.setattr(updater, "_run", _fake_run)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        monkeypatch.setattr(updater, "_package_install_dir", lambda: tmp_path)
        result = updater.apply_update(pull=False)

    assert result.applied is False
    assert result.reinstalled is False
    assert result.error is not None
    assert "uv tool install --reinstall --editable" in result.error
    # Ensure we didn't actually attempt the uv install (that would crash).
    assert not any(c[0] == "uv" for c in calls)


# ---------------------------------------------------------------------------
# _git_status — degenerate paths
# ---------------------------------------------------------------------------


def test_git_status_returns_false_when_no_git_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Systems without a git binary should degrade gracefully — not crash."""
    monkeypatch.setattr(updater.shutil, "which", lambda exe: None if exe == "git" else "/x")
    is_git, local, upstream, ahead, behind = updater._git_status(tmp_path)
    assert (is_git, local, upstream, ahead, behind) == (False, None, None, None, None)


def test_git_status_returns_false_when_rev_parse_head_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A directory with a `.git` folder but a corrupt repo (rev-parse fails)
    should report not-a-git-repo rather than half-populated state."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(updater.shutil, "which", lambda exe: "/fake/git")

    def _fake_run(cmd: list[str], cwd: Path) -> tuple[int, str]:
        if cmd[0] == "git" and cmd[1] == "rev-parse":
            return 128, ""  # git's "fatal: not a git repository" rc
        return 0, ""

    monkeypatch.setattr(updater, "_run", _fake_run)
    is_git, local, *_ = updater._git_status(tmp_path)
    assert is_git is False
    assert local is None


# ---------------------------------------------------------------------------
# _package_install_dir — when find_spec returns None
# ---------------------------------------------------------------------------


def test_package_install_dir_returns_none_when_spec_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When importlib can't find the package (e.g. some sandboxed setups),
    _package_install_dir returns None and apply_update degrades gracefully."""
    monkeypatch.setattr(updater.importlib.util, "find_spec", lambda _name: None)
    assert updater._package_install_dir() is None


# ---------------------------------------------------------------------------
# apply_result_to_dict — JSON envelope helper
# ---------------------------------------------------------------------------


def test_apply_result_to_dict_envelope() -> None:
    result = updater.UpdateApplyResult(
        current_version="0.8.0",
        install_dir="/path",
        applied=True,
        pulled=True,
        reinstalled=True,
    )
    payload = updater.apply_result_to_dict(result)
    assert payload["data"]["applied"] is True
    assert payload["data"]["install_dir"] == "/path"
    assert payload["meta"]["applied"] is True


# ---------------------------------------------------------------------------
# _git_status — full upstream-configured path (ahead/behind populated)
# ---------------------------------------------------------------------------


def test_git_status_with_upstream_populates_ahead_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the install dir is a real git repo with an upstream configured
    AND an upstream commit reachable, _git_status should populate ahead/behind."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(updater.shutil, "which", lambda exe: "/fake/git")

    def _fake_run(cmd: list[str], cwd: Path) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return 0, "abc123local"
        if cmd[:2] == ["git", "rev-parse"] and "@{u}" in cmd:
            return 0, "origin/main"
        if cmd[:2] == ["git", "fetch"]:
            return 0, ""
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "origin/main":
            return 0, "def456upstream"
        if cmd[:2] == ["git", "rev-list"]:
            return 0, "2\t5"  # 2 ahead, 5 behind
        return 1, ""

    monkeypatch.setattr(updater, "_run", _fake_run)
    is_git, local, upstream, ahead, behind = updater._git_status(tmp_path)
    assert is_git is True
    assert local == "abc123local"
    assert upstream == "def456upstream"
    assert ahead == 2
    assert behind == 5


def test_git_status_no_upstream_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repo without an upstream branch configured should return is_git=True
    with sha but None for upstream/ahead/behind (not crash)."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(updater.shutil, "which", lambda exe: "/fake/git")

    def _fake_run(cmd: list[str], cwd: Path) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return 0, "abc123"
        if "@{u}" in cmd:
            return 128, ""  # no upstream
        return 0, ""

    monkeypatch.setattr(updater, "_run", _fake_run)
    is_git, local, upstream, ahead, behind = updater._git_status(tmp_path)
    assert is_git is True
    assert local == "abc123"
    assert (upstream, ahead, behind) == (None, None, None)


def test_git_status_upstream_rev_parse_fails_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fetch succeeds but resolving the upstream sha fails (offline-ish state).
    Don't crash — return what we know (is_git=True, local sha) and None for
    upstream-derived fields."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(updater.shutil, "which", lambda exe: "/fake/git")

    def _fake_run(cmd: list[str], cwd: Path) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return 0, "localsha"
        if "@{u}" in cmd:
            return 0, "origin/main"
        if cmd[:2] == ["git", "fetch"]:
            return 0, ""
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "origin/main":
            return 1, ""  # upstream sha lookup failed
        return 0, ""

    monkeypatch.setattr(updater, "_run", _fake_run)
    is_git, local, upstream, ahead, behind = updater._git_status(tmp_path)
    assert is_git is True
    assert local == "localsha"
    assert (upstream, ahead, behind) == (None, None, None)


# ---------------------------------------------------------------------------
# apply_update — uv install fails
# ---------------------------------------------------------------------------


def test_apply_update_uv_install_failure_reports_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If `uv tool install` exits non-zero, apply_update should report
    applied=False with the rc and stderr in the error string."""
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/fake/uv")

    def _fake_run(cmd: list[str], cwd: Path) -> tuple[int, str]:
        if cmd[0] == "uv":
            return 5, "no compatible wheel for pydantic_core"
        return 0, ""

    monkeypatch.setattr(updater, "_run", _fake_run)
    monkeypatch.setattr(updater, "_package_install_dir", lambda: tmp_path)

    result = updater.apply_update(pull=False)
    assert result.applied is False
    assert result.reinstalled is False
    assert result.error is not None
    assert "rc=5" in result.error
    assert "no compatible wheel" in result.error


def test_apply_update_git_pull_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A git pull that fails for a real reason (not 'Already up to date')
    should abort the apply with a clear error before touching uv."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/fake/yes")
    uv_called = {"n": 0}

    def _fake_run(cmd: list[str], cwd: Path) -> tuple[int, str]:
        if cmd[0] == "git":
            return 1, "fatal: refusing to merge unrelated histories"
        if cmd[0] == "uv":
            uv_called["n"] += 1
            return 0, "ok"
        return 0, ""

    monkeypatch.setattr(updater, "_run", _fake_run)
    monkeypatch.setattr(updater, "_package_install_dir", lambda: tmp_path)

    result = updater.apply_update(pull=True)
    assert result.applied is False
    assert result.pulled is False
    assert "git pull failed" in (result.error or "")
    assert uv_called["n"] == 0  # bailed before uv
