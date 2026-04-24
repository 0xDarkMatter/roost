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
# Health style
# ---------------------------------------------------------------------------


def test_health_style_covers_all_states() -> None:
    for value in [h.value for h in Health]:
        assert output._health_style(value)


def test_health_style_unknown_bucket_fallback() -> None:
    assert output._health_style("not_a_real_state") == "white"
