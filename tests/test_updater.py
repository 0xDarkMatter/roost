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
