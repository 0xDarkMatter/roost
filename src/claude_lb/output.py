"""Output helpers: JSON envelope + stream-separated rendering (SPEC §3)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from rich.console import Console
from rich.table import Table

from .models import HealthCache, ProfileHealth
from .term import Term, emit_panel

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

    Columns: Profile · Health · Session% · Weekly% · Sonnet% · Opus% · Overage · Resets in · Probed.

    Per-model columns (Sonnet/Opus) are dropped when no entry has the data,
    keeping the table compact for typical use.
    """
    if not entries:
        stderr.print("[yellow]No profiles discovered.[/yellow]")
        return
    now = datetime.now(UTC)

    # Optional columns: only show if at least one entry has the data.
    has_plan = any(e.subscription_type for e in entries)
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
