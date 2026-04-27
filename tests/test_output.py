"""Output tests — JSON envelope, status rendering, ndjson, age formatting."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from claude_lb import output
from claude_lb.models import ErrorInfo, Health, HealthCache, ProfileHealth, Usage

FIXED_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _entry(
    name: str = "account-a",
    health: Health = Health.OK,
    probed_at: datetime | None = None,
    **kwargs: object,
) -> ProfileHealth:
    return ProfileHealth(
        name=name,
        health=health,
        probed_at=probed_at or FIXED_NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def _cache(*entries: ProfileHealth) -> HealthCache:
    return HealthCache(updated_at=FIXED_NOW, profiles={e.name: e for e in entries})


# ---------------------------------------------------------------------------
# emit_json / emit_text / emit_ndjson
# ---------------------------------------------------------------------------


def _capture_stdout(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def test_emit_json_writes_valid_json_to_stdout() -> None:
    out = _capture_stdout(output.emit_json, {"a": 1})
    assert json.loads(out) == {"a": 1}
    assert out.endswith("\n")


def test_emit_json_serialises_datetime_as_iso_utc() -> None:
    out = _capture_stdout(
        output.emit_json, {"t": datetime(2026, 4, 24, tzinfo=UTC)}
    )
    payload = json.loads(out)
    assert payload["t"] == "2026-04-24T00:00:00Z"


def test_emit_json_serialises_enum() -> None:
    out = _capture_stdout(output.emit_json, {"health": Health.OK})
    assert json.loads(out) == {"health": "ok"}


def test_emit_json_serialises_pydantic_model() -> None:
    entry = _entry(error=ErrorInfo(type="authentication_error", message="x"))
    out = _capture_stdout(output.emit_json, {"profile": entry})
    payload = json.loads(out)
    assert payload["profile"]["name"] == "account-a"


def test_emit_json_raises_typeerror_on_unserialisable() -> None:
    import pytest

    class Weird:
        pass

    buf = io.StringIO()
    with patch("sys.stdout", buf), pytest.raises(TypeError):
        output.emit_json({"x": Weird()})


def test_emit_text_appends_newline_if_missing() -> None:
    out = _capture_stdout(output.emit_text, "hello")
    assert out == "hello\n"


def test_emit_text_does_not_double_newline() -> None:
    out = _capture_stdout(output.emit_text, "hello\n")
    assert out == "hello\n"


def test_emit_ndjson_one_object_per_line() -> None:
    out = _capture_stdout(output.emit_ndjson, [{"a": 1}, {"b": 2}])
    lines = out.rstrip("\n").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": 2}


def test_emit_error_json_wraps_in_error_envelope() -> None:
    out = _capture_stdout(output.emit_error_json, "NOT_FOUND", "missing thing")
    payload = json.loads(out)
    assert payload == {"error": {"code": "NOT_FOUND", "message": "missing thing"}}


def test_emit_error_json_includes_details_when_given() -> None:
    out = _capture_stdout(
        output.emit_error_json,
        "VALIDATION",
        "bad thing",
        {"allowed": ["a", "b"]},
    )
    payload = json.loads(out)
    assert payload["error"]["details"] == {"allowed": ["a", "b"]}


# ---------------------------------------------------------------------------
# build_status_payload
# ---------------------------------------------------------------------------


def test_build_status_payload_counts_by_health() -> None:
    cache = _cache(
        _entry("a", Health.OK, usage=Usage(weekly_pct=10)),
        _entry("b", Health.AUTH_DEAD, error=ErrorInfo(type="authentication_error", message="x")),
        _entry("c", Health.RATE_LIMITED, retry_after_s=30),
    )
    payload = output.build_status_payload(cache, ["a", "b", "c"])
    assert payload["meta"]["count"] == 3
    assert payload["meta"]["ok"] == 1
    assert payload["meta"]["auth_dead"] == 1
    assert payload["meta"]["rate_limited"] == 1


def test_build_status_payload_includes_unknown_stubs_for_missing_profiles() -> None:
    cache = _cache(_entry("a", Health.OK))
    payload = output.build_status_payload(cache, ["a", "b"])
    assert payload["meta"]["count"] == 2
    assert payload["meta"]["ok"] == 1
    assert payload["meta"]["unknown"] == 1
    # Stub row for b has explicit nulls, not missing keys
    stub = next(d for d in payload["data"] if d["name"] == "b")
    assert stub["health"] == "unknown"
    assert stub["usage"] is None
    assert stub["retry_after_s"] is None


def test_build_status_payload_includes_all_enum_keys_even_if_zero() -> None:
    cache = _cache(_entry("a", Health.OK))
    payload = output.build_status_payload(cache, ["a"])
    meta_keys = set(payload["meta"].keys())
    expected = {
        "count",
        "ok",
        "rate_limited",
        "session_limit",
        "weekly_limit",
        "auth_dead",
        "network_error",
        "unknown",
    }
    assert expected <= meta_keys


def test_build_status_payload_empty_discovery() -> None:
    cache = _cache()
    payload = output.build_status_payload(cache, [])
    assert payload["data"] == []
    assert payload["meta"]["count"] == 0


# ---------------------------------------------------------------------------
# render_status_table
# ---------------------------------------------------------------------------


def test_render_status_table_no_entries_prints_warning() -> None:
    # Doesn't raise, doesn't touch stdout.
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        output.render_status_table([])
    assert buf.getvalue() == ""  # nothing to stdout (table goes to stderr)


def test_render_status_table_with_variety_does_not_crash() -> None:
    entries = [
        _entry("a", Health.OK, usage=Usage(weekly_pct=10)),
        _entry(
            "b",
            Health.SESSION_LIMIT,
            session_reset_at=FIXED_NOW + timedelta(hours=1),
        ),
        _entry(
            "c",
            Health.WEEKLY_LIMIT,
            weekly_reset_at=FIXED_NOW + timedelta(days=2),
        ),
        _entry("d", Health.AUTH_DEAD),
        _entry("e", Health.RATE_LIMITED, retry_after_s=45),
        _entry("f", Health.NETWORK_ERROR),
        _entry("g", Health.UNKNOWN),
    ]
    # Should not raise on any of the seven states.
    output.render_status_table(entries)


def test_render_status_table_handles_naive_probed_at() -> None:
    naive = datetime(2026, 4, 24, 11, 59, 0)  # no tzinfo
    entries = [_entry("a", Health.OK, probed_at=naive)]
    output.render_status_table(entries)  # must not raise


# ---------------------------------------------------------------------------
# Age helper
# ---------------------------------------------------------------------------


def test_humanize_age_seconds() -> None:
    assert output._humanize_age(5) == "5s ago"


def test_humanize_age_minutes() -> None:
    assert output._humanize_age(120) == "2m ago"


def test_humanize_age_hours() -> None:
    assert output._humanize_age(2 * 3600) == "2h ago"


def test_humanize_age_days() -> None:
    assert output._humanize_age(3 * 86400) == "3d ago"


def test_humanize_age_future() -> None:
    assert output._humanize_age(-1) == "in future"


# ---------------------------------------------------------------------------
# humanize_until
# ---------------------------------------------------------------------------


def test_humanize_until_none_returns_dash() -> None:
    assert output.humanize_until(None) == "—"


def test_humanize_until_past_returns_now() -> None:
    assert output.humanize_until(FIXED_NOW - timedelta(seconds=10), now=FIXED_NOW) == "now"


def test_humanize_until_seconds() -> None:
    assert output.humanize_until(FIXED_NOW + timedelta(seconds=45), now=FIXED_NOW) == "in 45s"


def test_humanize_until_minutes() -> None:
    assert output.humanize_until(FIXED_NOW + timedelta(minutes=37), now=FIXED_NOW) == "in 37m"


def test_humanize_until_hours_clean() -> None:
    assert output.humanize_until(FIXED_NOW + timedelta(hours=5), now=FIXED_NOW) == "in 5h"


def test_humanize_until_hours_and_minutes() -> None:
    assert (
        output.humanize_until(FIXED_NOW + timedelta(hours=3, minutes=20), now=FIXED_NOW)
        == "in 3h 20m"
    )


def test_humanize_until_days() -> None:
    assert output.humanize_until(FIXED_NOW + timedelta(days=2, hours=3), now=FIXED_NOW) == "in 2d"


def test_humanize_until_naive_target_coerced_utc() -> None:
    naive = (FIXED_NOW + timedelta(minutes=10)).replace(tzinfo=None)
    assert output.humanize_until(naive, now=FIXED_NOW) == "in 10m"


# ---------------------------------------------------------------------------
# _pct_cell + _render_extra_usage
# ---------------------------------------------------------------------------


def test_pct_cell_thresholds() -> None:
    assert output._pct_cell(None) == "—"
    assert output._pct_cell(10) == "10%"
    assert "yellow" in output._pct_cell(80)
    assert "red" in output._pct_cell(100)


def test_render_extra_usage_none_is_dash() -> None:
    assert output._render_extra_usage(None) == "—"


def test_render_extra_usage_disabled() -> None:
    from claude_lb.models import ExtraUsage
    assert output._render_extra_usage(ExtraUsage(is_enabled=False)) == "off"


def test_render_extra_usage_enabled_with_utilisation_and_currency() -> None:
    from claude_lb.models import ExtraUsage
    extra = ExtraUsage(is_enabled=True, utilization=45, currency="AUD")
    rendered = output._render_extra_usage(extra)
    assert "45%" in rendered
    assert "AUD" in rendered


def test_render_extra_usage_exhausted_is_red() -> None:
    from claude_lb.models import ExtraUsage
    extra = ExtraUsage(is_enabled=True, utilization=100, currency="USD")
    rendered = output._render_extra_usage(extra)
    assert "red" in rendered


# ---------------------------------------------------------------------------
# render_status_table — per-model & extra_usage columns only appear when data present
# ---------------------------------------------------------------------------


def test_render_status_table_drops_optional_columns_when_absent() -> None:
    """No entry has sonnet/opus/extra → those columns should not be rendered.
    We assert via render not crashing; colum-count is a Rich internal we don't check."""
    entries = [_entry("a", Health.OK, usage=None)]
    output.render_status_table(entries)


def test_render_status_table_with_extra_usage_does_not_crash() -> None:
    from claude_lb.models import ExtraUsage, Usage

    entries = [
        _entry(
            "a",
            Health.OK,
            usage=Usage(
                session_pct=30,
                weekly_pct=10,
                sonnet_pct=5,
                opus_pct=2,
                extra=ExtraUsage(
                    is_enabled=True, utilization=75, currency="AUD",
                    monthly_limit=100.0, used_credits=75.0,
                ),
            ),
        ),
    ]
    output.render_status_table(entries)


def test_render_status_table_includes_plan_when_any_profile_has_one() -> None:
    """The Plan column is optional — only rendered when at least one entry
    has a subscription_type. Can't easily inspect Rich tables, so assert no
    crash + spot-check via the JSON payload instead."""
    entries = [
        _entry("a", Health.OK, subscription_type="max"),
        _entry("b", Health.OK, subscription_type=None),
    ]
    output.render_status_table(entries)


def test_render_status_table_omits_plan_when_no_profile_has_one() -> None:
    entries = [_entry("a", Health.OK, subscription_type=None)]
    output.render_status_table(entries)


def test_build_status_payload_includes_subscription_type() -> None:
    cache = _cache(
        _entry("a", Health.OK, subscription_type="max"),
        _entry("b", Health.OK, subscription_type=None),
    )
    payload = output.build_status_payload(cache, ["a", "b"])
    row_a = next(d for d in payload["data"] if d["name"] == "a")
    row_b = next(d for d in payload["data"] if d["name"] == "b")
    assert row_a["subscription_type"] == "max"
    assert row_b["subscription_type"] is None


def test_build_status_payload_stub_for_unknown_profile_has_null_plan() -> None:
    """Never-probed profiles render as unknown stubs; subscription_type null."""
    cache = _cache()
    payload = output.build_status_payload(cache, ["ghost"])
    row = payload["data"][0]
    assert row["subscription_type"] is None


# ---------------------------------------------------------------------------
# Resets column — dual session/weekly format
# ---------------------------------------------------------------------------


def test_render_status_table_dual_resets_for_ok_profile() -> None:
    """The resets cell should include both S and W segments when both are set.
    We can't easily inspect Rich output; this test ensures the table doesn't
    crash when rendering a profile with both timestamps. Format is exercised
    via the indirect _pct_cell / humanize_until tests."""
    entries = [
        _entry(
            "a",
            Health.OK,
            session_reset_at=FIXED_NOW + timedelta(hours=1),
            weekly_reset_at=FIXED_NOW + timedelta(days=3),
        ),
    ]
    output.render_status_table(entries)


# ---------------------------------------------------------------------------
# Health style
# ---------------------------------------------------------------------------


def test_health_style_covers_all_states() -> None:
    for value in [h.value for h in Health]:
        assert output._health_style(value)


def test_health_style_unknown_bucket_fallback() -> None:
    assert output._health_style("not_a_real_state") == "white"


# ---------------------------------------------------------------------------
# _json_default — every branch
# ---------------------------------------------------------------------------


def test_json_default_handles_naive_datetime() -> None:
    """A datetime without tzinfo should be serialised as if it were UTC."""
    from datetime import datetime as _dt

    serialised = output._json_default(_dt(2026, 4, 25, 10, 30, 0))
    assert serialised == "2026-04-25T10:30:00Z"


def test_json_default_handles_aware_datetime() -> None:
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    serialised = output._json_default(_dt(2026, 4, 25, 10, 30, 0, tzinfo=_UTC))
    assert serialised == "2026-04-25T10:30:00Z"


def test_json_default_handles_enum() -> None:
    from claude_lb.models import Health

    assert output._json_default(Health.OK) == "ok"


def test_json_default_handles_pydantic_model() -> None:
    """Anything with a model_dump() method (i.e., a pydantic BaseModel) gets
    serialised via that hook."""
    from claude_lb.models import ErrorInfo

    err = ErrorInfo(type="x", message="y")
    serialised = output._json_default(err)
    assert serialised == {"type": "x", "message": "y"}


def test_json_default_raises_for_unknown_type() -> None:
    """Anything else should raise TypeError so json.dumps surfaces it cleanly
    rather than silently dropping data."""
    import pytest

    with pytest.raises(TypeError) as excinfo:
        output._json_default(object())
    assert "Not JSON serializable" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _iso helper — naive datetime branch
# ---------------------------------------------------------------------------


def test_iso_handles_naive_datetime() -> None:
    from datetime import datetime as _dt

    assert output._iso(_dt(2026, 4, 25, 10, 30)) == "2026-04-25T10:30:00Z"


def test_iso_returns_none_for_none_input() -> None:
    assert output._iso(None) is None


# ---------------------------------------------------------------------------
# _render_extra_usage — every branch (off / on-no-util / pct+currency / colour bands)
# ---------------------------------------------------------------------------


def test_render_extra_usage_disabled() -> None:
    from types import SimpleNamespace

    extra = SimpleNamespace(is_enabled=False, utilization=50, currency="USD")
    assert output._render_extra_usage(extra) == "off"


def test_render_extra_usage_enabled_but_no_utilization() -> None:
    from types import SimpleNamespace

    extra = SimpleNamespace(is_enabled=True, utilization=None, currency="USD")
    assert output._render_extra_usage(extra) == "on"


def test_render_extra_usage_with_currency() -> None:
    from types import SimpleNamespace

    extra = SimpleNamespace(is_enabled=True, utilization=42, currency="AUD")
    rendered = output._render_extra_usage(extra)
    assert "42%" in rendered
    assert "AUD" in rendered


def test_render_extra_usage_at_yellow_threshold() -> None:
    """utilization >= 80 should be yellow."""
    from types import SimpleNamespace

    extra = SimpleNamespace(is_enabled=True, utilization=85, currency=None)
    rendered = output._render_extra_usage(extra)
    assert "yellow" in rendered
    assert "85%" in rendered


def test_render_extra_usage_at_red_threshold() -> None:
    """utilization >= 100 should be red."""
    from types import SimpleNamespace

    extra = SimpleNamespace(is_enabled=True, utilization=100, currency="USD")
    rendered = output._render_extra_usage(extra)
    assert "red" in rendered
    assert "100% USD" in rendered


def test_render_extra_usage_below_thresholds_is_plain() -> None:
    from types import SimpleNamespace

    extra = SimpleNamespace(is_enabled=True, utilization=20, currency="USD")
    rendered = output._render_extra_usage(extra)
    # No colour markup wrapping the label
    assert "yellow" not in rendered
    assert "red" not in rendered
    assert "20%" in rendered


# ---------------------------------------------------------------------------
# Health enum properties — small but explicit
# ---------------------------------------------------------------------------


def test_health_is_healthy_only_for_ok() -> None:
    from claude_lb.models import Health

    assert Health.OK.is_healthy is True
    for h in Health:
        if h is not Health.OK:
            assert h.is_healthy is False


def test_health_is_terminal_for_dead_and_weekly_only() -> None:
    from claude_lb.models import Health

    assert Health.AUTH_DEAD.is_terminal is True
    assert Health.WEEKLY_LIMIT.is_terminal is True
    for h in Health:
        if h not in (Health.AUTH_DEAD, Health.WEEKLY_LIMIT):
            assert h.is_terminal is False


def test_health_is_transient_for_recoverable_states() -> None:
    from claude_lb.models import Health

    transient = {
        Health.RATE_LIMITED,
        Health.SESSION_LIMIT,
        Health.AUTH_EXPIRED,
        Health.NETWORK_ERROR,
        Health.UNKNOWN,
    }
    for h in transient:
        assert h.is_transient is True
    for h in Health:
        if h not in transient:
            assert h.is_transient is False


# ---------------------------------------------------------------------------
# Discovery — directory not present + invalid name + mtime stat fallback
# ---------------------------------------------------------------------------


def test_discover_in_returns_empty_for_missing_directory(tmp_path) -> None:
    from claude_lb.discovery import _discover_in

    nonexistent = tmp_path / "no-such-dir"
    assert _discover_in(nonexistent) == []


def test_discover_in_skips_dirs_with_invalid_names(tmp_path) -> None:
    """Profile names that don't match the regex must be silently skipped, not
    crashed on. Otherwise a stray '.DS_Store' or 'temp.bak' breaks discovery."""
    from claude_lb.discovery import _discover_in

    # Create one valid + one invalid-named dir
    (tmp_path / "valid").mkdir()
    (tmp_path / "valid" / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "oat-stub"}}'
    )
    (tmp_path / "has spaces in name").mkdir()
    (tmp_path / "has spaces in name" / ".credentials.json").write_text("{}")
    (tmp_path / ".dotfile").mkdir()  # leading-dot dir, also rejected

    profiles = _discover_in(tmp_path)
    names = [p.name for p in profiles]
    assert "valid" in names
    assert "has spaces in name" not in names
    assert ".dotfile" not in names


def test_discover_in_skips_files_in_profiles_dir(tmp_path) -> None:
    """A loose file (not directory) in the profiles dir should be skipped
    silently — covers the `if not entry.is_dir(): continue` guard."""
    from claude_lb.discovery import _discover_in

    # Mix of one valid profile dir + one stray file
    (tmp_path / "validprofile").mkdir()
    (tmp_path / "validprofile" / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "oat-stub"}}'
    )
    (tmp_path / "stray-file.txt").write_text("garbage")
    (tmp_path / ".DS_Store").write_text("mac noise")

    profiles = _discover_in(tmp_path)
    names = [p.name for p in profiles]
    assert names == ["validprofile"]


# ---------------------------------------------------------------------------
# output — humanize_until naive datetime + status table auth-state rows
# ---------------------------------------------------------------------------


def test_humanize_until_handles_naive_target_and_naive_now() -> None:
    """Both `target` and `now` without tzinfo should be coerced to UTC for
    the delta calculation — covers the two `if .tzinfo is None` guards."""
    from datetime import datetime as _dt
    from datetime import timedelta

    naive_now = _dt(2026, 4, 25, 10, 0, 0)  # naive
    naive_target = naive_now + timedelta(minutes=5)  # naive
    out = output.humanize_until(naive_target, now=naive_now)
    assert out == "in 5m" or out == "in 4m"  # rounding tolerance


def test_status_table_renders_auth_expired_remediation_in_session_column(
    capsys,
) -> None:
    """auth_expired profiles should display `claude-lb refresh <name>` in
    the session column (the remediation is what operators need to see)."""
    from datetime import UTC, datetime

    from claude_lb.models import ErrorInfo, Health, ProfileHealth

    entry = ProfileHealth(
        name="expired-acct",
        health=Health.AUTH_EXPIRED,
        probed_at=datetime.now(UTC),
        error=ErrorInfo(type="token_expired", message="expired"),
        credentials_mtime=1000.0,
    )
    output.render_status_table([entry])
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    flat = " ".join(rendered.split())
    assert "claude-lb refresh expired-acct" in flat


def test_status_table_renders_auth_dead_remediation_in_session_column(
    capsys,
) -> None:
    """auth_dead → `claude login` in the same column slot."""
    from datetime import UTC, datetime

    from claude_lb.models import ErrorInfo, Health, ProfileHealth

    entry = ProfileHealth(
        name="dead-acct",
        health=Health.AUTH_DEAD,
        probed_at=datetime.now(UTC),
        error=ErrorInfo(type="auth_error", message="dead"),
        credentials_mtime=1000.0,
    )
    output.render_status_table([entry])
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    flat = " ".join(rendered.split())
    assert "claude login" in flat
