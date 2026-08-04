"""Tests for widget.render_widget — see widget.py's module docstring for the
CSP/byte-budget/decoupling constraints this renderer must honour."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta

import pytest

from claude_lb.widget import render_widget

FORBIDDEN_TAGS = ("<!doctype", "<html", "<head", "<body")


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _profile(
    name: str = "agent-01",
    health: str = "ok",
    *,
    subscription_type: str | None = "max",
    probed_at: str | None = None,
    usage: dict | None = None,
    session_reset_at: str | None = None,
    weekly_reset_at: str | None = None,
) -> dict:
    now = datetime.now(UTC)
    return {
        "name": name,
        "health": health,
        "subscription_type": subscription_type,
        "probed_at": probed_at or _iso(now - timedelta(minutes=2)),
        "usage": usage,
        "session_reset_at": session_reset_at,
        "weekly_reset_at": weekly_reset_at or _iso(now + timedelta(hours=6)),
        "probe_latency_ms": 400,
    }


def _usage(**overrides) -> dict:
    base = {
        "session_pct": 12,
        "weekly_pct": 34,
        "sonnet_pct": None,
        "opus_pct": None,
        "limits": [],
        "spend": {
            "used_minor": 0,
            "currency": "USD",
            "exponent": 2,
            "percent": 0,
            "severity": "normal",
            "enabled": False,
        },
        "extra": {
            "is_enabled": False,
            "monthly_limit": None,
            "used_credits": None,
            "utilization": None,
            "currency": None,
        },
    }
    base.update(overrides)
    return base


def _assert_no_forbidden_tags(html: str) -> None:
    lowered = html.lower()
    for tag in FORBIDDEN_TAGS:
        assert tag not in lowered


def test_empty_fleet_renders_without_raising():
    html = render_widget([], {})
    _assert_no_forbidden_tags(html)
    assert "no profiles" in html.lower()


def test_four_health_states_show_state_word_not_just_colour():
    for health in ("ok", "rate_limited", "weekly_limit", "network_error"):
        profiles = [_profile(health=health)]
        html = render_widget(profiles, {"count": 1, "ok": 1 if health == "ok" else 0})
        assert health in html


def test_null_usage_renders_placeholders_without_traceback():
    profiles = [_profile(usage=None)]
    html = render_widget(profiles, {"count": 1, "ok": 1})
    _assert_no_forbidden_tags(html)
    assert "—" in html


def test_fable_pct_extracted_from_active_limit():
    usage = _usage(
        limits=[
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 42,
                "severity": "normal",
                "resets_at": _iso(datetime.now(UTC) + timedelta(hours=3)),
                "model": "Fable",
                "surface": None,
                "is_active": True,
            }
        ]
    )
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    assert "42%" in html


def test_fable_pct_missing_limits_renders_dash():
    usage = _usage(limits=[])
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    assert "Fable" in html
    # The Fable gauge row has no numeric percent anywhere for this profile.
    assert "42%" not in html


def test_fable_pct_absent_when_limits_key_missing_entirely():
    usage = _usage()
    del usage["limits"]
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    _assert_no_forbidden_tags(html)
    assert "Fable" in html


def test_inactive_fable_limit_is_ignored():
    usage = _usage(
        limits=[
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 99,
                "severity": "critical",
                "resets_at": _iso(datetime.now(UTC) + timedelta(hours=1)),
                "model": "Fable",
                "surface": None,
                "is_active": False,
            }
        ]
    )
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    assert "99%" not in html


def test_no_external_dependencies():
    profiles = [_profile() for _ in range(3)]
    html = render_widget(profiles, {"count": 3, "ok": 3})
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src" not in html.lower()
    assert '<link rel="stylesheet"' not in html.lower()
    assert "<link rel='stylesheet'" not in html.lower()


def test_no_arial_and_no_native_dialogs():
    profiles = [_profile()]
    html = render_widget(profiles, {"count": 1, "ok": 1})
    assert "Arial" not in html
    assert "alert(" not in html
    assert "confirm(" not in html
    assert "prompt(" not in html


def test_twelve_profile_fleet_stays_under_budget():
    profiles = [_profile(name=f"agent-{i:02d}") for i in range(12)]
    html = render_widget(profiles, {"count": 12, "ok": 12})
    assert len(html.encode("utf-8")) < 28_672
    _assert_no_forbidden_tags(html)


def test_oversized_fleet_drops_profiles_and_warns():
    now = datetime.now(UTC)
    profiles = [
        _profile(
            name=f"agent-with-a-fairly-long-profile-name-{i:03d}",
            probed_at=_iso(now - timedelta(minutes=i)),
            usage=_usage(),
        )
        for i in range(80)
    ]
    with pytest.warns(Warning, match="dropped"):
        html = render_widget(profiles, {"count": 80, "ok": 80}, max_bytes=4_000)
    assert len(html.encode("utf-8")) <= 4_000
    # The most-recently-probed profile (agent-with-...-000) must survive the drop.
    assert "agent-with-a-fairly-long-profile-name-000" in html


def test_no_warning_when_under_budget():
    profiles = [_profile()]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        render_widget(profiles, {"count": 1, "ok": 1})


def test_no_forbidden_wrapper_tags_multi_profile():
    profiles = [_profile(health=h) for h in ("ok", "session_limit", "auth_dead", "unknown")]
    html = render_widget(profiles, {"count": 4, "ok": 1})
    _assert_no_forbidden_tags(html)


def test_summary_header_reports_totals_and_incident():
    profiles = [_profile(name="a"), _profile(name="b", health="weekly_limit")]
    meta = {
        "count": 2,
        "ok": 1,
        "platform_status": {"indicator": "major", "description": "Elevated error rates"},
    }
    html = render_widget(profiles, meta)
    assert "2 profile" in html
    assert "1 ok" in html
    assert "Elevated error rates" in html


def test_summary_header_omits_incident_line_when_operational():
    profiles = [_profile()]
    meta = {"count": 1, "ok": 1, "platform_status": {"indicator": "none"}}
    html = render_widget(profiles, meta)
    assert '<span class="rw-incident"' not in html
