"""Renders `roost widget` output: a self-contained HTML capacity-card page.

The page is handed to Claude Code's `show_widget` tool, which renders it
inline in a chat pane behind a strict CSP — no CDN, no webfont, no
`fetch`/XHR, no `<script src>`/`<link>`. Everything here is one inline
`<style>` block plus plain markup; there is deliberately no `<script>` at
all (nothing here needs interactivity, so it can't accidentally reach for
`alert`/`confirm`/`prompt`, which are banned outright).

`show_widget` payloads that grow too large spool to a file instead of
rendering inline, so `render_widget` enforces a hard byte budget
(`max_bytes`, default 28 KiB) by dropping the least-recently-probed
profiles until the rendered page fits.

`render_widget` takes plain dicts shaped like `roost status --json`'s
`data[]`/`meta`, not `models.ProfileHealth` — the `limits[]`/`spend` usage
fields it reads are being added by a concurrent lane and may not exist on
every input. Do not "clean this up" into a Pydantic import: that would
recouple the widget to the probe layer and break the moment the shapes
drift, which is exactly the coupling this module exists to avoid.

`warnings.warn` (not `print`/`sys.stderr.write`) is the drop notice: it
keeps the function's only observable *return* effect pure and
deterministic while still surfacing on stderr by default, and lets
callers/tests assert on it with `pytest.warns` instead of scraping text.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any

# Palette and metrics are lifted verbatim from fleetflow's ff-monitor.html so
# roost's cards read as the same family as the fleet monitor and the summon
# session picker. Deliberately NOT the chat host's design tokens: this is a
# technical/instrument register (8px corners, 1px rules, square pips,
# monospace figures), and host tokens would pull it toward the softer
# editorial look of the surrounding page. Change it here and in
# ff-monitor.html together.
#
# Labels are Title case, not the uppercase+letter-spacing ff-monitor uses for
# its micro-labels — an explicit call from the repo owner. Keep it that way.
#
# Scoped to `.rw` rather than `:root` because this renders as a FRAGMENT inside
# a host page — writing `:root` would leak roost's palette onto everything else
# on it. The prefers-color-scheme block plus the data-theme overrides mean the
# cards track light/dark both standalone (opened as a file) and inside the chat
# host's own theme toggle. An earlier build hardcoded dark values as var()
# fallbacks, which rendered every card dark on a light desktop.
_INCIDENT_MAX_CHARS = 160

# Anthropic's Claude mark, as an inline path. Same asset fleetflow's dashboard
# uses for its provider marks — carried here rather than fetched because the
# show_widget CSP blocks every outbound request, so a remote logo would render
# as nothing. `currentColor` lets it inherit the surrounding text colour and
# therefore track light/dark with everything else.
_CLAUDE_MARK = (
    '<svg class="rw-mark" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">'
    '<path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486'
    "-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797"
    "-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579"
    "-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643"
    "-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893"
    " 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893"
    "-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0"
    "l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318"
    ".091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107"
    ".7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429"
    "h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321"
    "l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093"
    ".1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075"
    "-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621"
    "l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107"
    "-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768"
    "-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496"
    " 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959"
    "l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468"
    ".3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182"
    "-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008"
    "-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164"
    '-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/></svg>'
)

# Statuspage component/incident vocabulary -> colour. Ranked so "worst wins"
# when several incidents share a day.
_IMPACT_RANK = {"none": 0, "maintenance": 1, "minor": 2, "major": 3, "critical": 4}
_IMPACT_COLORS = {
    "none": "#1D9E75",
    "maintenance": "#8a8a86",
    "minor": "#E0A233",
    "major": "#BA7517",
    "critical": "#E24B4A",
}
_COMPONENT_COLORS = {
    "operational": "#1D9E75",
    "degraded_performance": "#E0A233",
    "partial_outage": "#BA7517",
    "major_outage": "#E24B4A",
    "under_maintenance": "#8a8a86",
}

_STYLE = (
    "<style>"
    ".rw{color-scheme:light dark;"
    "--rw-card:#fff;--rw-border:#e4e2dd;--rw-text:#1a1a17;--rw-muted:#8a887f;"
    "--rw-track:#efeee9;"
    "--rw-ok:#1d9e75;--rw-bad:#e24b4a;--rw-warn:#c8871b;--rw-idle:#d3d1c7;"
    "font:12px/1.45 \"Segoe UI\",\"Helvetica Neue\",ui-sans-serif,sans-serif;"
    "color:var(--rw-text)}"
    "@media (prefers-color-scheme:dark){.rw{"
    "--rw-card:#262624;--rw-border:#3a3936;--rw-text:#ececea;--rw-muted:#8f8d85;"
    "--rw-track:#1c1c1a;--rw-warn:#e0a233;--rw-idle:#444441}}"
    ":root[data-theme=\"dark\"] .rw{"
    "--rw-card:#262624;--rw-border:#3a3936;--rw-text:#ececea;--rw-muted:#8f8d85;"
    "--rw-track:#1c1c1a;--rw-warn:#e0a233;--rw-idle:#444441}"
    ":root[data-theme=\"light\"] .rw{"
    "--rw-card:#fff;--rw-border:#e4e2dd;--rw-text:#1a1a17;--rw-muted:#8a887f;"
    "--rw-track:#efeee9;--rw-warn:#c8871b;--rw-idle:#d3d1c7}"
    ".rw *{box-sizing:border-box}"
    ".rw-mono{font-family:ui-monospace,\"Cascadia Code\",Consolas,monospace}"
    ".rw-summary{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;"
    "margin:0 0 10px;font-size:11px;color:var(--rw-muted);letter-spacing:.02em}"
    ".rw-incident{width:100%;color:var(--rw-warn);letter-spacing:0;font-size:11px}"
    ".rw-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));"
    "gap:8px}"
    ".rw-empty{font-size:11px;color:var(--rw-muted);padding:1rem 0}"
    ".rw-card{background:var(--rw-card);border:1px solid var(--rw-border);"
    "border-radius:8px;padding:8px 10px;display:flex;flex-direction:column;"
    "gap:7px;min-width:0}"
    ".rw-hd{display:flex;align-items:center;gap:7px;flex-wrap:wrap;row-gap:4px}"
    ".rw-pip{width:8px;height:8px;border-radius:1.5px;flex:none}"
    ".rw-name{font-size:12px;font-weight:600;overflow-wrap:anywhere}"
    ".rw-state{font-size:11px;color:var(--rw-muted);letter-spacing:.02em}"
    ".rw-tag{font-size:11px;color:var(--rw-muted);letter-spacing:.02em;"
    "margin-left:auto}"
    ".rw-gauges{display:flex;flex-direction:column;gap:5px}"
    ".rw-glabel{display:flex;justify-content:space-between;align-items:baseline;"
    "gap:8px;font-size:11px;color:var(--rw-muted);letter-spacing:.02em}"
    ".rw-val{font-size:11px;font-weight:500;letter-spacing:0}"
    ".rw-gtrack{height:4px;border-radius:1px;background:var(--rw-track);"
    "overflow:hidden;margin-top:2px}"
    ".rw-gfill{height:100%;border-radius:1px}"
    ".rw-reset{font-size:10px;color:var(--rw-muted);margin-top:1px}"
    ".rw-foot{border-top:1px solid var(--rw-border);margin-top:1px;padding-top:5px;"
    "font-size:10px;color:var(--rw-muted)}"
    # Dashboard header — the fleet-level summary that sits above the grid.
    # Same layout doctrine as fleetflow's ff-monitor: pinned summary, cards
    # beneath.
    ".rw-head{background:var(--rw-card);border:1px solid var(--rw-border);"
    "border-radius:8px;padding:10px 12px;margin:0 0 8px;display:flex;"
    "flex-direction:column;gap:8px}"
    ".rw-head-row{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}"
    ".rw-title{font-size:13px;font-weight:600;letter-spacing:.01em}"
    ".rw-counts{font-size:11px;color:var(--rw-muted)}"
    ".rw-status{font-size:11px;color:var(--rw-muted);margin-left:auto}"
    ".rw-pick{font-size:11px;color:var(--rw-muted)}"
    ".rw-pick b{color:var(--rw-text);font-weight:600}"
    ".rw-mark{width:15px;height:15px;flex:none;color:#D97757}"
    # Two invisible sections: profiles centred on the left, Anthropic's own
    # status on the right. No rule between them — the gap does the separating.
    ".rw-cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,auto);"
    "gap:12px 24px;align-items:center}"
    "@media (max-width:560px){.rw-cols{grid-template-columns:minmax(0,1fr)}"
    ".rw-col-r{align-items:flex-start}}"
    ".rw-col-l{display:flex;justify-content:center}"
    ".rw-col-r{display:flex;flex-direction:column;gap:2px;min-width:190px}"
    ".rw-rings{display:flex;flex-wrap:wrap;justify-content:center;gap:14px;"
    "padding-top:2px}"
    ".rw-ringbox{display:flex;flex-direction:column;align-items:center;gap:2px;"
    "min-width:56px}"
    ".rw-ring{width:46px;height:46px;display:block}"
    ".rw-ringname{font-size:10px;color:var(--rw-muted);max-width:76px;"
    "overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".rw-legrow{display:flex;gap:6px}"
    ".rw-leg{font-size:9px;font-weight:500;"
    "font-family:ui-monospace,\"Cascadia Code\",Consolas,monospace}"
    ".rw-ringwin{font-size:9px;color:var(--rw-muted)}"
    # Component rows + incident grid (right column).
    ".rw-comp{display:flex;align-items:center;gap:6px;font-size:10px;"
    "color:var(--rw-muted);line-height:1.5}"
    ".rw-cdot{width:6px;height:6px;border-radius:1.5px;flex:none}"
    ".rw-cname{color:var(--rw-text);white-space:nowrap;overflow:hidden;"
    "text-overflow:ellipsis}"
    ".rw-cstate{margin-left:auto;white-space:nowrap}"
    ".rw-gridlabel{font-size:9px;color:var(--rw-muted);margin-top:6px}"
    ".rw-daygrid{display:grid;grid-template-columns:repeat(20,1fr);gap:2px;"
    "margin-top:3px;max-width:200px}"
    ".rw-day{display:block;width:100%;aspect-ratio:1;border-radius:1.5px;"
    "min-height:6px}"
    # Per-card stat chips + trend strip, in the ff-monitor idiom: a compact
    # metric row and a bar strip rather than prose.
    ".rw-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;"
    "font-size:10px;color:var(--rw-muted);border-top:1px solid var(--rw-border);"
    "padding-top:6px;margin-top:1px}"
    ".rw-stats b{display:block;color:var(--rw-text);font-weight:500;"
    "font-family:ui-monospace,\"Cascadia Code\",Consolas,monospace;font-size:11px}"
    ".rw-spark{display:flex;align-items:flex-end;gap:1px;height:16px;"
    "margin-top:5px}"
    ".rw-spark i{flex:1;min-width:1px;border-radius:1px;display:block;"
    "background:var(--rw-idle)}"
    "</style>"
)

# Severity buckets: green under 60, amber 60-84, red 85+ (gauges).
_SEVERITY_GREEN = "#1D9E75"
_SEVERITY_AMBER = "#BA7517"
_SEVERITY_RED = "#E24B4A"
_SEVERITY_GREY = "#8a8a86"

# Health-state -> pip/text colour. Amber for the states that clear on their
# own once a window resets, red for the ones needing intervention. Unknown
# values fall back to grey rather than raising — the taxonomy can gain a
# state without this renderer being updated in the same commit.
_HEALTH_COLORS = {
    "ok": _SEVERITY_GREEN,
    "rate_limited": _SEVERITY_AMBER,
    "session_limit": _SEVERITY_AMBER,
    "model_limit": _SEVERITY_AMBER,
    "weekly_limit": _SEVERITY_RED,
    "auth_dead": _SEVERITY_RED,
    "auth_expired": _SEVERITY_RED,
    "network_error": _SEVERITY_GREY,
    "unknown": _SEVERITY_GREY,
}


def render_widget(
    profiles: list[dict[str, Any]],
    meta: dict[str, Any],
    *,
    max_bytes: int = 28_672,
    recommended: dict[str, Any] | None = None,
    stats: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Render the capacity-card page for `roost widget`.

    `profiles` mirrors `status --json`'s `data[]`; `meta` mirrors its
    `meta` object (used for the dashboard header's totals and any
    platform-status incident line). Both are read defensively via
    `.get(...)` — see the module docstring for why.

    `recommended` is what `pick` WOULD return: `{"name", "rationale"}`, or
    `{"reason"}` when nothing is selectable. Pass `None` to omit the row
    entirely. It is supplied by the caller rather than computed here
    precisely so this renderer stays free of pick's machinery — and so the
    caller is the one accountable for using read-only `which` semantics
    (AGENTS.md rule 21). Rendering a dashboard must never move the
    stickiness pointer or append to picks.log.

    Drops the least-recently-probed profiles (oldest `probed_at` first)
    until the page fits `max_bytes`, warning via `warnings.warn` if it had
    to drop anything.
    """
    meta = meta if isinstance(meta, dict) else {}
    working = [p for p in (profiles or []) if isinstance(p, dict)]
    original_count = len(working)

    html = _render(working, meta, recommended, stats)
    while len(html.encode("utf-8")) > max_bytes and working:
        working = sorted(working, key=_probed_sort_key)
        working.pop(0)
        html = _render(working, meta, recommended, stats)

    dropped = original_count - len(working)
    if dropped:
        warnings.warn(
            f"roost widget: dropped {dropped} least-recently-probed profile(s) "
            f"to stay under the {max_bytes}-byte show_widget inline-render budget",
            stacklevel=2,
        )
    # Dropping profiles cannot shrink the fixed chrome (the style block and
    # the summary header). With a small enough budget, or a pathological
    # incident description, the page can still overshoot after the last
    # profile is gone — and silently returning an over-budget page is exactly
    # the failure the budget exists to prevent, because the caller only finds
    # out when show_widget declines to render it inline. Say so instead.
    size = len(html.encode("utf-8"))
    if size > max_bytes:
        warnings.warn(
            f"roost widget: page is {size} bytes, over the {max_bytes}-byte "
            "budget, and cannot be reduced further — the fixed chrome alone "
            "exceeds it. show_widget may refuse to render this inline.",
            stacklevel=2,
        )
    return html


def _render(
    profiles: list[dict[str, Any]],
    meta: dict[str, Any],
    recommended: dict[str, Any] | None = None,
    stats: dict[str, dict[str, Any]] | None = None,
) -> str:
    now = datetime.now(UTC)
    cards = "".join(
        _render_card(p, now, (stats or {}).get(str(p.get("name")))) for p in profiles
    )
    if cards:
        grid = f'<div class="rw-grid">{cards}</div>'
    else:
        grid = '<div class="rw-empty">No profiles discovered — run <code>roost probe</code> first.</div>'
    head = _render_dashboard(profiles, meta, recommended)
    # Everything lives inside .rw so the palette custom properties stay scoped
    # to this fragment instead of leaking onto the host page.
    return f'{_STYLE}<div class="rw">{head}{grid}</div>'



_RING_OUTER_R = 15.9155  # circumference 100 -> dasharray maps 1:1 to percent
_RING_INNER_R = 10.5


def _arc(radius: float, pct: float, colour: str, width: float) -> str:
    """One stroked arc. Dash length is scaled to the radius's circumference."""
    circumference = 2 * 3.141592653589793 * radius
    dash = circumference * max(0.0, min(100.0, pct)) / 100.0
    return (
        f'<circle cx="18" cy="18" r="{radius:g}" fill="none" stroke="{colour}" '
        f'stroke-width="{width:g}" stroke-linecap="round" '
        f'stroke-dasharray="{dash:.3f} {circumference - dash:.3f}" '
        'transform="rotate(-90 18 18)"/>'
    )


def _ring(profile: dict[str, Any]) -> str:
    """Concentric gauges for one profile: outer = Weekly, inner = Fable.

    Two rings rather than one because the two windows exhaust independently
    and the gap between them is the whole point — a profile at Weekly 76%
    with Fable at 90% is gated by Fable, and a single ring showing either
    number alone hides that.

    Pure SVG. No canvas, no library, nothing needing a network fetch the
    show_widget CSP would block.
    """
    name = str(profile.get("name") or "unknown")
    health = str(profile.get("health") or "unknown")
    usage = profile.get("usage")
    usage = usage if isinstance(usage, dict) else None
    weekly = _as_number(usage.get("weekly_pct")) if usage else None
    fable_limit = _active_fable_limit(usage)
    fable = _as_number(fable_limit.get("percent")) if fable_limit else None

    arcs = [
        '<circle cx="18" cy="18" r="15.9155" fill="none" '
        'stroke="var(--rw-track)" stroke-width="3"/>',
        '<circle cx="18" cy="18" r="10.5" fill="none" '
        'stroke="var(--rw-track)" stroke-width="2.4"/>',
    ]
    # An unhealthy profile reads as its state, not a percentage — a dead
    # profile at 5% usage is not 95% available.
    if health not in ("ok", "model_limit"):
        colour = _HEALTH_COLORS.get(health, _SEVERITY_GREY)
        arcs.append(_arc(_RING_OUTER_R, 100.0, colour, 3))
        centre = "!"
        centre_colour = colour
        # The screen-reader label must say the same thing the ring does. An
        # aria-label quoting "weekly 5%" for a dead profile would tell a
        # non-sighted user it has 95% of headroom, which is exactly the
        # misreading the visual treatment exists to prevent.
        aria = f"{name}: {health.replace('_', ' ')}"
    else:
        if weekly is not None:
            arcs.append(_arc(_RING_OUTER_R, weekly, _severity_color(weekly), 3))
        if fable is not None:
            arcs.append(_arc(_RING_INNER_R, fable, _severity_color(fable), 2.4))
        binding = max([p for p in (weekly, fable) if p is not None], default=None)
        centre = "—" if binding is None else f"{_fmt_pct(binding)}"
        centre_colour = _SEVERITY_GREY if binding is None else _severity_color(binding)
        aria = f"{name}: weekly {_fmt_pct(weekly)}, Fable {_fmt_pct(fable)}"

    if health not in ("ok", "model_limit"):
        # Same reason the centre shows "!" and the aria-label names the state:
        # a percentage next to a dead profile reads as available headroom.
        colour = _HEALTH_COLORS.get(health, _SEVERITY_GREY)
        legend = (
            f'<span class="rw-leg" style="color:{colour}">'
            f'{_esc(health.replace("_", " "))}</span>'
        )
    else:
        legend = (
            f'<span class="rw-leg" style="color:{_pct_colour(weekly)}">'
            f"W {_fmt_pct(weekly)}</span>"
            f'<span class="rw-leg" style="color:{_pct_colour(fable)}">'
            f"F {_fmt_pct(fable)}</span>"
        )
    return (
        '<div class="rw-ringbox">'
        f'<svg class="rw-ring" viewBox="0 0 36 36" role="img" '
        f'aria-label="{_esc(aria)}">'
        f"{''.join(arcs)}"
        f'<text x="18" y="20.6" text-anchor="middle" font-size="8.5" '
        f'fill="{centre_colour}" font-family="ui-monospace,Consolas,monospace">'
        f"{_esc(centre)}</text>"
        "</svg>"
        f'<span class="rw-ringname" title="{_esc(name)}">{_esc(name)}</span>'
        f'<span class="rw-legrow">{legend}</span>'
        "</div>"
    )


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "—"
    return f"{int(pct) if float(pct).is_integer() else round(pct, 1)}%"


def _pct_colour(pct: float | None) -> str:
    return _SEVERITY_GREY if pct is None else _severity_color(pct)


def _render_dashboard(
    profiles: list[dict[str, Any]],
    meta: dict[str, Any],
    recommended: dict[str, Any] | None,
) -> str:
    """Fleet-level header: counts, platform status, recommendation, rings."""
    total = meta.get("count")
    if not isinstance(total, int):
        total = len(profiles)
    ok = meta.get("ok")
    if not isinstance(ok, int):
        ok = sum(1 for p in profiles if p.get("health") == "ok")

    status_text = _platform_summary(meta)
    status = f'<span class="rw-status">{_esc(status_text)}</span>' if status_text else ""

    pick_row = ""
    if recommended and recommended.get("name"):
        why = recommended.get("rationale")
        why_text = f' <span class="rw-ringwin">{_esc(why)}</span>' if why else ""
        pick_row = (
            f'<div class="rw-pick">Recommended: <b>{_esc(recommended["name"])}</b>'
            f"{why_text}</div>"
        )
    elif recommended is not None:
        # An explicit no-candidate answer is more useful than an absent row.
        reason = recommended.get("reason") or "no profile currently selectable"
        pick_row = f'<div class="rw-pick">Recommended: <b>none</b> — {_esc(reason)}</div>'

    rings = "".join(_ring(p) for p in profiles)
    ring_row = f'<div class="rw-rings">{rings}</div>' if rings else ""
    right = _render_platform_panel(meta)

    body = (
        f'<div class="rw-cols"><div class="rw-col-l">{ring_row}</div>'
        f'<div class="rw-col-r">{right}</div></div>'
        if right
        else ring_row
    )

    return (
        '<div class="rw-head">'
        '<div class="rw-head-row">'
        f'{_CLAUDE_MARK}<span class="rw-title">roost</span>'
        f'<span class="rw-counts">{total} profile{"" if total == 1 else "s"} · '
        f"{ok} ok</span>"
        f"{status}"
        "</div>"
        f"{pick_row}{body}"
        f"{_incident_line(meta)}"
        "</div>"
    )


def _render_platform_panel(meta: dict[str, Any]) -> str:
    """Right-hand column: Anthropic component states + incident-day grid.

    Both inputs are optional — an older cache, a `--no-platform-status` run, or
    a Statuspage that never answered all yield an absent section rather than an
    empty box. Best-effort enrichment, per AGENTS.md rule 20.
    """
    platform = meta.get("platform_status")
    if not isinstance(platform, dict):
        return ""

    blocks: list[str] = []

    components = platform.get("components")
    if isinstance(components, list) and components:
        rows = []
        for component in components[:5]:
            if not isinstance(component, dict):
                continue
            name = _clip(component.get("name") or "?", 28)
            state = str(component.get("status") or "unknown")
            colour = _COMPONENT_COLORS.get(state, _SEVERITY_GREY)
            rows.append(
                '<div class="rw-comp">'
                f'<span class="rw-cdot" style="background:{colour}"></span>'
                f'<span class="rw-cname">{_esc(name)}</span>'
                f'<span class="rw-cstate" style="color:{colour}">'
                f'{_esc(state.replace("_", " "))}</span>'
                "</div>"
            )
        if rows:
            blocks.append("".join(rows))

    grid = _incident_grid(platform)
    if grid:
        blocks.append(grid)

    if not blocks:
        return ""
    return "".join(blocks)


def _incident_grid(platform: dict[str, Any]) -> str:
    """One square per day, coloured by that day's worst reported incident.

    Deliberately labelled "incidents", never "uptime". Statuspage's public API
    exposes incident records, not uptime measurements — a day with no incident
    is "nothing was reported", which is a weaker claim than "100% up". The
    window is whatever the data actually covers (the endpoint returns a bounded
    number of recent incidents), so it is read from the payload rather than
    assumed.
    """
    days = platform.get("incident_days")
    if not isinstance(days, list) or not days:
        return ""
    cells = []
    for day in days[-60:]:
        if not isinstance(day, dict):
            continue
        impact = str(day.get("impact") or "none")
        colour = _IMPACT_COLORS.get(impact, _SEVERITY_GREY)
        date = _esc(str(day.get("date") or ""))
        label = "no incidents" if impact == "none" else impact
        cells.append(
            f'<i class="rw-day" style="background:{colour}" '
            f'title="{date}: {_esc(label)}"></i>'
        )
    if not cells:
        return ""
    span = platform.get("history_days")
    span_text = f"last {span} days" if isinstance(span, int) and span > 0 else "recent"
    return (
        f'<div class="rw-gridlabel">Incidents · {_esc(span_text)}</div>'
        f'<div class="rw-daygrid">{"".join(cells)}</div>'
    )


def _platform_summary(meta: dict[str, Any]) -> str:
    """One-line Anthropic platform state, or '' when it is unknown."""
    platform_status = meta.get("platform_status")
    if not isinstance(platform_status, dict):
        return ""
    description = platform_status.get("description")
    if isinstance(description, str) and description:
        # Bounded for the same reason _incident_line is: a long Statuspage
        # description is the only unbounded field in the payload, and no
        # amount of dropping profiles can claw those bytes back.
        return f"Anthropic: {_clip(description)}"
    indicator = platform_status.get("indicator")
    if not indicator:
        return ""
    return f"Anthropic: {str(indicator).replace('_', ' ')}"



def _incident_line(meta: dict[str, Any]) -> str:
    platform_status = meta.get("platform_status")
    if not isinstance(platform_status, dict):
        return ""
    indicator = platform_status.get("indicator")
    if not indicator or indicator == "none":
        return ""
    label = platform_status.get("description") or str(indicator).replace("_", " ")
    return f'<span class="rw-incident">status.claude.com: {_esc(_clip(label))}</span>'


def _clip(text: object, limit: int = _INCIDENT_MAX_CHARS) -> str:
    """Bound a free-text field from the payload.

    Statuspage descriptions are the only unbounded input the renderer takes,
    and the byte budget cannot be recovered by dropping profiles — the text
    is in the fixed chrome.
    """
    value = str(text)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _render_card(
    profile: dict[str, Any],
    now: datetime,
    stats: dict[str, Any] | None = None,
) -> str:
    name = profile.get("name") or "unknown"
    health = profile.get("health") or "unknown"
    plan = profile.get("subscription_type")
    usage = profile.get("usage")
    usage = usage if isinstance(usage, dict) else None

    session_pct = _as_number(usage.get("session_pct")) if usage else None
    weekly_pct = _as_number(usage.get("weekly_pct")) if usage else None
    fable_limit = _active_fable_limit(usage)
    fable_pct = _as_number(fable_limit.get("percent")) if fable_limit else None
    overage_pct = _overage_pct(usage)

    fable_reset = _parse_dt(fable_limit.get("resets_at")) if fable_limit else None
    gauges = [
        _gauge_row("Session", session_pct, _parse_dt(profile.get("session_reset_at")), now),
        _gauge_row("Weekly", weekly_pct, _parse_dt(profile.get("weekly_reset_at")), now),
        _gauge_row("Fable", fable_pct, fable_reset, now),
    ]
    if overage_pct is not None:
        gauges.append(_gauge_row("Overage", overage_pct, None, now))

    return (
        '<div class="rw-card">'
        f"{_render_header(name, health, plan)}"
        f'<div class="rw-gauges">{"".join(gauges)}</div>'
        f"{_render_stats(stats)}"
        f"{_render_footer(profile, now, session_pct, weekly_pct, fable_pct, fable_limit)}"
        "</div>"
    )


def _render_stats(stats: dict[str, Any] | None) -> str:
    """Dispatch history + capacity trend for one profile.

    Everything here comes from roost's own logs, not the usage API: `picks`
    and `execs` from picks.log (the audit trail every pick and exec writes),
    `eta` from the opt-in usage-log's linear burn-rate projection. The API
    itself reports no tokens and no turns, so there is deliberately nothing
    resembling a token count here — inventing one would be worse than the
    blank space it fills.

    Absent stats render nothing at all rather than a row of zeros: the usage
    log is opt-in and off by default, and "0" would misread as "never
    dispatched" instead of "not recorded".
    """
    if not isinstance(stats, dict) or not stats:
        return ""
    cells = []
    picks = stats.get("picks")
    if isinstance(picks, int):
        cells.append(f"<span>Picks<b>{picks:,}</b></span>")
    execs = stats.get("execs")
    if isinstance(execs, int) and execs:
        cells.append(f"<span>Execs<b>{execs:,}</b></span>")
    eta = stats.get("eta")
    if isinstance(eta, str) and eta:
        cells.append(f"<span>Full in<b>{_esc(eta)}</b></span>")
    row = f'<div class="rw-stats">{"".join(cells)}</div>' if cells else ""
    return f"{row}{_render_spark(stats.get('trend'))}"


def _render_spark(trend: Any) -> str:
    """Bar strip of a profile's weekly-usage history, ff-monitor style.

    Heights are scaled against 100% rather than the series' own max, so two
    cards are directly comparable — a self-scaled strip would make a profile
    idling at 3% look identical to one at 90%.
    """
    if not isinstance(trend, list) or len(trend) < 2:
        return ""
    bars = []
    for value in trend[-28:]:
        try:
            pct = max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            continue
        height = max(6.0, pct)
        bars.append(
            f'<i style="height:{height:.0f}%;background:{_severity_color(pct)}"></i>'
        )
    if not bars:
        return ""
    return f'<div class="rw-spark">{"".join(bars)}</div>'


def _render_header(name: str, health: str, plan: object) -> str:
    color = _HEALTH_COLORS.get(health, _SEVERITY_GREY)
    tag = f'<span class="rw-tag">{_esc(plan)}</span>' if plan else ""
    return (
        '<div class="rw-hd">'
        f'<span class="rw-pip" style="background:{color}"></span>'
        f'<span class="rw-name">{_esc(name)}</span>'
        f'<span class="rw-state">{_esc(health)}</span>'
        f"{tag}"
        "</div>"
    )


def _render_footer(
    profile: dict[str, Any],
    now: datetime,
    session_pct: float | None,
    weekly_pct: float | None,
    fable_pct: float | None,
    fable_limit: dict[str, Any] | None,
) -> str:
    # Resets now render per-window on each gauge, so the footer carries only
    # probe freshness — the one fact that belongs to the card as a whole.
    del session_pct, weekly_pct, fable_pct, fable_limit
    probed_at = _parse_dt(profile.get("probed_at"))
    age = _relative_age(probed_at, now) if probed_at is not None else "—"
    return f'<div class="rw-foot">probed {_esc(age)}</div>'


def _gauge_row(
    label: str,
    pct: float | None,
    reset: datetime | None,
    now: datetime,
) -> str:
    """One capacity window: label, percent, bar, and its own reset time.

    Each window carries its own reset because they genuinely differ — the
    session window rolls every few hours while the weekly and model-scoped
    ones share a much later boundary. Claude's own usage panel shows them
    per-row for the same reason; a single card-level reset would have to pick
    one and silently misattribute the others.
    """
    if pct is None:
        return (
            f'<div class="rw-gauge"><div class="rw-glabel"><span>{label}</span>'
            '<span class="rw-val rw-mono">—</span></div>'
            '<div class="rw-gtrack"></div></div>'
        )
    clamped = max(0.0, min(100.0, pct))
    color = _severity_color(clamped)
    display = int(pct) if float(pct).is_integer() else round(pct, 1)
    reset_line = ""
    if reset is not None:
        reset_line = f'<div class="rw-reset">Resets {_esc(_format_reset(reset, now))}</div>'
    return (
        f'<div class="rw-gauge"><div class="rw-glabel"><span>{label}</span>'
        f'<span class="rw-val rw-mono" style="color:{color}">{display}% used</span></div>'
        f'<div class="rw-gtrack"><div class="rw-gfill" '
        f'style="width:{clamped:g}%;background:{color}"></div></div>'
        f"{reset_line}</div>"
    )


def _severity_color(pct: float) -> str:
    if pct >= 85:
        return _SEVERITY_RED
    if pct >= 60:
        return _SEVERITY_AMBER
    return _SEVERITY_GREEN


def _active_fable_limit(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not usage:
        return None
    limits = usage.get("limits")
    if not isinstance(limits, list):
        return None
    # Deliberately NOT filtered on `is_active`. Upstream marks exactly one
    # limit active per profile, meaning "this is the constraint currently
    # binding" — not "this limit is enforced". Filtering on it renders a
    # profile whose Fable window sits at 0% as "—" (no data) purely because
    # its session window is the nearer cap, which is the opposite of the
    # truth. Only the classifier gates on `is_active`, and only to decide
    # whether a profile leaves the pick pool.
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        model = entry.get("model")
        if isinstance(model, str) and model.strip().lower() == "fable":
            return entry
    return None


def _overage_pct(usage: dict[str, Any] | None) -> float | None:
    if not usage:
        return None
    spend = usage.get("spend")
    if not isinstance(spend, dict) or not spend.get("enabled"):
        return None
    return _as_number(spend.get("percent"))


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _parse_dt(value: object) -> datetime | None:
    """Accept both an ISO string and a live `datetime`.

    Both shapes genuinely reach here. The CLI passes the payload in-process,
    where `build_status_payload` hand-formats the top-level reset fields to
    ISO strings but dumps nested `limits[]` wholesale — so their `resets_at`
    is still a `datetime` object. Rejecting non-strings silently dropped
    every model-scoped reset in the rendered card while the string-fed tests
    stayed green.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _probed_sort_key(profile: dict[str, Any]) -> datetime:
    dt = _parse_dt(profile.get("probed_at"))
    return dt if dt is not None else datetime.min.replace(tzinfo=UTC)


def _relative_age(dt: datetime, now: datetime) -> str:
    delta = (now - dt).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 90 * 60:
        return f"{int(delta / 60)}m ago"
    if delta < 48 * 3600:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _format_reset(dt: datetime, now: datetime) -> str:
    """Absolute local wall-clock time for a reset, e.g. "Sat 2:00 PM".

    Mirrors Claude's own usage panel: relative only inside the last hour
    ("in 44 min"), absolute beyond it. A relative figure like "in 144h" is
    unreadable — nobody converts that to "Saturday afternoon" in their head,
    which is the only form that answers "can I run this tonight?".

    Rendered in LOCAL time, since the reset is something the operator plans
    their day around. The upstream timestamps are UTC, so this must convert;
    printing UTC as though it were local would be off by the whole offset.
    The date is added past a week out, where a bare weekday stops being
    unambiguous.
    """
    delta = (dt - now).total_seconds()
    if delta <= 0:
        return "now"
    if delta < 3600:
        return f"in {max(1, int(delta / 60))} min"
    local = dt.astimezone()
    # %-I / %#I are platform-specific, so strip the leading zero by hand.
    hour = local.strftime("%I").lstrip("0") or "12"
    stamp = f"{hour}:{local.strftime('%M %p')}"
    if delta < 7 * 86400:
        return f"{local.strftime('%a')} {stamp}"
    return f"{local.strftime('%a %d %b')} {stamp}"


def _esc(value: object) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
