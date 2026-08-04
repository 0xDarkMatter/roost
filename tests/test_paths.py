"""Tests for the platform-paths helpers — env-var overrides + filename outputs."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_lb import paths as paths_mod


def test_config_dir_returns_path() -> None:
    """The default path resolution always returns a Path. Value depends on
    platform and user env, so just assert shape."""
    p = paths_mod.config_dir()
    assert isinstance(p, Path)


# ---------------------------------------------------------------------------
# legacy config migration (claude-lb -> roost rename)
# ---------------------------------------------------------------------------


def test_migrate_moves_legacy_dir_when_new_absent(tmp_path: Path) -> None:
    legacy = tmp_path / "claude-lb"
    legacy.mkdir()
    (legacy / "health.json").write_text("{}")
    (legacy / "picks.log").write_text("audit\n")
    new = tmp_path / "roost"

    moved = paths_mod._migrate_legacy_config(legacy, new)

    assert moved is True
    assert not legacy.exists()
    assert (new / "health.json").read_text() == "{}"
    assert (new / "picks.log").read_text() == "audit\n"


def test_migrate_noop_when_new_already_exists(tmp_path: Path) -> None:
    """A populated new dir means we already migrated / fresh-installed under the
    new name — never clobber it with the stale legacy copy."""
    legacy = tmp_path / "claude-lb"
    legacy.mkdir()
    (legacy / "health.json").write_text("OLD")
    new = tmp_path / "roost"
    new.mkdir()
    (new / "health.json").write_text("NEW")

    moved = paths_mod._migrate_legacy_config(legacy, new)

    assert moved is False
    assert (new / "health.json").read_text() == "NEW"
    assert legacy.exists()  # left untouched


def test_migrate_noop_when_legacy_absent(tmp_path: Path) -> None:
    moved = paths_mod._migrate_legacy_config(tmp_path / "claude-lb", tmp_path / "roost")
    assert moved is False


def test_migrate_noop_when_paths_equal(tmp_path: Path) -> None:
    same = tmp_path / "cfg"
    same.mkdir()
    assert paths_mod._migrate_legacy_config(same, same) is False


def test_config_dir_triggers_migration_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """config_dir() migrates the legacy tree on first call and is a no-op after."""
    legacy = tmp_path / "claude-lb"
    legacy.mkdir()
    (legacy / "leases.json").write_text("{}")
    new = tmp_path / "roost"

    def _fake_user_config_dir(app_name: str, **_kw) -> str:
        return str(tmp_path / app_name)

    monkeypatch.setattr(paths_mod, "user_config_dir", _fake_user_config_dir)
    monkeypatch.setattr(paths_mod, "_migration_attempted", False)

    result = paths_mod.config_dir()
    assert result == new
    assert (new / "leases.json").read_text() == "{}"
    assert not legacy.exists()


def test_cache_path_is_health_json_under_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths_mod, "config_dir", lambda: Path("/cfg"))
    assert paths_mod.cache_path() == Path("/cfg/health.json")


def test_pick_log_path_is_picks_log_under_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths_mod, "config_dir", lambda: Path("/cfg"))
    assert paths_mod.pick_log_path() == Path("/cfg/picks.log")


def test_last_pick_path_is_last_pick_json_under_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paths_mod, "config_dir", lambda: Path("/cfg"))
    assert paths_mod.last_pick_path() == Path("/cfg/last-pick.json")


# ---------------------------------------------------------------------------
# profiles_dir — env-var override path
# ---------------------------------------------------------------------------


def test_profiles_dir_default_is_home_claude_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CLAUDE_LB_PROFILES_DIR", raising=False)
    monkeypatch.setattr(paths_mod.Path, "home", staticmethod(lambda: tmp_path))
    assert paths_mod.profiles_dir() == tmp_path / ".claude-profiles"


def test_profiles_dir_env_override_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    custom = tmp_path / "custom-profiles"
    monkeypatch.setenv("CLAUDE_LB_PROFILES_DIR", str(custom))
    assert paths_mod.profiles_dir() == custom


def test_profiles_dir_expands_user_in_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An override starting with `~/` should be expanded.

    Path.expanduser() reads HOME (Unix) / USERPROFILE (Windows) directly, not
    Path.home(), so we have to set the env var that the underlying os.path
    helper consults — patching Path.home wouldn't be picked up."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDE_LB_PROFILES_DIR", "~/some-relative-profiles")
    assert paths_mod.profiles_dir() == tmp_path / "some-relative-profiles"


# ---------------------------------------------------------------------------
# single_profile_fallback — CLAUDE_CONFIG_DIR pointing at a credentials.json
# ---------------------------------------------------------------------------


def test_single_profile_fallback_returns_none_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert paths_mod.single_profile_fallback() is None


def test_single_profile_fallback_returns_dir_when_credentials_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLAUDE_CONFIG_DIR pointed at a dir with .credentials.json → return it."""
    (tmp_path / ".credentials.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert paths_mod.single_profile_fallback() == tmp_path


def test_single_profile_fallback_returns_none_when_no_credentials_in_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dir exists but contains no .credentials.json — fall through to None
    so single-profile mode doesn't accidentally engage on an empty config dir."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert paths_mod.single_profile_fallback() is None


# ---------------------------------------------------------------------------
# ensure_config_dir — creates if missing
# ---------------------------------------------------------------------------


def test_ensure_config_dir_creates_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "deep" / "nested" / "config"
    monkeypatch.setattr(paths_mod, "config_dir", lambda: target)
    result = paths_mod.ensure_config_dir()
    assert result == target
    assert target.is_dir()


def test_ensure_config_dir_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calling ensure_config_dir twice should not raise (mkdir exist_ok=True)."""
    target = tmp_path / "cfg"
    monkeypatch.setattr(paths_mod, "config_dir", lambda: target)
    paths_mod.ensure_config_dir()
    # Second call should be a clean no-op
    paths_mod.ensure_config_dir()
    assert target.is_dir()
