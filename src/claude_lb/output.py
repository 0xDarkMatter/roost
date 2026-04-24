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


def emit_text(text: str) -> None:
    """Write plain text to stdout with a trailing newline."""
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

    table = Table(title="claude-lb profiles", show_lines=False)
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
            session_in = f"claude-lb refresh {e.name}"
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


def build_status_payload(cache: HealthCache, discovered_names: list[str]) -> dict[str, Any]:
    """Produce the --json payload for `profiles status`."""
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
    return {"data": data, "meta": meta}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat().replace("+00:00", "Z")
