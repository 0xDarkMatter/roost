"""Cache tests — atomic writes, TTL logic, mtime invalidation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# is_entry_fresh additional branches — AUTH_DEAD always fresh, naive expires
# ---------------------------------------------------------------------------


def test_is_entry_fresh_auth_dead_always_fresh() -> None:
    """AUTH_DEAD entries skip the TTL check — they're not gonna heal until
    `claude login` rewrites credentials (which bumps mtime, invalidating)."""
    now = _now()
    entry = ProfileHealth(
        name="dead",
        health=Health.AUTH_DEAD,
        probed_at=now - timedelta(days=30),
        expires_at=None,
        error=ErrorInfo(type="auth_error", message="dead"),
        credentials_mtime=1000.0,
    )
    assert cache_mod.is_entry_fresh(entry, credentials_mtime=1000.0, now=now) is True


def test_is_entry_fresh_returns_false_when_expires_at_missing() -> None:
    """A non-AUTH_DEAD entry without expires_at should be considered stale —
    safer to re-probe than to use a record we can't bound."""
    now = _now()
    entry = ProfileHealth(
        name="x",
        health=Health.OK,
        probed_at=now,
        expires_at=None,
        credentials_mtime=1000.0,
    )
    assert cache_mod.is_entry_fresh(entry, credentials_mtime=1000.0, now=now) is False


# ---------------------------------------------------------------------------
# Atomic write — failure cleans up tempfile
# ---------------------------------------------------------------------------


def test_atomic_write_unlinks_tempfile_on_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace fails inside save_cache, the tempfile must be cleaned up."""
    import os as _os

    target = tmp_path / "health.json"
    monkeypatch.setattr(cache_mod, "cache_path", lambda: target)
    monkeypatch.setattr(cache_mod, "ensure_config_dir", lambda: tmp_path)

    real_replace = _os.replace

    def boom(*a, **kw):
        raise OSError("simulated")

    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError):
        cache_mod.save_cache(HealthCache(updated_at=_now()))

    monkeypatch.setattr(_os, "replace", real_replace)
    leftover = list(tmp_path.glob(".health-*.json.tmp"))
    assert leftover == []


# ---------------------------------------------------------------------------
# Taxonomy — _parse_retry_after + _parse_iso degenerate branches
# ---------------------------------------------------------------------------


def test_parse_retry_after_handles_none() -> None:
    from claude_lb.taxonomy import _parse_retry_after

    assert _parse_retry_after(None) is None


def test_parse_retry_after_handles_garbage_string() -> None:
    from claude_lb.taxonomy import _parse_retry_after

    assert _parse_retry_after("not-a-number") is None


def test_parse_retry_after_rejects_negative_value() -> None:
    """Negative Retry-After is nonsensical — treat as missing rather than zero."""
    from claude_lb.taxonomy import _parse_retry_after

    assert _parse_retry_after("-5") is None


def test_parse_retry_after_strips_whitespace_and_parses_int() -> None:
    from claude_lb.taxonomy import _parse_retry_after

    assert _parse_retry_after("  30  ") == 30


def test_taxonomy_parse_iso_handles_z_and_naive() -> None:
    """The taxonomy module has its own _parse_iso — verify the same shape
    handling as pick._parse_iso (Z suffix, naive→UTC, garbage→None)."""
    from claude_lb.taxonomy import _parse_iso

    assert _parse_iso(None) is None
    assert _parse_iso("") is None
    assert _parse_iso(12345) is None
    assert _parse_iso("not-a-ts") is None
    parsed = _parse_iso("2026-04-25T10:30:00Z")
    assert parsed is not None and parsed.tzinfo is not None
    naive = _parse_iso("2026-04-25T10:30:00")
    assert naive is not None and naive.tzinfo is not None


def test_taxonomy_window_returns_none_for_non_dict_body() -> None:
    from claude_lb.taxonomy import _window

    assert _window(None, "five_hour") is None
    assert _window({"five_hour": "not-a-dict"}, "five_hour") is None
    assert _window({}, "missing_key") is None
    assert _window({"five_hour": {"utilization": 50}}, "five_hour") == {"utilization": 50}


# ---------------------------------------------------------------------------
# Doctor — config_dir mkdir failure / writability test failure
# ---------------------------------------------------------------------------


def test_doctor_config_dir_writable_fails_when_mkdir_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If `Path.mkdir` raises (e.g. read-only filesystem), the check should
    return passed=False with a helpful detail — not propagate the OSError."""
    from claude_lb import doctor as doctor_mod

    target = tmp_path / "deep" / "cfg"

    def boom_mkdir(self, *a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(doctor_mod, "config_dir", lambda: target)
    monkeypatch.setattr(type(target), "mkdir", boom_mkdir)
    result = doctor_mod._check_config_dir_writable()
    assert result.passed is False
    assert "Cannot create" in result.detail
    assert "read-only" in result.detail


def test_doctor_config_dir_writable_fails_when_tempfile_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A directory that exists but rejects writes (Windows ACL pattern) should
    fail the writability test even though mkdir(exist_ok=True) succeeded."""
    import tempfile as _tempfile

    from claude_lb import doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "config_dir", lambda: tmp_path)

    def boom_mkstemp(*a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(_tempfile, "mkstemp", boom_mkstemp)
    result = doctor_mod._check_config_dir_writable()
    assert result.passed is False
    assert "not writable" in result.detail


# ---------------------------------------------------------------------------
# Doctor — cache_readable detects unreadable file
# ---------------------------------------------------------------------------


def test_taxonomy_compute_expires_at_returns_none_for_state_without_ttl() -> None:
    """AUTH_EXPIRED is intentionally absent from DEFAULT_TTL_SECONDS — its
    cache entry has no expiry because mtime-bump on refresh is the
    invalidation signal, not a TTL. Hits the `ttl is None: return None`
    branch in compute_expires_at."""
    from datetime import UTC, datetime

    from claude_lb.models import Health
    from claude_lb.taxonomy import ClassificationResult, compute_expires_at

    result = ClassificationResult(health=Health.AUTH_EXPIRED)
    assert compute_expires_at(result, datetime.now(UTC)) is None


def test_doctor_cache_readable_handles_oserror_on_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the cache file exists but the open() raises (permission, locked,
    etc.), the check should return passed=False, not crash."""
    from claude_lb import doctor as doctor_mod

    cache_file = tmp_path / "health.json"
    cache_file.write_text("{}")

    monkeypatch.setattr(doctor_mod, "cache_path", lambda: cache_file)

    real_open = Path.open

    def boom_open(self, *a, **kw):
        if self == cache_file:
            raise OSError("locked")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", boom_open)
    result = doctor_mod._check_cache_readable()
    assert result.passed is False
    assert "cannot be read" in result.detail.lower()


# ---------------------------------------------------------------------------
# Phase D — exponential network_error backoff
# ---------------------------------------------------------------------------


def test_network_backoff_seconds_progression() -> None:
    """30s -> 60s -> 120s -> 240s -> 480s, then capped at 480s."""
    from claude_lb.cache import network_backoff_seconds

    assert network_backoff_seconds(0) == 30
    assert network_backoff_seconds(1) == 60
    assert network_backoff_seconds(2) == 120
    assert network_backoff_seconds(3) == 240
    assert network_backoff_seconds(4) == 480
    assert network_backoff_seconds(5) == 480
    assert network_backoff_seconds(50) == 480


def test_network_backoff_negative_clamped_to_zero() -> None:
    from claude_lb.cache import network_backoff_seconds

    assert network_backoff_seconds(-1) == 30
    assert network_backoff_seconds(-100) == 30


def test_is_entry_fresh_network_error_with_zero_failures_uses_static_ttl() -> None:
    """consecutive_failures=0 keeps the original behaviour: expires_at drives
    freshness, no backoff override."""
    from datetime import UTC, datetime, timedelta

    from claude_lb.cache import is_entry_fresh
    from claude_lb.models import Health, ProfileHealth

    now = datetime.now(UTC)
    entry = ProfileHealth(
        name="x",
        health=Health.NETWORK_ERROR,
        probed_at=now,
        expires_at=now + timedelta(seconds=20),
        consecutive_failures=0,
    )
    assert is_entry_fresh(entry, now=now) is True
    # Past expires_at -> stale
    assert is_entry_fresh(entry, now=now + timedelta(seconds=25)) is False


def test_is_entry_fresh_network_error_backoff_extends_ttl() -> None:
    """consecutive_failures > 0 extends TTL beyond the static expires_at."""
    from datetime import UTC, datetime, timedelta

    from claude_lb.cache import is_entry_fresh
    from claude_lb.models import Health, ProfileHealth

    now = datetime.now(UTC)
    # 1 prior failure -> 60s backoff
    entry = ProfileHealth(
        name="x",
        health=Health.NETWORK_ERROR,
        probed_at=now,
        expires_at=now + timedelta(seconds=10),  # ignored when backoff active
        consecutive_failures=1,
    )
    # 30s in: backoff says 60s, so still fresh.
    assert is_entry_fresh(entry, now=now + timedelta(seconds=30)) is True
    # 70s in: past 60s backoff -> stale.
    assert is_entry_fresh(entry, now=now + timedelta(seconds=70)) is False


def test_is_entry_fresh_network_error_backoff_caps_at_480s() -> None:
    from datetime import UTC, datetime, timedelta

    from claude_lb.cache import is_entry_fresh
    from claude_lb.models import Health, ProfileHealth

    now = datetime.now(UTC)
    entry = ProfileHealth(
        name="x",
        health=Health.NETWORK_ERROR,
        probed_at=now,
        consecutive_failures=10,
    )
    # Just under 480s -> still fresh.
    assert is_entry_fresh(entry, now=now + timedelta(seconds=479)) is True
    # Just over 480s -> stale.
    assert is_entry_fresh(entry, now=now + timedelta(seconds=481)) is False


def test_is_entry_fresh_non_network_state_unaffected_by_failures_field() -> None:
    """consecutive_failures should only affect NETWORK_ERROR entries."""
    from datetime import UTC, datetime, timedelta

    from claude_lb.cache import is_entry_fresh
    from claude_lb.models import Health, ProfileHealth

    now = datetime.now(UTC)
    entry = ProfileHealth(
        name="x",
        health=Health.OK,
        probed_at=now,
        expires_at=now + timedelta(seconds=10),
        consecutive_failures=99,  # ignored for non-NETWORK_ERROR
    )
    assert is_entry_fresh(entry, now=now + timedelta(seconds=5)) is True
    assert is_entry_fresh(entry, now=now + timedelta(seconds=20)) is False


def test_consecutive_failures_default_zero_for_backwards_compat() -> None:
    """Old health.json files written by v0.3.0 lack consecutive_failures.
    Pydantic should default the missing field to 0 without raising."""
    from datetime import UTC, datetime

    from claude_lb.models import ProfileHealth

    raw = {
        "name": "x",
        "health": "ok",
        "probed_at": datetime.now(UTC).isoformat(),
    }
    entry = ProfileHealth.model_validate(raw)
    assert entry.consecutive_failures == 0
