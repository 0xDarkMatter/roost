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


# ---------------------------------------------------------------------------
# Multi-pick (count > 1)
# ---------------------------------------------------------------------------


def test_count_returns_top_n_in_strategy_order() -> None:
    """count=3 with least-used returns 3 profiles ordered by ascending weekly%."""
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=40),
        _entry("b", Health.OK, weekly_pct=10),
        _entry("c", Health.OK, weekly_pct=20),
        _entry("d", Health.OK, weekly_pct=60),
    )
    outcome = pick(
        cache, ["a", "b", "c", "d"],
        strategy=Strategy.LEAST_USED, count=3, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"  # primary = lowest weekly
    assert [e.name for e in outcome.chosen_many] == ["b", "c", "a"]


def test_count_returns_fewer_when_candidates_limited() -> None:
    """If only 2 profiles pass the ladder, count=5 returns 2, not a failure."""
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10),
        _entry("b", Health.OK, weekly_pct=20),
        _entry("c", Health.AUTH_DEAD),
    )
    outcome = pick(
        cache, ["a", "b", "c"],
        strategy=Strategy.LEAST_USED, count=5, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert len(outcome.chosen_many) == 2
    assert {e.name for e in outcome.chosen_many} == {"a", "b"}


def test_count_greater_than_one_ignores_stickiness(
    _isolate_pick_paths: Path,
) -> None:
    """Sticky semantic (pin to last pick) doesn't compose with multi-pick."""
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=50),
        _entry("b", Health.OK, weekly_pct=10),
        _entry("c", Health.OK, weekly_pct=30),
    )
    _write_last_pick_at("a", FIXED_NOW - timedelta(seconds=30))
    outcome = pick(
        cache, ["a", "b", "c"],
        strategy=Strategy.STICKY, count=2, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    # Without stickiness, least-used ordering wins: b (10%) then c (30%).
    assert [e.name for e in outcome.chosen_many] == ["b", "c"]
    assert outcome.chosen.name == "b"


def test_count_one_still_honours_stickiness(_isolate_pick_paths: Path) -> None:
    """Explicit count=1 must not disable stickiness — it's the default."""
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=50),
        _entry("b", Health.OK, weekly_pct=10),
    )
    _write_last_pick_at("a", FIXED_NOW - timedelta(seconds=30))
    outcome = pick(
        cache, ["a", "b"],
        strategy=Strategy.STICKY, count=1, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "a"
    assert outcome.chosen_many == [outcome.chosen]


def test_count_zero_treated_as_one() -> None:
    """count=0 would be a useless invocation; clamp to 1 defensively."""
    cache = _cache(_entry("a", Health.OK))
    outcome = pick(cache, ["a"], count=0, now=FIXED_NOW)
    assert outcome.chosen is not None
    assert len(outcome.chosen_many) == 1


def test_count_fails_when_no_candidates_pass_ladder() -> None:
    """All AUTH_DEAD -> failure regardless of count."""
    cache = _cache(
        _entry("a", Health.AUTH_DEAD),
        _entry("b", Health.AUTH_DEAD),
    )
    outcome = pick(cache, ["a", "b"], count=3, now=FIXED_NOW)
    assert outcome.chosen is None
    assert outcome.chosen_many == []
    assert outcome.reason is PickFailureReason.ALL_AUTH_DEAD


# ---------------------------------------------------------------------------
# Internal helpers — _iso / _parse_iso symmetry + degenerate inputs
# ---------------------------------------------------------------------------


def test_pick_iso_handles_none_input() -> None:
    from claude_lb.pick import _iso

    assert _iso(None) is None


def test_pick_iso_assumes_utc_for_naive_datetime() -> None:
    from datetime import datetime as _dt

    from claude_lb.pick import _iso

    out = _iso(_dt(2026, 4, 25, 10, 30))
    assert out is not None
    assert out.endswith("+00:00")


def test_pick_parse_iso_handles_none_and_empty() -> None:
    from claude_lb.pick import _parse_iso

    assert _parse_iso(None) is None
    assert _parse_iso("") is None


def test_pick_parse_iso_handles_z_suffix() -> None:
    from claude_lb.pick import _parse_iso

    parsed = _parse_iso("2026-04-25T10:30:00Z")
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_pick_parse_iso_handles_naive_input() -> None:
    """A timestamp without offset should be parsed and assumed UTC."""
    from claude_lb.pick import _parse_iso

    parsed = _parse_iso("2026-04-25T10:30:00")
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_pick_parse_iso_returns_none_for_garbage() -> None:
    from claude_lb.pick import _parse_iso

    assert _parse_iso("not a timestamp") is None


# ---------------------------------------------------------------------------
# Pick log rotation — exercises the size-check + half-truncate path
# ---------------------------------------------------------------------------


def test_pick_log_rotation_truncates_to_half_when_oversize(tmp_path) -> None:
    """Once picks.log exceeds PICK_LOG_MAX_BYTES, the oldest half should
    be dropped on the next rotate. Use a tiny file + monkeypatched limit."""
    log_path = tmp_path / "picks.log"
    log_path.write_text("\n".join(f"line-{i}" for i in range(100)) + "\n")
    original = pick_mod.PICK_LOG_MAX_BYTES
    pick_mod.PICK_LOG_MAX_BYTES = 100  # tiny
    try:
        pick_mod._rotate_pick_log_if_needed(log_path)
    finally:
        pick_mod.PICK_LOG_MAX_BYTES = original
    remaining = log_path.read_text().splitlines()
    assert len(remaining) == 50  # half of 100


def test_pick_log_rotation_skips_when_under_limit(tmp_path) -> None:
    log_path = tmp_path / "picks.log"
    log_path.write_text("only one line\n")
    pick_mod._rotate_pick_log_if_needed(log_path)
    assert log_path.read_text() == "only one line\n"


def test_pick_log_rotation_no_op_when_file_missing(tmp_path) -> None:
    """A missing file must not crash rotation — just return cleanly."""
    pick_mod._rotate_pick_log_if_needed(tmp_path / "no-such-file.log")


# ---------------------------------------------------------------------------
# write_last_pick exception cleanup
# ---------------------------------------------------------------------------


def test_write_last_pick_unlinks_tempfile_on_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace fails persistently inside write_last_pick, the tempfile
    must be cleaned up — otherwise stale .last-pick-*.json.tmp files
    accumulate. Persistent replace failures are now logged + swallowed
    (last-pick.json is a stickiness hint, not load-bearing)."""
    import os as _os

    from claude_lb.pick import write_last_pick

    target = tmp_path / "last-pick.json"
    real_replace = _os.replace

    def boom_replace(*a, **kw):
        raise OSError("simulated")

    # Skip retry sleeps so the test stays fast.
    monkeypatch.setattr("claude_lb.pick.time.sleep", lambda _s: None)
    monkeypatch.setattr(_os, "replace", boom_replace)
    write_last_pick("account-a", path=target)  # no exception — swallowed
    monkeypatch.setattr(_os, "replace", real_replace)
    leftover = list(tmp_path.glob(".last-pick-*.json.tmp"))
    assert leftover == []
    assert not target.exists()  # nothing was written


def test_write_last_pick_retries_on_transient_permission_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows-style WinError 5 transient races should be absorbed by the
    retry helper — first call fails, second succeeds, file is written."""
    import os as _os

    from claude_lb.pick import write_last_pick

    target = tmp_path / "last-pick.json"
    real_replace = _os.replace
    call_count = {"n": 0}

    def flaky_replace(src, dst, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise PermissionError("[WinError 5] Access is denied")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr("claude_lb.pick.time.sleep", lambda _s: None)
    monkeypatch.setattr(_os, "replace", flaky_replace)
    write_last_pick("account-a", path=target)
    assert call_count["n"] == 2
    assert target.exists()
    assert "account-a" in target.read_text()


# ---------------------------------------------------------------------------
# read_last_pick — corrupt / missing / wrong-shape
# ---------------------------------------------------------------------------


def test_read_last_pick_returns_none_when_missing(tmp_path) -> None:
    from claude_lb.pick import read_last_pick

    assert read_last_pick(path=tmp_path / "no-such.json") is None


def test_read_last_pick_returns_none_for_corrupt_json(tmp_path) -> None:
    from claude_lb.pick import read_last_pick

    target = tmp_path / "bad.json"
    target.write_text("{not json")
    assert read_last_pick(path=target) is None


def test_read_last_pick_returns_none_for_wrong_shape(tmp_path) -> None:
    """Cache file with valid JSON but missing fields shouldn't crash."""
    import json as _json

    from claude_lb.pick import read_last_pick

    target = tmp_path / "wrong.json"
    target.write_text(_json.dumps({"profile": 12345, "timestamp": "x"}))
    assert read_last_pick(path=target) is None


# ---------------------------------------------------------------------------
# _diagnose_failure — every reason path
# ---------------------------------------------------------------------------


def test_diagnose_failure_with_empty_entries_returns_no_profiles() -> None:
    """Filter ladder yielded zero candidates because there are no profiles
    at all (vs. all-filtered) — should return NO_PROFILES."""
    from claude_lb.pick import _diagnose_failure
    from claude_lb.pick import PickFailureReason as PFR

    outcome = _diagnose_failure([], require_ok=False)
    assert outcome.reason is PFR.NO_PROFILES


def test_diagnose_failure_with_mixed_dead_and_expired_returns_all_auth_expired() -> None:
    """A fleet that's a mix of AUTH_DEAD and AUTH_EXPIRED should resolve to
    ALL_AUTH_EXPIRED — the actionable subset (the operator can `refresh`
    the expired ones first, then deal with dead profiles separately)."""
    from datetime import UTC, datetime

    from claude_lb.models import ErrorInfo, Health, ProfileHealth
    from claude_lb.pick import _diagnose_failure
    from claude_lb.pick import PickFailureReason as PFR

    now = datetime.now(UTC)
    entries = [
        ProfileHealth(
            name="dead",
            health=Health.AUTH_DEAD,
            probed_at=now,
            error=ErrorInfo(type="auth_error", message="dead"),
            credentials_mtime=1000.0,
        ),
        ProfileHealth(
            name="expired",
            health=Health.AUTH_EXPIRED,
            probed_at=now,
            error=ErrorInfo(type="token_expired", message="expired"),
            credentials_mtime=1000.0,
        ),
    ]
    outcome = _diagnose_failure(entries, require_ok=False)
    assert outcome.reason is PFR.ALL_AUTH_EXPIRED


def test_is_selectable_rejects_non_ok_when_require_ok_is_true() -> None:
    """The require_ok branch in _is_selectable should drop non-OK profiles
    even when they'd otherwise pass the ladder."""
    from datetime import UTC, datetime, timedelta

    from claude_lb.models import Health, ProfileHealth
    from claude_lb.pick import _is_selectable

    now = datetime.now(UTC)
    # NETWORK_ERROR normally passes the ladder (transient), but require_ok
    # should still drop it.
    entry = ProfileHealth(
        name="netfail",
        health=Health.NETWORK_ERROR,
        probed_at=now,
        expires_at=now + timedelta(minutes=5),
        credentials_mtime=1000.0,
    )
    assert _is_selectable(entry, now, require_ok=True) is False
    # Without require_ok, NETWORK_ERROR passes
    assert _is_selectable(entry, now, require_ok=False) is True
