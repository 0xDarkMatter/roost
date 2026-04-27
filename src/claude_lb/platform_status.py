"""Cached fetch of status.claude.com summary.

Used by:
- `claude-lb doctor` — always-fresh diagnostic check
- `claude-lb status` — cached header above the profile table

The cache is small (a single JSON blob at `<config>/platform-status.json`) and
short-lived (60s TTL). When a fresh fetch fails we fall back to whatever's in
the cache — stale info is more useful than nothing, especially during the
exact incidents this surface is meant to surface. Atomic write-rename via
tempfile, same pattern as the health cache.

The shape of the data here is shared between modules; doctor and status both
consume PlatformStatus directly.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .paths import config_dir, ensure_config_dir

STATUS_PAGE_URL = "https://status.claude.com/api/v2/summary.json"
STATUS_CACHE_TTL_S = 60
STATUS_CACHE_TIMEOUT_S = 2.0  # tighter than doctor's 3s — `status` is hot path
_SCHEMA_VERSION = 1


@dataclass
class PlatformStatus:
    indicator: str  # none / minor / major / critical / unknown
    description: str
    active_incidents: list[dict[str, Any]] = field(default_factory=list)
    degraded_components: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    fetch_error: str | None = None  # set when fresh fetch failed AND no cache to use

    @property
    def is_clean(self) -> bool:
        return (
            self.indicator == "none"
            and not self.active_incidents
            and not self.degraded_components
            and self.fetch_error is None
        )

    @property
    def has_warning(self) -> bool:
        return not self.is_clean

    def age_seconds(self, *, now: datetime | None = None) -> float:
        ref = now or datetime.now(UTC)
        return (ref - self.fetched_at).total_seconds()


def status_cache_path() -> Path:
    return config_dir() / "platform-status.json"


def _parse_summary(payload: dict[str, Any]) -> PlatformStatus:
    """Extract the fields we care about from a Statuspage v2 summary body."""
    status_obj = payload.get("status") or {}
    indicator = str(status_obj.get("indicator") or "unknown")
    description = str(status_obj.get("description") or "(no description)")
    active = [
        {
            "name": i.get("name"),
            "status": i.get("status"),
            "impact": i.get("impact"),
            "shortlink": i.get("shortlink"),
        }
        for i in (payload.get("incidents") or [])
        if str(i.get("status") or "").lower() != "resolved"
    ]
    degraded = [
        {"name": c.get("name"), "status": c.get("status")}
        for c in (payload.get("components") or [])
        if str(c.get("status") or "operational").lower() != "operational"
    ]
    return PlatformStatus(
        indicator=indicator,
        description=description,
        active_incidents=active,
        degraded_components=degraded,
    )


def fetch_platform_status(timeout_s: float = STATUS_CACHE_TIMEOUT_S) -> PlatformStatus:
    """One-shot fetch + parse. Raises httpx.HTTPError or ValueError on failure.

    Doesn't touch the cache. Doctor uses this directly (always-fresh check);
    `status` goes through `load_or_fetch` for cache-aware behaviour.
    """
    response = httpx.get(STATUS_PAGE_URL, timeout=timeout_s)
    response.raise_for_status()
    payload = response.json()
    return _parse_summary(payload)


def _write_cache(status: PlatformStatus) -> None:
    """Atomic write — same pattern as health.json. Best-effort: this is a
    cache, not load-bearing data, so any failure swallows silently."""
    try:
        target_dir = ensure_config_dir()
    except OSError:  # pragma: no cover  -- can't even create config dir; nothing to cache
        return
    target = target_dir / "platform-status.json"
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "fetched_at": status.fetched_at.isoformat().replace("+00:00", "Z"),
        "indicator": status.indicator,
        "description": status.description,
        "active_incidents": status.active_incidents,
        "degraded_components": status.degraded_components,
    }
    try:
        fd, tmp = tempfile.mkstemp(prefix=".platform-status-", dir=str(target_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, target)
        except Exception:  # pragma: no cover  -- atomic-write failure; best-effort cache
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:  # pragma: no cover  -- mkstemp failed; cache is best-effort
        return


def _read_cache() -> PlatformStatus | None:
    target = status_cache_path()
    if not target.is_file():
        return None
    try:
        with target.open("rb") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if payload.get("schema_version") != _SCHEMA_VERSION:
        return None
    fetched_at_raw = payload.get("fetched_at")
    if not isinstance(fetched_at_raw, str):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return PlatformStatus(
        indicator=str(payload.get("indicator") or "unknown"),
        description=str(payload.get("description") or "(no description)"),
        active_incidents=list(payload.get("active_incidents") or []),
        degraded_components=list(payload.get("degraded_components") or []),
        fetched_at=fetched_at,
    )


def load_or_fetch(
    *,
    ttl_s: int = STATUS_CACHE_TTL_S,
    timeout_s: float = STATUS_CACHE_TIMEOUT_S,
    force_refresh: bool = False,
) -> PlatformStatus | None:
    """Cache-aware fetch. Returns None only when there's truly nothing to show
    (no cache + fetch failed + no stale fallback).

    Behaviour matrix:
      - cache fresh (< ttl_s)        -> return cached, no network
      - cache stale or absent        -> fetch fresh; cache; return fresh
      - fetch fails + stale cache    -> return stale cache (still useful)
      - fetch fails + no cache       -> return PlatformStatus with fetch_error
                                        set so callers can render "unreachable"
      - force_refresh=True           -> always fetch; on failure, fall back to
                                        stale cache as above
    """
    cached = _read_cache()
    if not force_refresh and cached is not None and cached.age_seconds() < ttl_s:
        return cached

    try:
        fresh = fetch_platform_status(timeout_s=timeout_s)
    except (httpx.HTTPError, ValueError) as exc:
        if cached is not None:
            return cached  # stale-but-useful
        return PlatformStatus(
            indicator="unknown",
            description="status.claude.com unreachable",
            fetch_error=f"{type(exc).__name__}: {exc}",
        )

    _write_cache(fresh)
    return fresh


def format_status_line(status: PlatformStatus) -> str:
    """One-line summary suitable for stderr above the status table.

    Returns an empty string when there's nothing to say (clean state). Callers
    can `if line: stderr.print(line)` without nil-guards leaking elsewhere.
    """
    if status.is_clean:
        return ""

    age_s = int(status.age_seconds())
    age_suffix = ""
    # Annotate cached data older than the freshness window (means we're either
    # operating on a stale-fallback after fetch failure, or someone bumped TTL).
    if age_s > STATUS_CACHE_TTL_S:
        age_suffix = f" (cached {age_s}s ago)"

    if status.fetch_error:
        return f"[yellow]Anthropic status:[/yellow] unreachable ({status.fetch_error}){age_suffix}"

    # Use parens (not square brackets) for the impact/status tuple — Rich
    # parses unknown `[foo]` as style markup and silently drops the segment.
    parts = [f"[bold]Anthropic:[/bold] {status.description}"]
    if status.active_incidents:
        first = status.active_incidents[0]
        impact = first.get("impact") or "unknown"
        i_status = first.get("status") or "unknown"
        more = (
            f" (+{len(status.active_incidents) - 1} more)"
            if len(status.active_incidents) > 1
            else ""
        )
        parts.append(
            f"[yellow]incident[/yellow]: '{first.get('name')}' ({i_status}, {impact}){more}"
        )
    if status.degraded_components:
        names = ", ".join(str(c.get("name")) for c in status.degraded_components[:3])
        more = (
            ""
            if len(status.degraded_components) <= 3
            else f" (+{len(status.degraded_components) - 3} more)"
        )
        parts.append(f"[yellow]degraded[/yellow]: {names}{more}")

    return "[cyan]·[/cyan] " + "  ".join(parts) + age_suffix


def to_json_meta(status: PlatformStatus) -> dict[str, Any]:
    """Serialise for inclusion in a `meta` envelope (e.g. status --json)."""
    return {
        "indicator": status.indicator,
        "description": status.description,
        "active_incidents": status.active_incidents,
        "degraded_components": status.degraded_components,
        "fetched_at": status.fetched_at.isoformat().replace("+00:00", "Z"),
        "age_seconds": int(status.age_seconds()),
        "fetch_error": status.fetch_error,
    }
