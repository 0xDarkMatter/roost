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
    the user's real ~/.config/claude-lb. Also mirrors the override into the
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
