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
