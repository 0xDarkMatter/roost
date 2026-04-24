"""Pick tests — strategies, stickiness, filter ladder, failure modes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_lb import pick as pick_mod
from claude_lb.models import ErrorInfo, Health, HealthCache, ProfileHealth, Usage
from claude_lb.pick import (
    PickFailureReason,
    Strategy,
    append_pick_log,
    pick,
    read_last_pick,
    write_last_pick,
)


def _write_last_pick_at(name: str, when: datetime) -> None:
    """Write last-pick with an explicit timestamp (bypasses wall-clock)."""
    import json

    # Use the patched attribute from pick_mod so monkeypatching in the
    # isolate fixture takes effect. Importing last_pick_path directly at
    # module-top binds the name pre-patch.
    path = pick_mod.last_pick_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "profile": name,
        "timestamp": when.isoformat().replace("+00:00", "Z"),
    }))


FIXED_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _entry(
    name: str,
    health: Health,
    *,
    weekly_pct: int | None = None,
    session_pct: int | None = None,
    expires_at: datetime | None = None,
    weekly_reset_at: datetime | None = None,
    session_reset_at: datetime | None = None,
    probed_at: datetime | None = None,
) -> ProfileHealth:
    usage = None
    if weekly_pct is not None or session_pct is not None:
        usage = Usage(weekly_pct=weekly_pct, session_pct=session_pct)
    error = None
    if health is Health.AUTH_DEAD:
        error = ErrorInfo(type="authentication_error", message="x")
    return ProfileHealth(
        name=name,
        health=health,
        probed_at=probed_at or FIXED_NOW,
        expires_at=expires_at,
        error=error,
        usage=usage,
        weekly_reset_at=weekly_reset_at,
        session_reset_at=session_reset_at,
    )


def _cache(*entries: ProfileHealth) -> HealthCache:
    return HealthCache(updated_at=FIXED_NOW, profiles={e.name: e for e in entries})


# ---------------------------------------------------------------------------
# Path isolation: point stickiness / pick log at tmp.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_pick_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    last_pick_fp = config / "last-pick.json"
    pick_log_fp = config / "picks.log"

    def _last_pick() -> Path:
        return last_pick_fp

    def _pick_log() -> Path:
        return pick_log_fp

    monkeypatch.setattr(pick_mod, "last_pick_path", _last_pick)
    monkeypatch.setattr(pick_mod, "pick_log_path", _pick_log)
    # Also stamp the default stickiness env so tests don't inherit an override.
    monkeypatch.delenv("CLAUDE_LB_STICKINESS", raising=False)
    return config


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_pick_returns_only_ok_profile() -> None:
    cache = _cache(
        _entry("account-a", Health.OK, weekly_pct=10),
        _entry("account-c", Health.AUTH_DEAD),
    )
    outcome = pick(cache, ["account-a", "account-c"], now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "account-a"


def test_least_used_prefers_lowest_weekly() -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=50),
        _entry("b", Health.OK, weekly_pct=10),
        _entry("c", Health.OK, weekly_pct=30),
    )
    outcome = pick(cache, ["a", "b", "c"], strategy=Strategy.LEAST_USED, now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


def test_first_healthy_honours_discovery_order() -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=50),
        _entry("b", Health.OK, weekly_pct=10),
    )
    outcome = pick(cache, ["a", "b"], strategy=Strategy.FIRST_HEALTHY, now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "a"


def test_weighted_considers_session_usage() -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10, session_pct=90),
        _entry("b", Health.OK, weekly_pct=20, session_pct=5),
    )
    outcome = pick(cache, ["a", "b"], strategy=Strategy.WEIGHTED, now=FIXED_NOW)
    # a: 10 / (90+1) = 0.11, b: 20 / (5+1) = 3.33 -> a wins.
    assert outcome.chosen is not None
    assert outcome.chosen.name == "a"


def test_round_robin_pushes_last_pick_to_back(_isolate_pick_paths: Path) -> None:
    write_last_pick("a")
    cache = _cache(
        _entry("a", Health.OK),
        _entry("b", Health.OK),
    )
    outcome = pick(cache, ["a", "b"], strategy=Strategy.ROUND_ROBIN, now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


# ---------------------------------------------------------------------------
# Stickiness
# ---------------------------------------------------------------------------


def test_stickiness_returns_same_profile_within_window(
    _isolate_pick_paths: Path,
) -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=50),
        _entry("b", Health.OK, weekly_pct=10),
    )
    # Write the last-pick with a timestamp just before FIXED_NOW so the
    # stickiness window contains it. Otherwise pick() compares to wall-clock
    # and the FIXED_NOW-in-the-past scenario breaks the delta check.
    _write_last_pick_at("a", FIXED_NOW - timedelta(seconds=30))
    outcome = pick(cache, ["a", "b"], strategy=Strategy.STICKY, now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "a"
    assert outcome.strategy_used is Strategy.STICKY


def test_stickiness_falls_through_if_stale(_isolate_pick_paths: Path) -> None:
    # Write a last-pick that's older than the stickiness window by manipulating mtime
    # indirectly: use stickiness=0 to disable, which forces least-used fallback.
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=50),
        _entry("b", Health.OK, weekly_pct=10),
    )
    write_last_pick("a")
    outcome = pick(
        cache,
        ["a", "b"],
        strategy=Strategy.STICKY,
        stickiness_s=0,
        now=FIXED_NOW,
    )
    # stickiness=0 disables the sticky check; we fall through to least-used.
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


def test_stickiness_skips_dead_profile(_isolate_pick_paths: Path) -> None:
    write_last_pick("a")
    cache = _cache(
        _entry("a", Health.AUTH_DEAD),
        _entry("b", Health.OK),
    )
    outcome = pick(cache, ["a", "b"], strategy=Strategy.STICKY, now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


# ---------------------------------------------------------------------------
# Filter ladder edge cases
# ---------------------------------------------------------------------------


def test_weekly_limit_with_future_reset_is_filtered_out() -> None:
    cache = _cache(
        _entry(
            "a",
            Health.WEEKLY_LIMIT,
            weekly_reset_at=FIXED_NOW + timedelta(hours=24),
        ),
        _entry("b", Health.OK),
    )
    outcome = pick(cache, ["a", "b"], now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


def test_weekly_limit_with_past_reset_is_selectable() -> None:
    cache = _cache(
        _entry(
            "a",
            Health.WEEKLY_LIMIT,
            weekly_reset_at=FIXED_NOW - timedelta(minutes=1),
        ),
    )
    # Only candidate is 'a' with expired weekly reset — it passes the ladder.
    outcome = pick(cache, ["a"], strategy=Strategy.LEAST_USED, now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "a"


def test_rate_limited_filter_honours_expires_at() -> None:
    cache = _cache(
        _entry(
            "a",
            Health.RATE_LIMITED,
            expires_at=FIXED_NOW + timedelta(seconds=30),
        ),
        _entry("b", Health.OK),
    )
    outcome = pick(cache, ["a", "b"], now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


def test_require_ok_fails_when_none_ok() -> None:
    cache = _cache(
        _entry("a", Health.RATE_LIMITED, expires_at=FIXED_NOW + timedelta(seconds=1)),
    )
    outcome = pick(cache, ["a"], require_ok=True, now=FIXED_NOW)
    assert outcome.chosen is None
    assert outcome.reason is PickFailureReason.REQUIRE_OK_NONE


def test_all_auth_dead_signals_auth_required() -> None:
    cache = _cache(_entry("a", Health.AUTH_DEAD), _entry("b", Health.AUTH_DEAD))
    outcome = pick(cache, ["a", "b"], now=FIXED_NOW)
    assert outcome.chosen is None
    assert outcome.reason is PickFailureReason.ALL_AUTH_DEAD


def test_all_weekly_signals_earliest_recovery() -> None:
    reset_a = FIXED_NOW + timedelta(hours=24)
    reset_b = FIXED_NOW + timedelta(hours=12)
    cache = _cache(
        _entry("a", Health.WEEKLY_LIMIT, weekly_reset_at=reset_a),
        _entry("b", Health.WEEKLY_LIMIT, weekly_reset_at=reset_b),
    )
    outcome = pick(cache, ["a", "b"], now=FIXED_NOW)
    assert outcome.chosen is None
    assert outcome.reason is PickFailureReason.ALL_WEEKLY
    assert outcome.earliest_recovery_at == reset_b


def test_all_throttled_signals_rate_limited() -> None:
    cache = _cache(
        _entry(
            "a",
            Health.RATE_LIMITED,
            expires_at=FIXED_NOW + timedelta(seconds=30),
        ),
        _entry(
            "b",
            Health.SESSION_LIMIT,
            session_reset_at=FIXED_NOW + timedelta(hours=1),
        ),
    )
    outcome = pick(cache, ["a", "b"], now=FIXED_NOW)
    assert outcome.chosen is None
    assert outcome.reason is PickFailureReason.ALL_THROTTLED


def test_no_profiles() -> None:
    outcome = pick(HealthCache(updated_at=FIXED_NOW), [], now=FIXED_NOW)
    assert outcome.reason is PickFailureReason.NO_PROFILES


# ---------------------------------------------------------------------------
# Pick log
# ---------------------------------------------------------------------------


def test_append_pick_log_writes_tsv(_isolate_pick_paths: Path) -> None:
    append_pick_log("account-a", Strategy.LEAST_USED, 0.91)
    log_path = _isolate_pick_paths / "picks.log"
    content = log_path.read_text()
    lines = content.strip().splitlines()
    assert len(lines) == 1
    parts = lines[0].split("\t")
    assert parts[1] == "account-a"
    assert parts[2] == "least-used"
    assert "score=0.91" in parts[3]


# ---------------------------------------------------------------------------
# Round-trip last-pick
# ---------------------------------------------------------------------------


def test_write_and_read_last_pick(_isolate_pick_paths: Path) -> None:
    write_last_pick("account-a")
    got = read_last_pick()
    assert got is not None
    name, ts = got
    assert name == "account-a"
    assert ts.tzinfo is not None


def test_read_last_pick_missing_returns_none(_isolate_pick_paths: Path) -> None:
    assert read_last_pick() is None


def test_read_last_pick_invalid_json(_isolate_pick_paths: Path) -> None:
    path = pick_mod.last_pick_path()
    path.write_text("{not json")
    assert read_last_pick() is None


def test_read_last_pick_missing_fields(_isolate_pick_paths: Path) -> None:
    import json as _json

    path = pick_mod.last_pick_path()
    path.write_text(_json.dumps({"profile": "account-a"}))  # no timestamp
    assert read_last_pick() is None


# ---------------------------------------------------------------------------
# Stickiness env var
# ---------------------------------------------------------------------------


def test_stickiness_env_var_overrides_default(
    _isolate_pick_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_LB_STICKINESS", "10")
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=50),
        _entry("b", Health.OK, weekly_pct=10),
    )
    # Write last-pick far enough back to exceed the 10s window
    _write_last_pick_at("a", FIXED_NOW - timedelta(seconds=30))
    outcome = pick(cache, ["a", "b"], strategy=Strategy.STICKY, now=FIXED_NOW)
    # Outside window -> least-used wins -> b.
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


def test_stickiness_env_var_invalid_ignored(
    _isolate_pick_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_LB_STICKINESS", "not-a-number")
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=50),
        _entry("b", Health.OK, weekly_pct=10),
    )
    _write_last_pick_at("a", FIXED_NOW - timedelta(seconds=30))
    # Default 300s window applies -> 30s ago is sticky.
    outcome = pick(cache, ["a", "b"], strategy=Strategy.STICKY, now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "a"


# ---------------------------------------------------------------------------
# Pick log rotation
# ---------------------------------------------------------------------------


def test_pick_log_rotates_when_over_size(
    _isolate_pick_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pick_mod, "PICK_LOG_MAX_BYTES", 200)
    log_path = pick_mod.pick_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Pre-fill with lines that exceed the cap.
    log_path.write_text(("x" * 80 + "\n") * 10)
    # One more append triggers rotation.
    append_pick_log("account-a", Strategy.LEAST_USED, 1.0)
    after = log_path.read_text()
    assert len(after) < 800  # half-ish plus the new line


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_all_terminal_is_catchall_diagnosis() -> None:
    # Mix of AUTH_DEAD and WEEKLY_LIMIT -> falls through to ALL_TERMINAL.
    cache = _cache(
        _entry("a", Health.AUTH_DEAD),
        _entry(
            "b",
            Health.WEEKLY_LIMIT,
            weekly_reset_at=FIXED_NOW + timedelta(hours=1),
        ),
    )
    outcome = pick(cache, ["a", "b"], now=FIXED_NOW)
    assert outcome.chosen is None
    assert outcome.reason is PickFailureReason.ALL_TERMINAL


def test_missing_cache_entry_yields_unknown_stub() -> None:
    """A discovered profile without a cache entry is still selectable."""
    cache = HealthCache(updated_at=FIXED_NOW, profiles={})
    outcome = pick(cache, ["account-a"], now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "account-a"
    assert outcome.chosen.health is Health.UNKNOWN
