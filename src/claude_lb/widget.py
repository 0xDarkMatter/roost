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
# uppercase micro-labels, monospace figures), and host tokens would pull it
# toward the softer editorial look of the surrounding page. Change it here and
# in ff-monitor.html together.
#
# Scoped to `.rw` rather than `:root` because this renders as a FRAGMENT inside
# a host page — writing `:root` would leak roost's palette onto everything else
# on it. The prefers-color-scheme block plus the data-theme overrides mean the
# cards track light/dark both standalone (opened as a file) and inside the chat
# host's own theme toggle. An earlier build hardcoded dark values as var()
# fallbacks, which rendered every card dark on a light desktop.
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
    "margin:0 0 10px;font-size:10px;color:var(--rw-muted);"
    "text-transform:uppercase;letter-spacing:.08em}"
    ".rw-incident{width:100%;color:var(--rw-warn);text-transform:none;"
    "letter-spacing:0;font-size:11px}"
    ".rw-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));"
    "gap:8px}"
    ".rw-empty{font-size:11px;color:var(--rw-muted);padding:1rem 0}"
    ".rw-card{background:var(--rw-card);border:1px solid var(--rw-border);"
    "border-radius:8px;padding:8px 10px;display:flex;flex-direction:column;"
    "gap:7px;min-width:0}"
    ".rw-hd{display:flex;align-items:center;gap:7px;flex-wrap:wrap;row-gap:4px}"
    ".rw-pip{width:8px;height:8px;border-radius:1.5px;flex:none}"
    ".rw-name{font-size:12px;font-weight:600;overflow-wrap:anywhere}"
    ".rw-state{font-size:10px;color:var(--rw-muted);text-transform:uppercase;"
    "letter-spacing:.06em}"
    ".rw-tag{font-size:10px;color:var(--rw-muted);text-transform:uppercase;"
    "letter-spacing:.06em;margin-left:auto}"
    ".rw-gauges{display:flex;flex-direction:column;gap:5px}"
    ".rw-glabel{display:flex;justify-content:space-between;align-items:baseline;"
    "gap:8px;font-size:10px;color:var(--rw-muted);text-transform:uppercase;"
    "letter-spacing:.08em}"
    ".rw-val{font-size:11px;font-weight:500;letter-spacing:0}"
    ".rw-gtrack{height:4px;border-radius:1px;background:var(--rw-track);"
    "overflow:hidden;margin-top:2px}"
    ".rw-gfill{height:100%;border-radius:1px}"
    ".rw-reset{font-size:10px;color:var(--rw-muted);margin-top:1px}"
    ".rw-foot{border-top:1px solid var(--rw-border);margin-top:1px;padding-top:5px;"
    "font-size:10px;color:var(--rw-muted)}"
    "</style>"
)

# Severity buckets: green under 60, amber 60-84, red 85+ (gauges).
_SEVERITY_GREEN = "#1D9E75"
_SEVERITY_AMBER = "#BA7517"
_SEVERITY_RED = "#E24B4A"
_SEVERITY_GREY = "#8a8a86"

# Health-state -> pip/text colour. `model_limit` isn't in the taxonomy yet
# (see models.Health) but is named in the widget spec, so it's mapped
# defensively for forward compat; unknown values fall back to grey.
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
    profiles: list[dict[str, Any]], meta: dict[str, Any], *, max_bytes: int = 28_672
) -> str:
    """Render the capacity-card page for `roost widget`.

    `profiles` mirrors `status --json`'s `data[]`; `meta` mirrors its
    `meta` object (used for the summary header's totals and any
    platform-status incident line). Both are read defensively via
    `.get(...)` — see the module docstring for why.

    Drops the least-recently-probed profiles (oldest `probed_at` first)
    until the page fits `max_bytes`, warning via `warnings.warn` if it had
    to drop anything.
    """
    meta = meta if isinstance(meta, dict) else {}
    working = [p for p in (profiles or []) if isinstance(p, dict)]
    original_count = len(working)

    html = _render(working, meta)
    while len(html.encode("utf-8")) > max_bytes and working:
        working = sorted(working, key=_probed_sort_key)
        working.pop(0)
        html = _render(working, meta)

    dropped = original_count - len(working)
    if dropped:
        warnings.warn(
            f"roost widget: dropped {dropped} least-recently-probed profile(s) "
            f"to stay under the {max_bytes}-byte show_widget inline-render budget",
            stacklevel=2,
        )
    return html


def _render(profiles: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    now = datetime.now(UTC)
    cards = "".join(_render_card(p, now) for p in profiles)
    if cards:
        grid = f'<div class="rw-grid">{cards}</div>'
    else:
        grid = '<div class="rw-empty">No profiles discovered — run <code>roost probe</code> first.</div>'
    # Everything lives inside .rw so the palette custom properties stay scoped
    # to this fragment instead of leaking onto the host page.
    return f'{_STYLE}<div class="rw">{_render_summary(profiles, meta)}{grid}</div>'


def _render_summary(profiles: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    total = meta.get("count")
    if not isinstance(total, int):
        total = len(profiles)
    ok = meta.get("ok")
    if not isinstance(ok, int):
        ok = sum(1 for p in profiles if p.get("health") == "ok")
    line = (
        '<div class="rw-summary">'
        f'<span>{total} profile{"" if total == 1 else "s"}</span>'
        f"<span>{ok} ok</span>"
        f"{_incident_line(meta)}"
        "</div>"
    )
    return line


def _incident_line(meta: dict[str, Any]) -> str:
    platform_status = meta.get("platform_status")
    if not isinstance(platform_status, dict):
        return ""
    indicator = platform_status.get("indicator")
    if not indicator or indicator == "none":
        return ""
    label = platform_status.get("description") or str(indicator).replace("_", " ")
    return f'<span class="rw-incident">status.claude.com: {_esc(label)}</span>'


def _render_card(profile: dict[str, Any], now: datetime) -> str:
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
        f"{_render_footer(profile, now, session_pct, weekly_pct, fable_pct, fable_limit)}"
        "</div>"
    )


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
