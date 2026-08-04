"""Output helpers: JSON envelope + stream-separated rendering (SPEC §3)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from enum import Enum
from typing import IO, Any

from rich.console import Console
from rich.table import Table

from .models import HealthCache, ProfileHealth
from .term import Term, display_width, emit_panel

stderr = Console(stderr=True)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def emit_json(payload: Any) -> None:
    """Write JSON to stdout with a trailing newline."""
    sys.stdout.write(json.dumps(payload, indent=2, default=_json_default))
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_ndjson(items: list[Any]) -> None:
    """Write one JSON object per line to stdout."""
    for item in items:
        sys.stdout.write(json.dumps(item, default=_json_default))
        sys.stdout.write("\n")
    sys.stdout.flush()


def _ensure_utf8_stdout() -> None:
    """Reconfigure stdout to UTF-8 once, if the stream supports it.

    Idempotent and best-effort: a stream already at UTF-8 is left alone, and a
    replaced stdout (pytest's capture, a StringIO) may lack `reconfigure`
    entirely — neither case is an error worth failing a command over.
    """
    stream = sys.stdout
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    if encoding in ("utf8", "utf8mb4"):
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8")
    except (ValueError, OSError):  # pragma: no cover -- exotic stream
        pass


def emit_text(text: str) -> None:
    """Write plain text to stdout with a trailing newline.

    Forces UTF-8 on the way out. Windows defaults stdout to the console
    codepage (cp1252/cp437), which cannot encode the separators and dashes
    the widget and table renderers emit — redirecting `roost widget` to a
    file produced mojibake before this. Data on stdout must survive a pipe.
    """
    _ensure_utf8_stdout()
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def emit_error_json(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    emit_json(payload)


def _humanize_age(delta_seconds: float) -> str:
    if delta_seconds < 0:
        return "in future"
    if delta_seconds < 90:
        return f"{int(delta_seconds)}s ago"
    if delta_seconds < 90 * 60:
        return f"{int(delta_seconds / 60)}m ago"
    if delta_seconds < 48 * 3600:
        return f"{int(delta_seconds / 3600)}h ago"
    return f"{int(delta_seconds / 86400)}d ago"


def humanize_until(target: datetime | None, now: datetime | None = None) -> str:
    """Format a future timestamp as 'in Xm' / 'in Xh Ym' / 'in Xd'.

    Callers get a short, scannable string suitable for terminal columns.
    Past timestamps return 'now' (reset is already available). None returns '—'.
    """
    if target is None:
        return "—"
    current = now or datetime.now(UTC)
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    delta = (target - current).total_seconds()
    if delta <= 0:
        return "now"
    if delta < 60:
        return f"in {int(delta)}s"
    if delta < 3600:
        return f"in {int(delta / 60)}m"
    if delta < 48 * 3600:
        hours = int(delta / 3600)
        minutes = int((delta - hours * 3600) / 60)
        return f"in {hours}h {minutes}m" if minutes else f"in {hours}h"
    return f"in {int(delta / 86400)}d"


def _health_style(health: str) -> str:
    return {
        "ok": "green",
        "rate_limited": "yellow",
        "session_limit": "yellow",
        "weekly_limit": "red",
        # Amber (rendered as Rich "yellow" — there's no distinct amber in the
        # 8-color palette) matching session_limit/auth_expired: the account
        # is alive and this clears on its own once model_reset_at passes.
        "model_limit": "yellow",
        "auth_expired": "yellow",
        "auth_dead": "red",
        "network_error": "magenta",
        "unknown": "dim",
    }.get(health, "white")


def _pct_cell(pct: int | None, threshold_high: int = 100) -> str:
    """Render a utilisation percent with colour coding."""
    if pct is None:
        return "—"
    if pct >= threshold_high:
        return f"[red]{pct}%[/red]"
    if pct >= 80:
        return f"[yellow]{pct}%[/yellow]"
    return f"{pct}%"


def _render_extra_usage(extra: object) -> str:
    """Render the extra_usage (monthly overage) column."""
    if extra is None:
        return "—"
    # Avoid importing ExtraUsage here — duck-type the fields we need.
    is_enabled = getattr(extra, "is_enabled", False)
    utilization = getattr(extra, "utilization", None)
    currency = getattr(extra, "currency", None)
    if not is_enabled:
        return "off"
    if utilization is None:
        return "on"
    label = f"{utilization}%"
    if currency:
        label += f" {currency}"
    if utilization >= 100:
        return f"[red]{label}[/red]"
    if utilization >= 80:
        return f"[yellow]{label}[/yellow]"
    return label


def render_status_table(entries: list[ProfileHealth]) -> None:
    """Render the status table to stderr.

    Columns: Profile · Health · Session% · Weekly% · Fable% · Sonnet% · Opus%
    · Overage · Session in · Weekly in · Probed.

    Per-model columns are dropped when no entry has the data, keeping the
    table compact for typical use. Fixed three (Fable/Sonnet/Opus) rather
    than derived from the union of `usage.limits[].model` names — Anthropic
    has only ever populated three model-scoped windows to date, and a fixed
    set keeps column order deterministic for tests/scripts without needing
    to sort a dynamic name list on every render.
    """
    if not entries:
        stderr.print("[yellow]No profiles discovered.[/yellow]")
        return
    now = datetime.now(UTC)

    # Optional columns: only show if at least one entry has the data.
    has_plan = any(e.subscription_type for e in entries)
    has_fable = any(e.usage and e.usage.fable_pct is not None for e in entries)
    # sonnet_pct/opus_pct come from the legacy seven_day_sonnet/seven_day_opus
    # windows, which are null on every current Max account now that
    # Anthropic moved per-model capacity into `limits[]` (see Usage
    # docstring in models.py). They are NOT dead code: a Pro/Team account or
    # an older server build may still populate them, so the columns stay,
    # gated by the same has-data check as every other optional column.
    has_sonnet = any(e.usage and e.usage.sonnet_pct is not None for e in entries)
    has_opus = any(e.usage and e.usage.opus_pct is not None for e in entries)
    has_extra = any(e.usage and e.usage.extra is not None for e in entries)

    table = Table(title="roost profiles", show_lines=False)
    table.add_column("Profile", style="bold")
    if has_plan:
        table.add_column("Plan")
    table.add_column("Health")
    table.add_column("Session", justify="right")
    table.add_column("Weekly", justify="right")
    if has_fable:
        table.add_column("Fable", justify="right")
    if has_sonnet:
        table.add_column("Sonnet", justify="right")
    if has_opus:
        table.add_column("Opus", justify="right")
    if has_extra:
        table.add_column("Overage", justify="right")
    table.add_column("Session in", justify="right", no_wrap=True)
    table.add_column("Weekly in", justify="right", no_wrap=True)
    table.add_column("Probed")

    for e in entries:
        health_str = e.health.value
        style = _health_style(health_str)

        # Session + weekly percent columns
        session_cell = (
            _pct_cell(e.usage.session_pct) if (e.usage and e.usage.session_pct is not None) else "—"
        )
        weekly_cell = (
            _pct_cell(e.usage.weekly_pct) if (e.usage and e.usage.weekly_pct is not None) else "—"
        )

        # Separate Session / Weekly reset columns so tabular output lines up.
        # Broken states (auth_*, rate_limited) surface the remediation in the
        # Session column; Weekly is "—" in those cases since it's not
        # meaningful. For OK-ish profiles, both show durations without the
        # "in " prefix (the column header makes the meaning clear).
        session_in: str
        weekly_in: str
        if e.retry_after_s is not None:
            session_in = f"{e.retry_after_s}s"
            weekly_in = "—"
        elif e.health.value == "auth_expired":
            session_in = f"roost refresh {e.name}"
            weekly_in = "—"
        elif e.health.value == "auth_dead":
            session_in = "claude login"
            weekly_in = "—"
        elif e.health.value == "model_limit":
            # Model-scoped exhaustion is orthogonal to the aggregate session
            # window — the account isn't session-limited, so showing the
            # model's own reset (model_reset_at) in the Session slot is the
            # actionable answer to "when can I use this model again". Weekly
            # keeps showing the real weekly reset since that window is still
            # live and informative.
            session_in = (
                humanize_until(e.model_reset_at, now).removeprefix("in ")
                if e.model_reset_at is not None
                else "—"
            )
            weekly_in = (
                humanize_until(e.weekly_reset_at, now).removeprefix("in ")
                if e.weekly_reset_at is not None
                else "—"
            )
        else:
            session_in = (
                humanize_until(e.session_reset_at, now).removeprefix("in ")
                if e.session_reset_at is not None
                else "—"
            )
            weekly_in = (
                humanize_until(e.weekly_reset_at, now).removeprefix("in ")
                if e.weekly_reset_at is not None
                else "—"
            )

        probed_at = e.probed_at
        if probed_at.tzinfo is None:
            probed_at = probed_at.replace(tzinfo=UTC)
        age = _humanize_age((now - probed_at).total_seconds())

        row: list[str] = [e.name]
        if has_plan:
            row.append(e.subscription_type or "—")
        row.extend(
            [
                f"[{style}]{health_str}[/{style}]",
                session_cell,
                weekly_cell,
            ]
        )
        if has_fable:
            row.append(
                _pct_cell(e.usage.fable_pct) if (e.usage and e.usage.fable_pct is not None) else "—"
            )
        if has_sonnet:
            row.append(
                _pct_cell(e.usage.sonnet_pct) if (e.usage and e.usage.sonnet_pct is not None) else "—"
            )
        if has_opus:
            row.append(
                _pct_cell(e.usage.opus_pct) if (e.usage and e.usage.opus_pct is not None) else "—"
            )
        if has_extra:
            row.append(_render_extra_usage(e.usage.extra) if e.usage else "—")
        row.extend([session_in, weekly_in, age])
        table.add_row(*row)
    stderr.print(table)


def build_status_payload(
    cache: HealthCache,
    discovered_names: list[str],
    *,
    platform_status_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce the --json payload for `profiles status`.

    If `platform_status_meta` is provided (from
    `platform_status.to_json_meta`), it's folded into `meta.platform_status`
    so scripts can consume Anthropic's status alongside the per-profile data.
    """
    now = datetime.now(UTC)
    data: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "ok": 0,
        "rate_limited": 0,
        "session_limit": 0,
        "weekly_limit": 0,
        "auth_expired": 0,
        "auth_dead": 0,
        "network_error": 0,
        "unknown": 0,
    }
    for name in discovered_names:
        entry = cache.profiles.get(name)
        if entry is None:
            counts["unknown"] += 1
            data.append({
                "name": name,
                "health": "unknown",
                "subscription_type": None,
                "probed_at": now.isoformat().replace("+00:00", "Z"),
                "usage": None,
                "retry_after_s": None,
                "session_reset_at": None,
                "weekly_reset_at": None,
                "error": None,
            })
            continue
        counts[entry.health.value] = counts.get(entry.health.value, 0) + 1
        data.append({
            "name": entry.name,
            "health": entry.health.value,
            "subscription_type": entry.subscription_type,
            "probed_at": _iso(entry.probed_at),
            "expires_at": _iso(entry.expires_at),
            "usage": entry.usage.model_dump() if entry.usage else None,
            "retry_after_s": entry.retry_after_s,
            "session_reset_at": _iso(entry.session_reset_at),
            "weekly_reset_at": _iso(entry.weekly_reset_at),
            "error": entry.error.model_dump() if entry.error else None,
            "probe_latency_ms": entry.probe_latency_ms,
        })
    meta: dict[str, Any] = {"count": len(data), **counts}
    if platform_status_meta is not None:
        meta["platform_status"] = platform_status_meta
    return {"data": data, "meta": meta}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat().replace("+00:00", "Z")


def render_pick_explanation(
    *,
    discovered_names: list[str],
    chosen: str | None,
    strategy: str,
    rationale: str,
    excluded_reasons: dict[str, str],
    filter_scores: dict[str, float],
) -> None:
    """Render the pick decision as a grouped-tree panel to stderr.

    Profiles are bucketed into CHOSEN / CANDIDATES / EXCLUDED / NOT CONSIDERED
    sections (TERMINAL-DESIGN.md §5.1); the rationale rides a dim summary line
    above the footer. Empty buckets are omitted. The whole panel is one write
    so it stays readable when stderr is redirected to a file.
    """
    t = Term()

    candidates = [
        n for n in discovered_names
        if n != chosen and n not in excluded_reasons and n in filter_scores
    ]
    excluded = [n for n in discovered_names if n in excluded_reasons]
    not_considered = [
        n for n in discovered_names
        if n != chosen and n not in excluded_reasons and n not in filter_scores
    ]

    lines: list[str] = [
        t.panel_open("roost", "roost · pick", indicator=strategy),
        t.vert(),
    ]

    def _section(label: str, names: list[str], color: str | None, detail_for) -> None:
        if not names:
            return
        lines.append(t.section(label, len(names), color=color))
        for i, name in enumerate(names):
            lines.append(
                t.leaf(name, detail=detail_for(name), last=(i == len(names) - 1))
            )
        lines.append(t.vert())

    if chosen is not None:
        score = filter_scores.get(chosen)
        detail = f"score={score:.2f}" if score is not None else "—"
        lines.append(t.section("CHOSEN", 1, color="green"))
        lines.append(t.leaf(chosen, detail=detail, last=True))
        lines.append(t.vert())

    _section(
        "CANDIDATES", candidates, None,
        lambda n: f"score={filter_scores[n]:.2f}",
    )
    _section("EXCLUDED", excluded, "red", lambda n: excluded_reasons[n])
    _section("NOT CONSIDERED", not_considered, "dim", lambda _n: "—")

    if rationale:
        lines.append(t.summary_line(rationale))
        lines.append(t.vert())

    footer_health = (
        t.health("healthy", chosen) if chosen is not None
        else t.health("critical", "no pick")
    )
    lines.append(t.panel_close(left_text="pick decision", right_text=footer_health))

    emit_panel(lines)


# Map roost's 9-state Health values onto term.Term's health-glyph vocabulary
# (healthy/pending/warning/critical/busted/unknown — see term.py's _HEALTH
# registry). This is a display-only mapping; it doesn't affect classification.
_CARD_HEALTH_STATE: dict[str, str] = {
    "ok": "healthy",
    "session_limit": "pending",
    # Amber, same rationale as _health_style: alive but degraded, clears on
    # its own once model_reset_at passes.
    "model_limit": "warning",
    "rate_limited": "warning",
    "auth_expired": "warning",
    "weekly_limit": "critical",
    "network_error": "critical",
    "auth_dead": "busted",
    "unknown": "unknown",
}

# (label, extractor) pairs for the four capacity windows a card shows, in
# display order. extractor reads the percent from a Usage instance (never
# called when usage is None — callers guard that).
_CARD_WINDOWS: list[tuple[str, Any]] = [
    ("Session", lambda u: u.session_pct),
    ("Weekly", lambda u: u.weekly_pct),
    ("Fable", lambda u: u.fable_pct),
    ("Overage", lambda u: u.extra.utilization if u.extra else None),
]


def _bar_width_for(t: Term) -> int:
    """Bar glyph count, shrunk to fit narrow terminals.

    Reserves room for the left rail, a name column, the gap, and the percent
    text so the row never overruns `t.width` on an 80-column-or-narrower tty.
    """
    return max(6, min(20, t.width - 34))


def _capacity_bar(pct: int | None, t: Term, *, width: int) -> str:
    """Render a proportional bar for one capacity window.

    ASCII fallback (`#`/`-`) applies whenever `t.ascii` is set (TERM_ASCII=1
    or a non-UTF locale) — the block glyphs (`█`/`░`) aren't guaranteed to
    exist in every legacy/CI terminal font, and term.py's whole point is to
    make that fallback automatic for every consumer.
    """
    if pct is None:
        return t.paint("dim", "-" * width if t.ascii else "░" * width)
    filled = max(0, min(width, round(width * min(pct, 100) / 100)))
    full_ch = "#" if t.ascii else "█"
    empty_ch = "-" if t.ascii else "░"
    bar = full_ch * filled + empty_ch * (width - filled)
    color = "red" if pct >= 100 else "yellow" if pct >= 80 else "green"
    return t.paint(color, bar)


def _capacity_row(label: str, pct: int | None, t: Term, *, name_col: int = 8) -> str:
    pad = " " * max(name_col - display_width(label), 0)
    # "-" under ascii_mode so a TERM_ASCII=1/non-UTF terminal never sees the
    # em dash byte — every glyph on this row must honour the same fallback.
    pct_text = f"{pct}%" if pct is not None else ("-" if t.ascii else "—")
    bar = _capacity_bar(pct, t, width=_bar_width_for(t))
    return f"{t.paint('dim', t.vert_g)}   {label}{pad} {bar}  {pct_text}"


def _capacity_card_lines(entry: ProfileHealth, t: Term) -> list[str]:
    """Build the panel lines for one profile's capacity card."""
    health_str = entry.health.value
    card_state = _CARD_HEALTH_STATE.get(health_str, "unknown")
    plan = entry.subscription_type or ("-" if t.ascii else "—")

    lines = [
        t.panel_open("roost", entry.name, indicator=health_str),
        t.vert(),
        t.summary_line(f"plan: {plan}"),
    ]
    usage = entry.usage
    for label, extractor in _CARD_WINDOWS:
        # usage=None (Pro/Team accounts, see AGENTS.md) must render "—"
        # placeholders, never a crash and never a misleading 0%.
        pct = extractor(usage) if usage is not None else None
        lines.append(_capacity_row(label, pct, t))
    lines.append(t.vert())
    lines.append(
        t.panel_close(left_text="capacity", right_text=t.health(card_state, health_str))
    )
    return lines


def render_capacity_cards(entries: list[ProfileHealth], *, file: IO[str] | None = None) -> None:
    """Render one capacity card per profile to `file` (default stderr).

    Human chrome only, never the data product roost's stdout contract
    promises (SPEC §3 / AGENTS.md rule 8), so this is stderr-bound like every
    other renderer in this module. Each card is its own panel (profile name +
    health, plan, and a bar per Session/Weekly/Fable/Overage window); all
    panels are written in one call so output stays coherent even when
    stderr is redirected to a file.
    """
    out = file if file is not None else sys.stderr
    t = Term(stream=out)
    lines: list[str] = []
    for entry in entries:
        lines.extend(_capacity_card_lines(entry, t))
    if lines:
        emit_panel(lines, file=out)
