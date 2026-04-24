"""Cache tests — atomic writes, TTL logic, mtime invalidation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_lb import cache as cache_mod
from claude_lb.models import ErrorInfo, Health, HealthCache, ProfileHealth


def _now() -> datetime:
    return datetime.now(UTC)


def _make_entry(
    name: str = "account-a",
    health: Health = Health.OK,
    expires_at: datetime | None = None,
    mtime: float = 1000.0,
) -> ProfileHealth:
    return ProfileHealth(
        name=name,
        health=health,
        probed_at=_now(),
        expires_at=expires_at,
        credentials_mtime=mtime,
    )


def test_load_missing_cache_returns_empty(tmp_path: Path) -> None:
    cache = cache_mod.load_cache(tmp_path / "missing.json")
    assert cache.profiles == {}


def test_load_invalid_json_returns_empty(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    cache = cache_mod.load_cache(broken)
    assert cache.profiles == {}


def test_round_trip_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    entry = _make_entry()
    cache = HealthCache(updated_at=_now(), profiles={"account-a": entry})
    cache_mod.save_cache(cache, path)
    loaded = cache_mod.load_cache(path)
    assert "account-a" in loaded.profiles
    assert loaded.profiles["account-a"].health is Health.OK


def test_save_is_atomic_no_temp_leftover(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    cache = HealthCache(updated_at=_now(), profiles={})
    cache_mod.save_cache(cache, path)
    # No stray .tmp files left behind
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_upsert_profile(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    cache_mod.upsert_profile("account-a", _make_entry("account-a"), path)
    cache_mod.upsert_profile("account-b", _make_entry("account-b"), path)
    loaded = cache_mod.load_cache(path)
    assert set(loaded.profiles.keys()) == {"account-a", "account-b"}


def test_remove_profile(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    cache_mod.upsert_profile("account-a", _make_entry("account-a"), path)
    assert cache_mod.remove_profile("account-a", path) is True
    assert cache_mod.remove_profile("account-a", path) is False
    assert cache_mod.load_cache(path).profiles == {}


def test_is_entry_fresh_within_ttl() -> None:
    now = _now()
    entry = _make_entry(expires_at=now + timedelta(minutes=1))
    assert cache_mod.is_entry_fresh(entry, credentials_mtime=1000.0, now=now) is True


def test_is_entry_stale_past_ttl() -> None:
    now = _now()
    entry = _make_entry(expires_at=now - timedelta(seconds=1))
    assert cache_mod.is_entry_fresh(entry, credentials_mtime=1000.0, now=now) is False


def test_auth_dead_never_expires() -> None:
    now = _now()
    entry = ProfileHealth(
        name="x",
        health=Health.AUTH_DEAD,
        probed_at=now - timedelta(days=7),
        expires_at=None,
        credentials_mtime=1000.0,
        error=ErrorInfo(type="authentication_error", message="x"),
    )
    assert cache_mod.is_entry_fresh(entry, credentials_mtime=1000.0, now=now) is True


def test_mtime_change_invalidates_auth_dead() -> None:
    """Critical: if the operator re-runs `claude login --profile X`, the
    credentials file's mtime bumps and the auth_dead entry must re-probe."""
    now = _now()
    entry = ProfileHealth(
        name="x",
        health=Health.AUTH_DEAD,
        probed_at=now,
        expires_at=None,
        credentials_mtime=1000.0,
    )
    assert cache_mod.is_entry_fresh(entry, credentials_mtime=2000.0, now=now) is False


def test_expires_at_with_naive_datetime_still_works() -> None:
    """A naive datetime in cache is coerced to UTC for comparison."""
    now = _now()
    entry = _make_entry(
        expires_at=(now + timedelta(minutes=1)).replace(tzinfo=None),
    )
    assert cache_mod.is_entry_fresh(entry, credentials_mtime=1000.0, now=now) is True
