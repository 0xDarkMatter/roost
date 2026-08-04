"""Tests for platform_status — caching + format helpers + stale fallback."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from claude_lb import paths as paths_mod
from claude_lb import platform_status as ps


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect config_dir to a tmp path so the cache writes don't pollute
    the user's real ~/.config/roost. Also mirrors the override into the
    platform_status module since it imports config_dir at module-load time."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(paths_mod, "config_dir", lambda: config)
    monkeypatch.setattr(paths_mod, "ensure_config_dir", lambda: config)
    monkeypatch.setattr(ps, "config_dir", lambda: config)
    monkeypatch.setattr(ps, "ensure_config_dir", lambda: config)
    return config


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _summary(
    *,
    indicator: str = "none",
    description: str = "All Systems Operational",
    incidents: list | None = None,
    components: list | None = None,
) -> dict:
    return {
        "page": {"name": "Claude"},
        "status": {"indicator": indicator, "description": description},
        "incidents": incidents or [],
        "components": components or [],
    }


def test_parse_clean_summary_is_clean() -> None:
    status = ps._parse_summary(_summary())
    assert status.is_clean is True
    assert status.has_warning is False
    assert status.indicator == "none"


def test_parse_filters_resolved_incidents() -> None:
    status = ps._parse_summary(_summary(incidents=[
        {"name": "Old", "status": "resolved", "impact": "minor"},
        {"name": "Active", "status": "monitoring", "impact": "minor"},
    ]))
    assert len(status.active_incidents) == 1
    assert status.active_incidents[0]["name"] == "Active"


def test_parse_filters_operational_components() -> None:
    status = ps._parse_summary(_summary(components=[
        {"name": "API", "status": "operational"},
        {"name": "Console", "status": "degraded_performance"},
    ]))
    assert len(status.degraded_components) == 1
    assert status.degraded_components[0]["name"] == "Console"


def test_parse_monitoring_incident_with_clean_indicator_warns() -> None:
    """Indicator='none' but active monitoring incident → not clean."""
    status = ps._parse_summary(_summary(incidents=[
        {"name": "Elevated errors", "status": "monitoring", "impact": "minor"},
    ]))
    assert status.is_clean is False
    assert status.has_warning is True


def test_parse_summary_captures_all_components_including_operational() -> None:
    status = ps._parse_summary(_summary(components=[
        {"name": "API", "status": "operational"},
        {"name": "Console", "status": "degraded_performance"},
    ]))
    names = {c["name"] for c in status.components}
    assert names == {"API", "Console"}


def test_parse_summary_drops_group_components() -> None:
    status = ps._parse_summary(_summary(components=[
        {"name": "API Group", "status": "operational", "group": True},
        {"name": "API - US", "status": "operational"},
    ]))
    names = {c["name"] for c in status.components}
    assert names == {"API - US"}
    assert "API Group" not in names


def test_parse_summary_degraded_components_regression() -> None:
    """Existing degraded_components filtering must be unaffected by adding
    the new `components` field alongside it."""
    status = ps._parse_summary(_summary(components=[
        {"name": "API", "status": "operational"},
        {"name": "Console", "status": "degraded_performance"},
        {"name": "Group", "status": "operational", "group": True},
    ]))
    assert len(status.degraded_components) == 1
    assert status.degraded_components[0]["name"] == "Console"


# ---------------------------------------------------------------------------
# Incident-day history — _reduce_incident_days + fetch_incident_days
# ---------------------------------------------------------------------------


def _incidents_payload(incidents: list[dict]) -> dict:
    return {"page": {"name": "Claude"}, "incidents": incidents}


def test_reduce_incident_days_single_day_incident() -> None:
    payload = _incidents_payload([
        {
            "name": "Blip",
            "impact": "minor",
            "created_at": "2026-07-07T10:00:00Z",
            "resolved_at": "2026-07-07T11:00:00Z",
        }
    ])
    days, history_days = ps._reduce_incident_days(payload)
    assert days == [{"date": "2026-07-07", "impact": "minor", "count": 1}]
    assert history_days == 1


def test_reduce_incident_days_multi_day_incident_marks_every_day() -> None:
    payload = _incidents_payload([
        {
            "name": "Long outage",
            "impact": "major",
            "created_at": "2026-07-07T22:00:00Z",
            "resolved_at": "2026-07-09T02:00:00Z",
        }
    ])
    days, history_days = ps._reduce_incident_days(payload)
    dates = [d["date"] for d in days]
    assert dates == ["2026-07-07", "2026-07-08", "2026-07-09"]
    assert all(d["impact"] == "major" for d in days)
    assert history_days == 3


def test_reduce_incident_days_worst_impact_wins_on_shared_day() -> None:
    payload = _incidents_payload([
        {
            "name": "Minor blip",
            "impact": "minor",
            "created_at": "2026-07-07T01:00:00Z",
            "resolved_at": "2026-07-07T02:00:00Z",
        },
        {
            "name": "Major outage",
            "impact": "major",
            "created_at": "2026-07-07T10:00:00Z",
            "resolved_at": "2026-07-07T12:00:00Z",
        },
    ])
    days, _ = ps._reduce_incident_days(payload)
    assert len(days) == 1
    assert days[0]["impact"] == "major"
    assert days[0]["count"] == 2


def test_reduce_incident_days_fills_clean_days_between_incidents() -> None:
    payload = _incidents_payload([
        {
            "name": "First",
            "impact": "minor",
            "created_at": "2026-07-01T00:00:00Z",
            "resolved_at": "2026-07-01T01:00:00Z",
        },
        {
            "name": "Second",
            "impact": "minor",
            "created_at": "2026-07-04T00:00:00Z",
            "resolved_at": "2026-07-04T01:00:00Z",
        },
    ])
    days, history_days = ps._reduce_incident_days(payload)
    assert history_days == 4
    by_date = {d["date"]: d for d in days}
    assert by_date["2026-07-02"]["impact"] == "none"
    assert by_date["2026-07-02"]["count"] == 0
    assert by_date["2026-07-03"]["impact"] == "none"


def test_reduce_incident_days_history_days_reflects_actual_span_not_constant() -> None:
    """history_days must be derived from the fetched data, never a hardcoded
    window like 90 — incidents.json itself only reaches back ~28 days."""
    payload = _incidents_payload([
        {
            "name": "Solo",
            "impact": "minor",
            "created_at": "2026-07-07T00:00:00Z",
            "resolved_at": "2026-07-07T01:00:00Z",
        }
    ])
    _, history_days = ps._reduce_incident_days(payload)
    assert history_days == 1
    assert history_days != 90


def test_reduce_incident_days_unresolved_incident_spans_through_now() -> None:
    now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
    payload = _incidents_payload([
        {
            "name": "Ongoing",
            "impact": "critical",
            "created_at": "2026-07-08T00:00:00Z",
            "resolved_at": None,
        }
    ])
    days, history_days = ps._reduce_incident_days(payload, now=now)
    dates = [d["date"] for d in days]
    assert dates == ["2026-07-08", "2026-07-09", "2026-07-10"]
    assert history_days == 3


def test_reduce_incident_days_empty_payload_returns_empty() -> None:
    days, history_days = ps._reduce_incident_days(_incidents_payload([]))
    assert days == []
    assert history_days == 0


def test_fetch_incident_days_success(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ps.INCIDENTS_PAGE_URL).respond(200, json=_incidents_payload([
        {
            "name": "Blip",
            "impact": "minor",
            "created_at": "2026-07-07T10:00:00Z",
            "resolved_at": "2026-07-07T11:00:00Z",
        }
    ]))
    days, history_days = ps.fetch_incident_days(timeout_s=1.0)
    assert history_days == 1
    assert days[0]["date"] == "2026-07-07"


def test_fetch_incident_days_timeout_returns_empty(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ps.INCIDENTS_PAGE_URL).mock(side_effect=httpx.TimeoutException("slow"))
    days, history_days = ps.fetch_incident_days(timeout_s=1.0)
    assert days == []
    assert history_days == 0


def test_fetch_incident_days_http_error_returns_empty(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ps.INCIDENTS_PAGE_URL).respond(500)
    days, history_days = ps.fetch_incident_days(timeout_s=1.0)
    assert days == []
    assert history_days == 0


def test_fetch_incident_days_malformed_json_returns_empty(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ps.INCIDENTS_PAGE_URL).respond(200, content=b"<html>")
    days, history_days = ps.fetch_incident_days(timeout_s=1.0)
    assert days == []
    assert history_days == 0


# ---------------------------------------------------------------------------
# fetch_platform_status — direct (used by doctor)
# ---------------------------------------------------------------------------


def test_fetch_returns_status_on_200(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ps.STATUS_PAGE_URL).respond(200, json=_summary())
    status = ps.fetch_platform_status(timeout_s=1.0)
    assert status.indicator == "none"
    assert status.is_clean


def test_fetch_raises_on_http_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ps.STATUS_PAGE_URL).respond(503)
    with pytest.raises(httpx.HTTPError):
        ps.fetch_platform_status(timeout_s=1.0)


def test_fetch_raises_on_invalid_json(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ps.STATUS_PAGE_URL).respond(200, content=b"<html>")
    with pytest.raises(ValueError):
        ps.fetch_platform_status(timeout_s=1.0)


# ---------------------------------------------------------------------------
# load_or_fetch — cache logic
# ---------------------------------------------------------------------------


def test_load_or_fetch_writes_cache_on_first_call(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    respx_mock.get(ps.STATUS_PAGE_URL).respond(200, json=_summary())
    result = ps.load_or_fetch(timeout_s=1.0)
    assert result is not None
    assert result.is_clean
    cache_file = _isolate / "platform-status.json"
    assert cache_file.is_file()
    payload = json.loads(cache_file.read_text())
    assert payload["indicator"] == "none"
    assert payload["schema_version"] == ps._SCHEMA_VERSION


def test_load_or_fetch_uses_fresh_cache_without_network(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    """With a fresh cache, no HTTP call should be made."""
    # Seed cache directly (recent timestamp).
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "indicator": "none",
        "description": "All Systems Operational",
        "active_incidents": [],
        "degraded_components": [],
    }))
    # No respx route — any HTTP call would raise.
    result = ps.load_or_fetch(timeout_s=1.0)
    assert result is not None
    assert result.indicator == "none"
    assert respx_mock.calls.call_count == 0


def test_load_or_fetch_refetches_when_cache_stale(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    """Cache older than ttl_s → fetch fresh."""
    cache_file = _isolate / "platform-status.json"
    old_ts = (datetime.now(UTC) - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "fetched_at": old_ts,
        "indicator": "none",
        "description": "Stale",
        "active_incidents": [],
        "degraded_components": [],
    }))
    respx_mock.get(ps.STATUS_PAGE_URL).respond(200, json=_summary(description="Fresh"))
    result = ps.load_or_fetch(ttl_s=60, timeout_s=1.0)
    assert result is not None
    assert result.description == "Fresh"


def test_load_or_fetch_falls_back_to_stale_when_fetch_fails(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    """If the fresh fetch fails AND we have stale cache, return the stale data
    rather than nothing — better than going dark during an actual incident."""
    cache_file = _isolate / "platform-status.json"
    old_ts = (datetime.now(UTC) - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "fetched_at": old_ts,
        "indicator": "minor",
        "description": "Partial Outage",
        "active_incidents": [{"name": "Stale incident", "status": "monitoring", "impact": "minor"}],
        "degraded_components": [],
    }))
    respx_mock.get(ps.STATUS_PAGE_URL).mock(side_effect=httpx.ConnectError("dead"))
    result = ps.load_or_fetch(ttl_s=60, timeout_s=1.0)
    assert result is not None
    assert result.description == "Partial Outage"  # stale-fallback served


def test_load_or_fetch_returns_unreachable_status_when_no_cache_and_fetch_fails(
    respx_mock: respx.MockRouter,
) -> None:
    """Cold start + network down → return a synthetic 'unreachable' status so
    callers can render *something* instead of pretending all is well."""
    respx_mock.get(ps.STATUS_PAGE_URL).mock(side_effect=httpx.ConnectError("dead"))
    result = ps.load_or_fetch(timeout_s=1.0)
    assert result is not None
    assert result.fetch_error is not None
    assert "ConnectError" in result.fetch_error
    assert result.is_clean is False  # has fetch_error → not clean
    assert result.has_warning


def test_load_or_fetch_force_refresh_bypasses_fresh_cache(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "indicator": "none",
        "description": "From cache",
        "active_incidents": [],
        "degraded_components": [],
    }))
    respx_mock.get(ps.STATUS_PAGE_URL).respond(
        200, json=_summary(description="From network")
    )
    result = ps.load_or_fetch(force_refresh=True, timeout_s=1.0)
    assert result is not None
    assert result.description == "From network"
    assert respx_mock.calls.call_count == 1


def test_load_or_fetch_ignores_cache_with_wrong_schema(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text(json.dumps({
        "schema_version": 999,
        "indicator": "none",
        "description": "junk",
    }))
    respx_mock.get(ps.STATUS_PAGE_URL).respond(200, json=_summary())
    result = ps.load_or_fetch(timeout_s=1.0)
    assert result is not None
    assert respx_mock.calls.call_count == 1  # bad schema → fetched anyway


def test_load_or_fetch_ignores_corrupt_cache(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text("{not json")
    respx_mock.get(ps.STATUS_PAGE_URL).respond(200, json=_summary())
    result = ps.load_or_fetch(timeout_s=1.0)
    assert result is not None
    assert respx_mock.calls.call_count == 1


def test_load_or_fetch_populates_incident_days_on_cache_miss(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    respx_mock.get(ps.STATUS_PAGE_URL).respond(200, json=_summary())
    respx_mock.get(ps.INCIDENTS_PAGE_URL).respond(200, json=_incidents_payload([
        {
            "name": "Blip",
            "impact": "minor",
            "created_at": "2026-07-07T10:00:00Z",
            "resolved_at": "2026-07-07T11:00:00Z",
        }
    ]))
    result = ps.load_or_fetch(timeout_s=1.0)
    assert result is not None
    assert result.history_days == 1
    assert result.incident_days[0]["date"] == "2026-07-07"
    cache_file = _isolate / "platform-status.json"
    payload = json.loads(cache_file.read_text())
    assert payload["history_days"] == 1
    assert payload["incident_days"][0]["date"] == "2026-07-07"


def test_load_or_fetch_incidents_failure_does_not_break_summary(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    """The summary fetch must still succeed even when incidents.json dies —
    the two fetches are independent best-effort calls (Rule 20)."""
    respx_mock.get(ps.STATUS_PAGE_URL).respond(200, json=_summary(description="Operational"))
    respx_mock.get(ps.INCIDENTS_PAGE_URL).respond(500)
    result = ps.load_or_fetch(timeout_s=1.0)
    assert result is not None
    assert result.description == "Operational"
    assert result.incident_days == []
    assert result.history_days == 0


def test_load_or_fetch_fresh_cache_hit_does_not_fetch_incidents(
    respx_mock: respx.MockRouter, _isolate: Path
) -> None:
    """Fresh-cache path stays a zero-network hot path — no incidents fetch
    either, so the history feature never slows down the common case."""
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "indicator": "none",
        "description": "All Systems Operational",
        "active_incidents": [],
        "degraded_components": [],
        "components": [],
        "incident_days": [{"date": "2026-07-07", "impact": "minor", "count": 1}],
        "history_days": 1,
    }))
    # No respx routes registered at all — any HTTP call would raise.
    result = ps.load_or_fetch(timeout_s=1.0)
    assert result is not None
    assert result.history_days == 1
    assert respx_mock.calls.call_count == 0


# ---------------------------------------------------------------------------
# format_status_line
# ---------------------------------------------------------------------------


def test_format_status_line_clean_returns_empty() -> None:
    status = ps._parse_summary(_summary())
    assert ps.format_status_line(status) == ""


def test_format_status_line_includes_incident_name() -> None:
    status = ps._parse_summary(_summary(incidents=[
        {"name": "Elevated errors on Claude Opus 4.7", "status": "monitoring", "impact": "minor"},
    ]))
    line = ps.format_status_line(status)
    assert "Elevated errors on Claude Opus 4.7" in line
    assert "monitoring" in line
    assert "minor" in line


def test_format_status_line_summarises_multiple_incidents() -> None:
    status = ps._parse_summary(_summary(incidents=[
        {"name": "First", "status": "investigating", "impact": "minor"},
        {"name": "Second", "status": "monitoring", "impact": "minor"},
        {"name": "Third", "status": "identified", "impact": "minor"},
    ]))
    line = ps.format_status_line(status)
    assert "First" in line
    assert "+2 more" in line


def test_format_status_line_renders_unreachable_state() -> None:
    status = ps.PlatformStatus(
        indicator="unknown",
        description="status.claude.com unreachable",
        fetch_error="ConnectError: dead",
    )
    line = ps.format_status_line(status)
    assert "unreachable" in line
    assert "ConnectError" in line


def test_format_status_line_marks_stale_cache() -> None:
    """Cached longer than the freshness window gets an age suffix — signals
    operators that they're looking at stale-fallback data."""
    status = ps._parse_summary(_summary(incidents=[
        {"name": "Old incident", "status": "monitoring", "impact": "minor"},
    ]))
    status.fetched_at = datetime.now(UTC) - timedelta(seconds=300)
    line = ps.format_status_line(status)
    assert "cached" in line
    assert "ago" in line


# ---------------------------------------------------------------------------
# to_json_meta
# ---------------------------------------------------------------------------


def test_to_json_meta_round_trips_fields() -> None:
    status = ps._parse_summary(_summary(incidents=[
        {"name": "X", "status": "monitoring", "impact": "minor"},
    ]))
    meta = ps.to_json_meta(status)
    assert meta["indicator"] == "none"
    assert meta["description"] == "All Systems Operational"
    assert meta["active_incidents"][0]["name"] == "X"
    assert isinstance(meta["age_seconds"], int)
    assert meta["fetched_at"].endswith("Z")
    assert meta["fetch_error"] is None


def test_to_json_meta_includes_components_and_history_fields() -> None:
    status = ps._parse_summary(_summary(components=[
        {"name": "API", "status": "operational"},
    ]))
    status.incident_days = [{"date": "2026-07-07", "impact": "minor", "count": 1}]
    status.history_days = 1
    meta = ps.to_json_meta(status)
    assert meta["components"][0]["name"] == "API"
    assert meta["incident_days"][0]["date"] == "2026-07-07"
    assert meta["history_days"] == 1


# ---------------------------------------------------------------------------
# Corrupt-cache field-mismatch variants — every guard branch in _read_cache
# ---------------------------------------------------------------------------


def test_read_cache_handles_old_shape_without_components_or_incident_days(
    _isolate: Path,
) -> None:
    """A cache file written by a pre-history version of this module has
    neither `components` nor `incident_days`/`history_days` — loading it must
    yield sane empty defaults, never a KeyError."""
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "indicator": "none",
        "description": "All Systems Operational",
        "active_incidents": [],
        "degraded_components": [],
    }))
    result = ps._read_cache()
    assert result is not None
    assert result.components == []
    assert result.incident_days == []
    assert result.history_days == 0


def test_read_cache_returns_none_when_fetched_at_field_missing(
    _isolate: Path,
) -> None:
    """A cache file with the right schema_version but no fetched_at field
    should be treated as unreadable (re-fetch path) rather than crashing."""
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "indicator": "none",
        "description": "missing fetched_at",
    }))
    assert ps._read_cache() is None


def test_read_cache_returns_none_when_fetched_at_is_not_string(
    _isolate: Path,
) -> None:
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "fetched_at": 12345,  # int, not iso string
        "indicator": "none",
        "description": "wrong type",
    }))
    assert ps._read_cache() is None


def test_read_cache_returns_none_when_fetched_at_is_unparseable(
    _isolate: Path,
) -> None:
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "fetched_at": "not-a-timestamp",
        "indicator": "none",
        "description": "junk ts",
    }))
    assert ps._read_cache() is None


def test_read_cache_handles_naive_fetched_at_by_assuming_utc(
    _isolate: Path,
) -> None:
    """A naive datetime string (no offset) should be assumed UTC, not crash."""
    cache_file = _isolate / "platform-status.json"
    cache_file.write_text(json.dumps({
        "schema_version": ps._SCHEMA_VERSION,
        "fetched_at": "2026-04-25T10:00:00",  # no Z, no offset
        "indicator": "none",
        "description": "All operational",
        "active_incidents": [],
        "degraded_components": [],
    }))
    result = ps._read_cache()
    assert result is not None
    assert result.fetched_at.tzinfo is not None  # UTC was inferred


# ---------------------------------------------------------------------------
# format_status_line — degraded-components-only path (no incidents)
# ---------------------------------------------------------------------------


def test_format_status_line_renders_degraded_components_alone() -> None:
    """No incidents but a degraded component should still render a header."""
    status = ps._parse_summary(_summary(components=[
        {"name": "Claude API", "status": "degraded_performance"},
    ]))
    line = ps.format_status_line(status)
    assert "Claude API" in line
    assert "degraded" in line


def test_format_status_line_truncates_many_degraded_components() -> None:
    status = ps._parse_summary(_summary(components=[
        {"name": f"Comp{i}", "status": "degraded_performance"}
        for i in range(5)
    ]))
    line = ps.format_status_line(status)
    assert "Comp0" in line
    assert "Comp1" in line
    assert "Comp2" in line
    # 4th and 5th truncated with " (+N more)"
    assert "+2 more" in line
