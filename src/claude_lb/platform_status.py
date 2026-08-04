"""Cached fetch of status.claude.com summary + incident-day history.

Used by:
- `roost doctor` — always-fresh diagnostic check
- `roost status` — cached header above the profile table

The cache is small (a single JSON blob at `<config>/platform-status.json`) and
short-lived (60s TTL). When a fresh fetch fails we fall back to whatever's in
the cache — stale info is more useful than nothing, especially during the
exact incidents this surface is meant to surface. Atomic write-rename via
tempfile, same pattern as the health cache.

Two upstream endpoints feed PlatformStatus: summary.json (indicator, active
incidents, per-component status) and incidents.json (reduced to a per-day
history for dashboard widgets). Both fetches are best-effort per Rule 20 in
AGENTS.md — see fetch_incident_days() for how the incidents side fails safe.

The shape of the data here is shared between modules; doctor and status both
consume PlatformStatus directly.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .paths import config_dir, ensure_config_dir

STATUS_PAGE_URL = "https://status.claude.com/api/v2/summary.json"
INCIDENTS_PAGE_URL = "https://status.claude.com/api/v2/incidents.json"
STATUS_CACHE_TTL_S = 60
STATUS_CACHE_TIMEOUT_S = 2.0  # tighter than doctor's 3s — `status` is hot path
_SCHEMA_VERSION = 1

# Statuspage's own impact vocabulary, ranked worst-wins. Do NOT sort these as
# strings — "critical" < "major" alphabetically would invert the ranking.
_IMPACT_RANK: dict[str, int] = {
    "none": 0,
    "maintenance": 1,
    "minor": 2,
    "major": 3,
    "critical": 4,
}


def _worst_impact(a: str, b: str) -> str:
    return a if _IMPACT_RANK.get(a, 0) >= _IMPACT_RANK.get(b, 0) else b


@dataclass
class PlatformStatus:
    indicator: str  # none / minor / major / critical / unknown
    description: str
    active_incidents: list[dict[str, Any]] = field(default_factory=list)
    degraded_components: list[dict[str, Any]] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)  # every non-group component
    # Per-UTC-calendar-day incident record, oldest -> newest, contiguous (clean
    # days included as impact="none"). This is NOT an uptime series — it is a
    # reduction of *reported* incidents.json entries. A day with no incident
    # touching it means "nothing was reported that day", not "measured 100%
    # uptime" — Statuspage's public API doesn't expose uptime measurements at
    # all, so never relabel this field or its docs as uptime.
    incident_days: list[dict[str, Any]] = field(default_factory=list)
    # How many days incident_days actually spans. incidents.json only returns
    # the ~50 most recent incidents (observed ~28 days of coverage, not a
    # fixed 90-day window) — always derive this from the fetched data instead
    # of hardcoding a window size, so a consumer can label the grid honestly.
    history_days: int = 0
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
    # Statuspage "group" rows are containers for other components (e.g. "API"
    # grouping "API - US" / "API - EU"), not real components — including them
    # would render as duplicate/empty entries in a per-component list.
    components = [
        {"name": c.get("name"), "status": c.get("status")}
        for c in (payload.get("components") or [])
        if not c.get("group")
    ]
    return PlatformStatus(
        indicator=indicator,
        description=description,
        active_incidents=active,
        degraded_components=degraded,
        components=components,
    )


def _parse_incident_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _reduce_incident_days(
    payload: dict[str, Any], *, now: datetime | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Reduce raw incidents.json into one contiguous record per UTC calendar day.

    A multi-day incident (created_at .. resolved_at, or .. now if unresolved)
    marks every day it touched, not just its start day. When two incidents
    share a day, the day's impact is the worse of the two (see _worst_impact).
    Gaps between the earliest and latest touched day are filled with
    impact="none"/count=0 so a consumer gets a contiguous grid without having
    to fill gaps itself.
    """
    ref_now = now or datetime.now(UTC)
    by_day: dict[date, dict[str, Any]] = {}
    for inc in payload.get("incidents") or []:
        created = _parse_incident_timestamp(inc.get("created_at"))
        if created is None:
            continue
        resolved = _parse_incident_timestamp(inc.get("resolved_at"))
        end = resolved or ref_now  # unresolved incidents span through to today
        if end < created:
            end = created
        impact = str(inc.get("impact") or "none").lower()
        if impact not in _IMPACT_RANK:
            impact = "none"

        day = created.date()
        end_day = end.date()
        while day <= end_day:
            entry = by_day.get(day)
            if entry is None:
                by_day[day] = {"impact": impact, "count": 1}
            else:
                entry["impact"] = _worst_impact(entry["impact"], impact)
                entry["count"] += 1
            day += timedelta(days=1)

    if not by_day:
        return [], 0

    range_start, range_end = min(by_day), max(by_day)
    days: list[dict[str, Any]] = []
    cursor = range_start
    while cursor <= range_end:
        entry = by_day.get(cursor, {"impact": "none", "count": 0})
        days.append(
            {"date": cursor.isoformat(), "impact": entry["impact"], "count": entry["count"]}
        )
        cursor += timedelta(days=1)

    return days, (range_end - range_start).days + 1


def fetch_incident_days(
    timeout_s: float = STATUS_CACHE_TIMEOUT_S,
) -> tuple[list[dict[str, Any]], int]:
    """One-shot fetch + reduce of incidents.json.

    Best-effort like the rest of this module (Rule 20): any failure — network,
    HTTP error, malformed JSON, or an unexpected payload shape — returns
    ([], 0) rather than raising. This is enrichment on top of enrichment;
    it must never be able to take down the summary fetch it accompanies.
    """
    try:
        response = httpx.get(INCIDENTS_PAGE_URL, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
        return _reduce_incident_days(payload)
    except Exception:  # noqa: BLE001 -- best-effort enrichment, see docstring
        return [], 0


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
        "components": status.components,
        "incident_days": status.incident_days,
        "history_days": status.history_days,
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
        # .get() defaults handle a cache file written by a pre-history version
        # of this module, which has neither key.
        components=list(payload.get("components") or []),
        incident_days=list(payload.get("incident_days") or []),
        history_days=int(payload.get("history_days") or 0),
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

    The incidents.json fetch (component/incident-day history) rides along
    with the summary fetch ONLY when the summary itself is being fetched
    fresh — i.e. on a cache miss or force_refresh. A fresh-cache hit stays a
    zero-network call, same as before this history feature existed, so the
    hot path (`status` on every invocation) isn't made any slower.
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

    fresh.incident_days, fresh.history_days = fetch_incident_days(timeout_s=timeout_s)

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
        "components": status.components,
        "incident_days": status.incident_days,
        "history_days": status.history_days,
        "fetched_at": status.fetched_at.isoformat().replace("+00:00", "Z"),
        "age_seconds": int(status.age_seconds()),
        "fetch_error": status.fetch_error,
    }
