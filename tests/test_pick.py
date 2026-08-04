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
    overage_pct: int | None = None,
    expires_at: datetime | None = None,
    weekly_reset_at: datetime | None = None,
    session_reset_at: datetime | None = None,
    model_reset_at: datetime | None = None,
    probed_at: datetime | None = None,
) -> ProfileHealth:
    from claude_lb.models import ExtraUsage

    usage = None
    if weekly_pct is not None or session_pct is not None or overage_pct is not None:
        extra = (
            ExtraUsage(is_enabled=True, utilization=overage_pct)
            if overage_pct is not None else None
        )
        usage = Usage(
            weekly_pct=weekly_pct, session_pct=session_pct, extra=extra,
        )
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
        model_reset_at=model_reset_at,
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
    from claude_lb.pick import PickFailureReason as PFR
    from claude_lb.pick import _diagnose_failure

    outcome = _diagnose_failure([], require_ok=False)
    assert outcome.reason is PFR.NO_PROFILES


def test_diagnose_failure_with_mixed_dead_and_expired_returns_all_auth_expired() -> None:
    """A fleet that's a mix of AUTH_DEAD and AUTH_EXPIRED should resolve to
    ALL_AUTH_EXPIRED — the actionable subset (the operator can `refresh`
    the expired ones first, then deal with dead profiles separately)."""
    from datetime import UTC, datetime

    from claude_lb.models import ErrorInfo, Health, ProfileHealth
    from claude_lb.pick import PickFailureReason as PFR
    from claude_lb.pick import _diagnose_failure

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


# ---------------------------------------------------------------------------
# --avoid (Phase A)
# ---------------------------------------------------------------------------


def test_avoid_excludes_named_profile() -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10),
        _entry("b", Health.OK, weekly_pct=20),
        _entry("c", Health.OK, weekly_pct=30),
    )
    outcome = pick(
        cache,
        ["a", "b", "c"],
        strategy=Strategy.LEAST_USED,
        avoid={"a"},
        now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


def test_avoid_multiple_profiles() -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10),
        _entry("b", Health.OK, weekly_pct=20),
        _entry("c", Health.OK, weekly_pct=30),
    )
    outcome = pick(
        cache,
        ["a", "b", "c"],
        strategy=Strategy.LEAST_USED,
        avoid={"a", "b"},
        now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "c"


def test_avoid_all_falls_through_to_diagnose_failure() -> None:
    cache = _cache(
        _entry("a", Health.OK),
        _entry("b", Health.OK),
    )
    outcome = pick(
        cache,
        ["a", "b"],
        strategy=Strategy.LEAST_USED,
        avoid={"a", "b"},
        now=FIXED_NOW,
    )
    # Both healthy entries avoided -> failure. Reason is whatever diagnose
    # returns when health is fine but avoidance excluded everyone — it
    # falls through to ALL_TERMINAL because health is OK so no ALL_AUTH_*
    # categorical match.
    assert outcome.chosen is None
    assert outcome.reason is not None


def test_avoid_skips_sticky_pick_and_falls_through_to_strategy(
    _isolate_pick_paths: Path,
) -> None:
    """If the sticky pick is in the avoid list, the sticky shortcut is skipped
    and the underlying strategy (least-used as fallback) runs instead."""
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=80),  # sticky pin
        _entry("b", Health.OK, weekly_pct=10),
    )
    _write_last_pick_at("a", FIXED_NOW - timedelta(seconds=30))

    outcome = pick(
        cache,
        ["a", "b"],
        strategy=Strategy.STICKY,
        avoid={"a"},
        now=FIXED_NOW,
    )

    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"
    # Strategy used in this fallthrough should be the underlying STICKY
    # default (which routes to LEAST_USED) — see pick.py:419 for the rule
    # that surfaces STICKY in strategy_used when sticky was the request.
    assert outcome.strategy_used in (Strategy.LEAST_USED, Strategy.STICKY)


def test_avoid_composes_with_count(_isolate_pick_paths: Path) -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10),
        _entry("b", Health.OK, weekly_pct=20),
        _entry("c", Health.OK, weekly_pct=30),
        _entry("d", Health.OK, weekly_pct=40),
    )
    outcome = pick(
        cache,
        ["a", "b", "c", "d"],
        strategy=Strategy.LEAST_USED,
        avoid={"a"},
        count=2,
        now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    names = [e.name for e in outcome.chosen_many]
    assert names == ["b", "c"]
    assert "a" not in names


def test_avoid_with_count_returns_partial_when_too_few_remain() -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10),
        _entry("b", Health.OK, weekly_pct=20),
    )
    outcome = pick(
        cache,
        ["a", "b"],
        strategy=Strategy.LEAST_USED,
        avoid={"a"},
        count=5,
        now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert [e.name for e in outcome.chosen_many] == ["b"]


def test_empty_avoid_set_is_noop() -> None:
    """Passing avoid=set() (or omitting) must behave identically."""
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10),
        _entry("b", Health.OK, weekly_pct=20),
    )
    out_with = pick(
        cache, ["a", "b"], strategy=Strategy.LEAST_USED, avoid=set(), now=FIXED_NOW,
    )
    out_without = pick(
        cache, ["a", "b"], strategy=Strategy.LEAST_USED, now=FIXED_NOW,
    )
    assert out_with.chosen is not None
    assert out_without.chosen is not None
    assert out_with.chosen.name == out_without.chosen.name == "a"


def test_sticky_unaffected_when_sticky_pick_not_in_avoid(
    _isolate_pick_paths: Path,
) -> None:
    """Sanity: --avoid b shouldn't impact sticky pin to a."""
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=99),
        _entry("b", Health.OK, weekly_pct=10),
        _entry("c", Health.OK, weekly_pct=15),
    )
    _write_last_pick_at("a", FIXED_NOW - timedelta(seconds=30))
    outcome = pick(
        cache,
        ["a", "b", "c"],
        strategy=Strategy.STICKY,
        avoid={"b"},
        now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "a"
    assert outcome.strategy_used is Strategy.STICKY


# ---------------------------------------------------------------------------
# Phase B — lowest-overage strategy
# ---------------------------------------------------------------------------


def test_lowest_overage_prefers_minimum_utilization() -> None:
    cache = _cache(
        _entry("hot", Health.OK, weekly_pct=10, overage_pct=80),
        _entry("cool", Health.OK, weekly_pct=70, overage_pct=20),
    )
    outcome = pick(
        cache, ["hot", "cool"],
        strategy=Strategy.LOWEST_OVERAGE, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "cool"
    assert outcome.strategy_used is Strategy.LOWEST_OVERAGE


def test_lowest_overage_treats_missing_data_as_zero() -> None:
    """Profiles without overage data should rank as low (best) — the absence
    of cost signal isn't a reason to deprioritise them."""
    cache = _cache(
        _entry("with-overage", Health.OK, weekly_pct=10, overage_pct=50),
        _entry("no-overage", Health.OK, weekly_pct=70),
    )
    outcome = pick(
        cache, ["with-overage", "no-overage"],
        strategy=Strategy.LOWEST_OVERAGE, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "no-overage"


def test_lowest_overage_with_count_returns_top_n() -> None:
    cache = _cache(
        _entry("a", Health.OK, overage_pct=50),
        _entry("b", Health.OK, overage_pct=10),
        _entry("c", Health.OK, overage_pct=30),
    )
    outcome = pick(
        cache, ["a", "b", "c"],
        strategy=Strategy.LOWEST_OVERAGE, count=2, now=FIXED_NOW,
    )
    assert [e.name for e in outcome.chosen_many] == ["b", "c"]


def test_lowest_overage_unhealthy_profiles_filtered_first() -> None:
    cache = _cache(
        _entry("dead", Health.AUTH_DEAD, overage_pct=0),
        _entry("alive", Health.OK, overage_pct=99),
    )
    outcome = pick(
        cache, ["dead", "alive"],
        strategy=Strategy.LOWEST_OVERAGE, now=FIXED_NOW,
    )
    # AUTH_DEAD is filtered before lowest-overage even sees it.
    assert outcome.chosen is not None
    assert outcome.chosen.name == "alive"


# ---------------------------------------------------------------------------
# Phase B — --max-cost filter
# ---------------------------------------------------------------------------


def test_max_cost_excludes_profiles_at_or_above_threshold() -> None:
    cache = _cache(
        _entry("cheap", Health.OK, weekly_pct=10, overage_pct=20),
        _entry("expensive", Health.OK, weekly_pct=10, overage_pct=85),
    )
    outcome = pick(
        cache, ["cheap", "expensive"],
        strategy=Strategy.LEAST_USED, max_cost=80, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "cheap"
    # The excluded one shows up in the diagnostic dict.
    assert "expensive" in outcome.excluded_reasons
    assert "85%" in outcome.excluded_reasons["expensive"]


def test_max_cost_does_not_exclude_profiles_without_overage_data() -> None:
    """Pro/Team plans (no extra) should pass through max_cost unchallenged."""
    cache = _cache(
        _entry("pro-plan", Health.OK, weekly_pct=10),  # no overage
        _entry("max-plan", Health.OK, weekly_pct=20, overage_pct=99),
    )
    outcome = pick(
        cache, ["pro-plan", "max-plan"],
        strategy=Strategy.LEAST_USED, max_cost=50, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "pro-plan"
    assert "max-plan" in outcome.excluded_reasons
    assert "pro-plan" not in outcome.excluded_reasons


def test_max_cost_zero_excludes_any_profile_with_overage_at_all() -> None:
    """max_cost=0 should reject any profile reporting non-zero overage."""
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10, overage_pct=1),
        _entry("b", Health.OK, weekly_pct=10),  # no overage
    )
    outcome = pick(
        cache, ["a", "b"],
        strategy=Strategy.LEAST_USED, max_cost=0, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"
    assert "a" in outcome.excluded_reasons


def test_max_cost_failure_when_all_excluded() -> None:
    cache = _cache(
        _entry("a", Health.OK, overage_pct=99),
        _entry("b", Health.OK, overage_pct=85),
    )
    outcome = pick(
        cache, ["a", "b"],
        strategy=Strategy.LEAST_USED, max_cost=50, now=FIXED_NOW,
    )
    assert outcome.chosen is None
    assert "a" in outcome.excluded_reasons
    assert "b" in outcome.excluded_reasons


# ---------------------------------------------------------------------------
# Phase B — explain population (excluded_reasons + filter_scores)
# ---------------------------------------------------------------------------


def test_pick_populates_filter_scores_for_candidates() -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10),
        _entry("b", Health.OK, weekly_pct=20),
        _entry("c", Health.OK, weekly_pct=30),
    )
    outcome = pick(
        cache, ["a", "b", "c"],
        strategy=Strategy.LEAST_USED, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert set(outcome.filter_scores.keys()) == {"a", "b", "c"}
    assert outcome.filter_scores["a"] == 10.0
    assert outcome.filter_scores["b"] == 20.0


def test_pick_populates_excluded_reasons_for_health_failures() -> None:
    cache = _cache(
        _entry("dead", Health.AUTH_DEAD),
        _entry("ok", Health.OK, weekly_pct=10),
    )
    outcome = pick(
        cache, ["dead", "ok"],
        strategy=Strategy.LEAST_USED, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert "dead" in outcome.excluded_reasons
    assert "auth_dead" in outcome.excluded_reasons["dead"]
    assert "ok" in outcome.filter_scores


def test_pick_populates_excluded_reasons_for_avoid_filter() -> None:
    cache = _cache(
        _entry("a", Health.OK, weekly_pct=10),
        _entry("b", Health.OK, weekly_pct=20),
    )
    outcome = pick(
        cache, ["a", "b"],
        strategy=Strategy.LEAST_USED, avoid={"a"}, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert "a" in outcome.excluded_reasons
    assert "avoid" in outcome.excluded_reasons["a"].lower()


def test_pick_populates_excluded_reasons_for_max_cost_filter() -> None:
    cache = _cache(
        _entry("ok", Health.OK, weekly_pct=10, overage_pct=10),
        _entry("hot", Health.OK, weekly_pct=10, overage_pct=90),
    )
    outcome = pick(
        cache, ["ok", "hot"],
        strategy=Strategy.LEAST_USED, max_cost=80, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert "hot" in outcome.excluded_reasons
    assert "overage" in outcome.excluded_reasons["hot"].lower()


def test_pick_filter_scores_present_in_sticky_path(
    _isolate_pick_paths: Path,
) -> None:
    """Even on the sticky shortcut, filter_scores should be populated for the
    chosen profile so --explain rendering works in that path too."""
    cache = _cache(
        _entry("sticky", Health.OK, weekly_pct=50),
        _entry("other", Health.OK, weekly_pct=10),
    )
    _write_last_pick_at("sticky", FIXED_NOW - timedelta(seconds=30))
    outcome = pick(
        cache, ["sticky", "other"],
        strategy=Strategy.STICKY, now=FIXED_NOW,
    )
    assert outcome.chosen is not None
    assert outcome.chosen.name == "sticky"
    assert "sticky" in outcome.filter_scores


# ---------------------------------------------------------------------------
# MODEL_LIMIT — scoped-limit exhaustion (limits[] / Fable)
# ---------------------------------------------------------------------------


def test_model_limit_with_future_reset_is_filtered_out() -> None:
    cache = _cache(
        _entry(
            "a",
            Health.MODEL_LIMIT,
            model_reset_at=FIXED_NOW + timedelta(hours=24),
        ),
        _entry("b", Health.OK),
    )
    outcome = pick(cache, ["a", "b"], now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


def test_model_limit_with_past_reset_is_selectable() -> None:
    cache = _cache(
        _entry(
            "a",
            Health.MODEL_LIMIT,
            model_reset_at=FIXED_NOW - timedelta(minutes=1),
        ),
    )
    outcome = pick(cache, ["a"], strategy=Strategy.LEAST_USED, now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "a"


def test_model_limit_with_no_reset_is_filtered_out() -> None:
    """No model_reset_at at all -> treated as still-exhausted (matches the
    None-means-not-yet-recovered semantic of WEEKLY_LIMIT/SESSION_LIMIT)."""
    cache = _cache(
        _entry("a", Health.MODEL_LIMIT, model_reset_at=None),
        _entry("b", Health.OK),
    )
    outcome = pick(cache, ["a", "b"], now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "b"


def test_all_model_limit_signals_earliest_recovery() -> None:
    reset_a = FIXED_NOW + timedelta(hours=24)
    reset_b = FIXED_NOW + timedelta(hours=12)
    cache = _cache(
        _entry("a", Health.MODEL_LIMIT, model_reset_at=reset_a),
        _entry("b", Health.MODEL_LIMIT, model_reset_at=reset_b),
    )
    outcome = pick(cache, ["a", "b"], now=FIXED_NOW)
    assert outcome.chosen is None
    assert outcome.reason is PickFailureReason.ALL_MODEL_LIMIT
    assert outcome.earliest_recovery_at == reset_b


def test_model_limit_mixed_with_ok_prefers_ok() -> None:
    cache = _cache(
        _entry("limited", Health.MODEL_LIMIT, model_reset_at=FIXED_NOW + timedelta(hours=1)),
        _entry("ok", Health.OK, weekly_pct=50),
    )
    outcome = pick(cache, ["limited", "ok"], now=FIXED_NOW)
    assert outcome.chosen is not None
    assert outcome.chosen.name == "ok"


def test_earliest_recovery_uses_the_blocking_window_not_the_soonest() -> None:
    """A profile recovers when ITS blocking window resets, not the soonest one.

    A model-limited profile whose session window rolls over in an hour is
    still model-limited until the scoped window resets days later. Reporting
    the session reset told a caller to retry straight into the same failure.
    """
    from claude_lb.pick import _earliest_recovery

    soon = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    later = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    entry = ProfileHealth(
        name="a",
        health=Health.MODEL_LIMIT,
        probed_at=soon,
        session_reset_at=soon,
        model_reset_at=later,
    )
    assert _earliest_recovery([entry]) == later
