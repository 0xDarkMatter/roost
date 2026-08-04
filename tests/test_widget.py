"""Tests for widget.render_widget — see widget.py's module docstring for the
CSP/byte-budget/decoupling constraints this renderer must honour."""

from __future__ import annotations

import re
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


def test_inactive_fable_limit_is_still_displayed():
    """`is_active: false` means "not the binding constraint", not "no data".

    Upstream marks exactly one limit active per profile. Skipping the
    inactive ones would render a profile whose Fable window is genuinely at
    0% as "—", which reads as "unknown" when it actually means "plenty
    left". Only the health classifier gates on `is_active`.
    """
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
    assert "99%" in html


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
    # Budget must clear the fixed chrome (style block, Claude mark, and
    # dashboard header — ~7 KB) or nothing can fit and the drop path is not
    # what is being tested — see test_budget_below_fixed_chrome_warns.
    with pytest.warns(Warning, match="dropped"):
        html = render_widget(profiles, {"count": 80, "ok": 80}, max_bytes=12_000)
    assert len(html.encode("utf-8")) <= 12_000
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


def test_reset_accepts_live_datetime_not_just_iso_string():
    """The CLI passes the payload in-process, where nested timestamps stay live.

    `build_status_payload` hand-formats the top-level reset fields to ISO
    strings but dumps nested `limits[]` wholesale, so a scoped limit's
    `resets_at` arrives as a real `datetime`. Every test here fed
    JSON-shaped strings, so an earlier `_parse_dt` that rejected non-strings
    stayed green while the rendered card silently dropped every model-scoped
    reset line. Assert both shapes.
    """
    reset = datetime.now(UTC) + timedelta(days=3)
    for value in (reset, _iso(reset)):
        usage = _usage(
            limits=[
                {
                    "kind": "weekly_scoped",
                    "group": "weekly",
                    "percent": 85,
                    "severity": "warning",
                    "resets_at": value,
                    "model": "Fable",
                    "surface": None,
                    "is_active": True,
                }
            ]
        )
        html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
        assert "Resets" in html, f"no reset line rendered for {type(value).__name__}"


def test_reset_uses_absolute_time_beyond_an_hour():
    """Relative offsets past an hour are unreadable — "in 144h" answers nothing."""
    usage = _usage(
        limits=[
            {
                "kind": "weekly_scoped",
                "percent": 50,
                "resets_at": datetime.now(UTC) + timedelta(days=3),
                "model": "Fable",
                "is_active": True,
            }
        ]
    )
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    assert re.search(r"Resets \w{3} \d{1,2}:\d{2} [AP]M", html)
    assert "in 72h" not in html


def test_theme_is_not_hardcoded_dark():
    """Cards must track the viewer's theme in both directions.

    An earlier build used dark values as `var()` fallbacks, so a fragment
    rendered outside the chat host (opened as a file) was locked dark on a
    light desktop.
    """
    html = render_widget([_profile()], {"count": 1, "ok": 1})
    assert "prefers-color-scheme:dark" in html.replace(" ", "")
    assert 'data-theme="light"' in html
    # Palette vars are scoped to the fragment, never leaked onto the host page.
    assert ":root{" not in html.replace(" ", "")


# ---------------------------------------------------------------------------
# Dashboard header
# ---------------------------------------------------------------------------


def test_dashboard_renders_title_and_counts():
    html = render_widget([_profile(), _profile(name="b")], {"count": 2, "ok": 2})
    assert "rw-head" in html
    assert "2 profiles" in html
    assert "2 ok" in html


def test_dashboard_renders_recommendation():
    html = render_widget(
        [_profile()],
        {"count": 1, "ok": 1},
        recommended={"name": "evolution7", "rationale": "least-used"},
    )
    assert "Recommended" in html
    assert "evolution7" in html
    assert "least-used" in html


def test_dashboard_reports_no_candidate_explicitly():
    """An explicit "none" beats an absent row — the operator asked."""
    html = render_widget(
        [_profile(health="auth_dead")],
        {"count": 1, "ok": 0},
        recommended={"reason": "All profiles auth-dead."},
    )
    assert "Recommended" in html
    assert "All profiles auth-dead." in html


def test_dashboard_omits_recommendation_when_not_supplied():
    html = render_widget([_profile()], {"count": 1, "ok": 1})
    assert "Recommended" not in html


def test_dashboard_ring_uses_the_binding_window_not_the_first():
    """The ring must show the WORST window, not whichever is read first.

    A profile at weekly 5% but Fable 90% has 10% of headroom, not 95%.
    Showing the wrong one would invert the whole point of the overview.
    """
    usage = _usage(
        session_pct=1,
        weekly_pct=5,
        limits=[
            {
                "kind": "weekly_scoped",
                "percent": 90,
                "model": "Fable",
                "is_active": True,
                "resets_at": _iso(datetime.now(UTC) + timedelta(days=2)),
            }
        ],
    )
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    head = html.split('<div class="rw-head">')[1].split('<div class="rw-grid">')[0]
    # Outer ring is Weekly, inner is Fable, and the legend names both so the
    # gap between them (5% vs 90%) is legible rather than implied.
    assert "W 5%" in head
    assert "F 90%" in head
    # The centre figure is the binding one, not the first one read.
    assert "90%" in head


def test_dashboard_ring_shows_state_not_percent_for_unhealthy():
    """A dead profile at 5% usage is not 95% available."""
    html = render_widget(
        [_profile(health="auth_dead", usage=_usage(weekly_pct=5))],
        {"count": 1, "ok": 0},
    )
    head = html.split('<div class="rw-head">')[1].split('<div class="rw-grid">')[0]
    assert "5%" not in head
    # The aria-label must say the same thing the ring does — announcing
    # "weekly 5%" would tell a screen-reader user it has 95% of headroom.
    assert "auth dead" in head


def test_dashboard_ring_handles_null_usage():
    html = render_widget([_profile(usage=None)], {"count": 1, "ok": 1})
    assert "rw-ringbox" in html


def test_dashboard_shows_platform_status():
    meta = {
        "count": 1,
        "ok": 1,
        "platform_status": {"indicator": "none", "description": "All Systems Operational"},
    }
    html = render_widget([_profile()], meta)
    assert "Anthropic: All Systems Operational" in html


def test_dashboard_rings_are_pure_svg_no_external_refs():
    html = render_widget(
        [_profile(name=f"p{i}") for i in range(4)], {"count": 4, "ok": 4}
    )
    # Count the markup, not the string — the CSS carries a .rw-ringbox rule.
    assert html.count('<div class="rw-ringbox">') == 4
    assert "<svg" in html
    assert "http" not in html


def test_budget_below_fixed_chrome_warns_instead_of_lying():
    """Dropping profiles cannot shrink the style block or the header.

    Silently returning an over-budget page is the failure the budget exists
    to prevent: the caller only finds out when show_widget declines to
    render it inline.
    """
    with pytest.warns(Warning, match="cannot be reduced"):
        html = render_widget([], {"count": 0, "ok": 0}, max_bytes=1)
    assert len(html.encode("utf-8")) > 1


def test_long_incident_description_is_bounded():
    """The one unbounded field in the payload cannot blow the budget."""
    meta = {
        "count": 1,
        "ok": 1,
        "platform_status": {"indicator": "major", "description": "x" * 30_000},
    }
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        html = render_widget([_profile()], meta)
    assert len(html.encode("utf-8")) < 28_672


# ---------------------------------------------------------------------------
# Square-grid gauges + view toggle
# ---------------------------------------------------------------------------


def test_square_gauges_render_three_columns_of_ten():
    usage = _usage(session_pct=2, weekly_pct=76)
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    card = html.split('<div class="rw-card">')[1]
    stacks = re.findall(r'rw-sqstack">(.*?)</div>', card, re.S)
    assert len(stacks) == 3, "one stack per window: Session, Weekly, Fable"
    for stack in stacks:
        assert stack.count("<i") == 10


def test_square_gauge_lights_at_least_one_square_when_nonzero():
    """2% rounds to nothing at 10%-per-square granularity.

    An empty column beside a "2%" label reads as broken rather than as
    nearly-empty, so a non-zero percent always lights one. The exact figure
    is printed beneath, which is what makes the rounding safe.
    """
    usage = _usage(session_pct=2, weekly_pct=0)
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    card = html.split('<div class="rw-card">')[1]
    stacks = re.findall(r'rw-sqstack">(.*?)</div>', card, re.S)
    assert stacks[0].count('<i class=') == 1, "2% lights exactly one square"
    assert stacks[1].count('<i class=') == 0, "0% lights none"


def test_square_gauge_fill_tracks_percentage():
    usage = _usage(session_pct=50, weekly_pct=100)
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    card = html.split('<div class="rw-card">')[1]
    stacks = re.findall(r'rw-sqstack">(.*?)</div>', card, re.S)
    assert stacks[0].count('<i class=') == 5
    assert stacks[1].count('<i class=') == 10


def test_view_toggle_is_css_only_no_script():
    """The toggle must not reintroduce JavaScript.

    The page's no-<script> guarantee is what makes "cannot reach for
    alert/confirm/prompt" structurally true rather than a convention. A
    hidden checkbox plus a sibling selector buys interactivity without it.
    """
    html = render_widget([_profile()], {"count": 1, "ok": 1})
    assert "<script" not in html.lower()
    assert 'class="rw-modechk"' in html
    assert 'for="rw-mode"' in html
    # The checkbox must precede what it restyles — CSS only selects forward.
    assert html.index("rw-modechk") < html.index('<div class="rw-head">')
    assert html.index("rw-modechk") < html.index('<div class="rw-grid">')


def test_both_gauge_views_are_present_for_the_toggle():
    html = render_widget([_profile(usage=_usage())], {"count": 1, "ok": 1})
    assert "rw-bars" in html
    assert "rw-squares" in html


def test_component_rows_omit_operational_text():
    """Repeating "operational" down a healthy column buries the one row
    that would matter. The dot carries the state; only an abnormal one
    earns words."""
    meta = {
        "count": 1,
        "ok": 1,
        "platform_status": {
            "indicator": "none",
            "components": [
                {"name": "Claude Code", "status": "operational"},
                {"name": "Claude API (api.anthropic.com)", "status": "degraded_performance"},
            ],
        },
    }
    html = render_widget([_profile()], meta)
    head = html.split('<div class="rw-head">')[1].split('<div class="rw-grid">')[0]
    visible = re.sub(r'title="[^"]*"', "", head)
    assert "operational" not in visible, "healthy rows say nothing; the dot carries it"
    assert "degraded performance" in visible, "an abnormal state still earns words"
    # The state stays available to assistive tech and on hover.
    assert 'title="Claude Code: operational"' in head


def test_component_names_drop_parenthetical_hostnames():
    meta = {
        "count": 1,
        "ok": 1,
        "platform_status": {
            "indicator": "none",
            "components": [{"name": "Claude Console (platform.claude.com)", "status": "operational"}],
        },
    }
    html = render_widget([_profile()], meta)
    assert ">Claude Console<" in html
    assert "platform.cl" not in html.split("title=")[0]
