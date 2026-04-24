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


def _health_style(health: str) -> str:
    return {
        "ok": "green",
        "rate_limited": "yellow",
        "session_limit": "yellow",
        "weekly_limit": "red",
        "auth_dead": "red",
        "network_error": "magenta",
        "unknown": "dim",
    }.get(health, "white")


def render_status_table(entries: list[ProfileHealth]) -> None:
    """Render the status table to stderr."""
    if not entries:
        stderr.print("[yellow]No profiles discovered.[/yellow]")
        return
    now = datetime.now(UTC)
    table = Table(title="claude-lb profiles", show_lines=False)
    table.add_column("Profile", style="bold")
    table.add_column("Health")
    table.add_column("Retry / Reset")
    table.add_column("Weekly")
    table.add_column("Probed")
    for e in entries:
        health_str = e.health.value
        style = _health_style(health_str)
        retry = "—"
        if e.retry_after_s:
            retry = f"{e.retry_after_s}s"
        elif e.session_reset_at:
            retry = e.session_reset_at.strftime("%Y-%m-%d %H:%M UTC")
        elif e.weekly_reset_at:
            retry = e.weekly_reset_at.strftime("%Y-%m-%d %H:%M UTC")
        elif e.health.value == "auth_dead":
            retry = "claude login"
        weekly = (
            f"{e.usage.weekly_pct}%"
            if (e.usage and e.usage.weekly_pct is not None)
            else "—"
        )
        probed_at = e.probed_at
        if probed_at.tzinfo is None:
            probed_at = probed_at.replace(tzinfo=UTC)
        age = _humanize_age((now - probed_at).total_seconds())
        table.add_row(
            e.name,
            f"[{style}]{health_str}[/{style}]",
            retry,
            weekly,
            age,
        )
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
