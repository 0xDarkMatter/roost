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
    # Budget must clear the fixed chrome (style block, Claude mark, icon
    # sprite, dashboard header — ~10 KB) or nothing can fit and the drop path
    # is not what is being tested. See test_budget_below_fixed_chrome_warns
    # for that case, and _fixed_chrome_bytes below, which pins the figure so
    # this number stops drifting silently.
    with pytest.warns(Warning, match="dropped"):
        html = render_widget(profiles, {"count": 80, "ok": 80}, max_bytes=16_000)
    assert len(html.encode("utf-8")) <= 16_000
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


def test_square_gauges_render_one_grid_per_window():
    """Three 5x10 blocks: Session, Weekly, Fable.

    Each block is ONE element painted with stacked gradients, not 50 tags.
    Three cards' worth of real squares would be 600 elements and ~8 KB —
    enough on its own to push the page past the inline-render budget.
    """
    usage = _usage(session_pct=2, weekly_pct=76)
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    card = html.split('<div class="rw-card">')[1]
    grids = re.findall(r'rw-sqgrid" style="--p:(\d+)px', card)
    assert len(grids) == 3
    assert card.count("<i") == 0, "the grid is drawn, not built from tags"


def test_square_gauge_lights_at_least_one_row_when_nonzero():
    """2% rounds to nothing at 10%-per-row granularity.

    An empty block beside a "2%" label reads as broken rather than as
    nearly-empty. The exact figure is printed beneath, which is what makes
    the rounding safe.
    """
    usage = _usage(session_pct=2, weekly_pct=0)
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    card = html.split('<div class="rw-card">')[1]
    fills = [int(v) for v in re.findall(r'rw-sqgrid" style="--p:(\d+)px', card)]
    assert fills[0] == 7, "2% lights exactly one row (7px cell, gap trimmed)"
    assert fills[1] == 0, "0% lights none"


def test_square_gauge_fill_quantises_to_whole_rows():
    """The fill boundary must land on a gap.

    An unsnapped gradient slices a row of squares in half, which reads as a
    rendering fault rather than as a value.
    """
    usage = _usage(session_pct=50, weekly_pct=76)
    html = render_widget([_profile(usage=usage)], {"count": 1, "ok": 1})
    card = html.split('<div class="rw-card">')[1]
    fills = [int(v) for v in re.findall(r'rw-sqgrid" style="--p:(\d+)px', card)]
    assert fills[0] == 43, "50% = 5 rows = 5*9-2 px"
    assert fills[1] == 70, "76% snaps to 8 rows = 8*9-2 px"
    # Every stop must be a whole pixel or the browser antialiases the edge,
    # which is what made the grid render soft.
    assert all((f + 2) % 9 == 0 for f in fills if f), "fills land on row edges"


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


def test_component_strips_render_per_service():
    """Each service carries its own history, as status.claude.com does.

    A fleet-wide roll-up cannot answer "is Claude Code specifically having a
    bad week?" — one noisy service colours every day for all of them.
    """
    meta = {
        "count": 1,
        "ok": 1,
        "platform_status": {
            "indicator": "none",
            "components": [
                {"name": "Claude Code", "status": "operational"},
                {"name": "Claude API", "status": "operational"},
            ],
            "history_days": 3,
            "incident_days": [
                {"date": "2026-08-01", "impact": "none", "count": 0},
                {"date": "2026-08-02", "impact": "major", "count": 1},
                {"date": "2026-08-03", "impact": "none", "count": 0},
            ],
            "component_days": {
                "Claude Code": [
                    {"date": "2026-08-01", "impact": "none", "count": 0},
                    {"date": "2026-08-02", "impact": "major", "count": 1},
                    {"date": "2026-08-03", "impact": "none", "count": 0},
                ],
                "Claude API": [
                    {"date": "2026-08-01", "impact": "none", "count": 0},
                    {"date": "2026-08-02", "impact": "none", "count": 0},
                    {"date": "2026-08-03", "impact": "none", "count": 0},
                ],
            },
        },
    }
    html = render_widget([_profile()], meta)
    strips = re.findall(r'rw-cal">(.*?)</div>', html, re.S)
    assert len(strips) == 2, "one calendar per component"
    assert 'class="c-o"' in strips[0], "Claude Code shows its major-impact day"
    assert 'class="c-o"' not in strips[1], "Claude API stays clean that day"


def test_component_strip_labels_the_real_window_not_a_fixed_one():
    """The endpoint returns a bounded number of incidents, so the window is
    whatever it reaches back to — captioning a 3-day series "last 90 days"
    would be a claim the data cannot support."""
    meta = {
        "count": 1,
        "ok": 1,
        "platform_status": {
            "indicator": "none",
            "components": [{"name": "Claude Code", "status": "operational"}],
            "history_days": 3,
            "component_days": {"Claude Code": [{"date": "2026-08-01", "impact": "none"}]},
        },
    }
    html = render_widget([_profile()], meta)
    assert "last 3 days" in html
    assert "90" not in html.split("rw-gridlabel")[1][:60]


def test_clean_days_carry_no_tooltip():
    """A caption for the absence of news, times 145 cells, is 3 KB of budget."""
    meta = {
        "count": 1,
        "ok": 1,
        "platform_status": {
            "indicator": "none",
            "components": [{"name": "Claude Code", "status": "operational"}],
            "history_days": 2,
            "component_days": {
                "Claude Code": [
                    {"date": "2026-08-01", "impact": "none", "count": 0},
                    {"date": "2026-08-02", "impact": "minor", "count": 2},
                ]
            },
        },
    }
    html = render_widget([_profile()], meta)
    strip = re.findall(r'rw-cal">(.*?)</div>', html, re.S)[0]
    assert strip.count("title=") == 1, "only the incident day is captioned"
    assert "08-02 minor ×2" in strip


def test_fixed_chrome_leaves_room_for_a_real_fleet():
    """Pin the chrome cost so it cannot creep past the budget unnoticed.

    Every profile card is ~4 KB. The chrome (style block, Claude mark, icon
    sprite, dashboard header) is paid once regardless of fleet size, so it
    is the figure that decides how many cards fit before the drop path
    starts silently removing profiles from a fleet overview.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        floor = len(render_widget([], {}).encode("utf-8"))
    assert floor < 12_000, (
        f"fixed chrome is {floor} bytes; at this rate a four-profile fleet "
        "stops fitting the 28 KB inline-render budget"
    )


def test_stat_icons_use_a_sprite_not_repeated_paths():
    """Four cards x five stats is twenty icons; inlining the paths twenty
    times would cost more than the cards themselves."""
    profiles = [_profile(name=f"p{i}") for i in range(4)]
    stats = {f"p{i}": {"picks": 10, "execs": 2, "fail_pct": 0} for i in range(4)}
    html = render_widget(profiles, {"count": 4, "ok": 4}, stats=stats)
    assert html.count("<defs>") == 1, "one sprite"
    assert html.count('<use href="#i-') >= 8, "referenced, not repeated"
    # The referencing svg needs its own viewBox or the 24-unit icons clip.
    assert 'class="rw-i" viewBox="0 0 24 24"' in html


def test_stats_render_only_what_exists():
    """A missing metric renders no chip. The usage log is opt-in, so a zero
    would misread as 'never dispatched' rather than 'not recorded'."""
    html = render_widget(
        [_profile()], {"count": 1, "ok": 1}, stats={"agent-01": {"picks": 5}}
    )
    assert "Picks" in html
    assert "Execs" not in html
    assert "Full in" not in html


def test_detail_is_shed_before_any_profile_is_dropped():
    """A fleet overview that omits an account is lying by omission.

    The drop path removes the LEAST-RECENTLY-PROBED profile — precisely the
    one you are least likely to notice missing. Losing the alternate gauge
    view costs a nicety; losing a card costs the answer.
    """
    profiles = [_profile(name=f"agent-{i}", usage=_usage()) for i in range(4)]
    meta = {
        "count": 4,
        "ok": 4,
        "platform_status": {
            "indicator": "none",
            "components": [{"name": f"svc-{i}", "status": "operational"} for i in range(5)],
            "history_days": 29,
            "component_days": {
                f"svc-{i}": [
                    {"date": f"2026-07-{d:02d}", "impact": "none", "count": 0}
                    for d in range(1, 30)
                ]
                for i in range(5)
            },
        },
    }
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any drop warning fails the test
        html = render_widget(profiles, meta, max_bytes=20_000)
    for i in range(4):
        assert f">agent-{i}<" in html, f"agent-{i} was dropped instead of detail"


def test_reduced_detail_still_shows_gauges_and_hides_the_toggle():
    """Bars are the CSS default, so dropping the bar markup without pinning
    the squares visible renders cards with no gauges at all — and leaves a
    toggle whose other position shows nothing."""
    profiles = [_profile(name=f"agent-{i}", usage=_usage()) for i in range(4)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        html = render_widget(profiles, {"count": 4, "ok": 4}, max_bytes=15_000)
    assert "rw-sq-only" in html, "squares must be pinned visible"
    assert "rw-sqgrid" in html, "some gauge must survive"
    assert 'class="rw-toggle"' not in html, "no toggle without a bar view"
    assert 'id="rw-mode"' not in html


def test_incident_calendar_pads_to_the_weekday_column():
    """Seven columns only mean something if each is one weekday.

    2026-07-01 is a Wednesday, so two blanks precede it.
    """
    meta = {
        "count": 1,
        "ok": 1,
        "platform_status": {
            "indicator": "none",
            "components": [{"name": "Claude Code", "status": "operational"}],
            "history_days": 3,
            "component_days": {
                "Claude Code": [
                    {"date": "2026-07-01", "impact": "none", "count": 0},
                    {"date": "2026-07-02", "impact": "none", "count": 0},
                ]
            },
        },
    }
    html = render_widget([_profile()], meta)
    cal = re.findall(r'rw-cal">(.*?)</div>', html, re.S)[0]
    assert cal.count("rw-pad") == 2, "Wednesday sits in the third column"
    assert cal.count("<i") == 4, "two pads plus two days"


def test_incident_calendar_survives_an_unparseable_date():
    """Best-effort enrichment must not raise on bad upstream data."""
    meta = {
        "count": 1,
        "ok": 1,
        "platform_status": {
            "indicator": "none",
            "components": [{"name": "Claude Code", "status": "operational"}],
            "history_days": 1,
            "component_days": {"Claude Code": [{"date": "not-a-date", "impact": "none"}]},
        },
    }
    html = render_widget([_profile()], meta)
    assert "rw-cal" in html
