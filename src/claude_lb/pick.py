"""Pick algorithm (SPEC §9).

Filter ladder -> strategy sort -> return top 1 (or all).
Stickiness is applied BEFORE the strategy when enabled (default 300s).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from .models import Health, HealthCache, ProfileHealth
from .paths import last_pick_path, pick_log_path

log = logging.getLogger(__name__)

DEFAULT_STICKINESS_S = 300
PICK_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


class Strategy(str, Enum):
    STICKY = "sticky"
    LEAST_USED = "least-used"
    ROUND_ROBIN = "round-robin"
    WEIGHTED = "weighted"
    FIRST_HEALTHY = "first-healthy"


class PickFailureReason(str, Enum):
    NO_PROFILES = "no_profiles"
    ALL_AUTH_DEAD = "all_auth_dead"
    ALL_AUTH_EXPIRED = "all_auth_expired"
    ALL_WEEKLY = "all_weekly_limit"
    ALL_THROTTLED = "all_throttled"
    ALL_TERMINAL = "all_terminal"
    REQUIRE_OK_NONE = "require_ok_none"


@dataclass
class PickOutcome:
    """Result of pick(): either a chosen profile or a structured failure.

    `chosen` is the primary (first) pick; `chosen_many` is the ordered list of
    all picks (length 1 for single-pick, up to `count` for multi-pick). The
    two fields always agree: `chosen == chosen_many[0]` when ok is True.
    """

    chosen: ProfileHealth | None = None
    chosen_many: list[ProfileHealth] = field(default_factory=list)
    strategy_used: Strategy | None = None
    reason: PickFailureReason | None = None
    earliest_recovery_at: datetime | None = None
    rationale: str = ""

    @property
    def ok(self) -> bool:
        return self.chosen is not None


def _now() -> datetime:
    return datetime.now(UTC)


def _stickiness_seconds(override: int | None) -> int:
    if override is not None:
        return max(0, override)
    env = os.environ.get("CLAUDE_LB_STICKINESS")
    if env is not None:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return DEFAULT_STICKINESS_S


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        val = s
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        parsed = datetime.fromisoformat(val)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# ---------------------------------------------------------------------------
# Stickiness
# ---------------------------------------------------------------------------


def read_last_pick(path: Path | None = None) -> tuple[str, datetime] | None:
    target = path or last_pick_path()
    if not target.is_file():
        return None
    try:
        with target.open("rb") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    name = data.get("profile")
    ts = _parse_iso(data.get("timestamp"))
    if not isinstance(name, str) or ts is None:
        return None
    return name, ts


def write_last_pick(name: str, path: Path | None = None) -> None:
    target = path or last_pick_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"profile": name, "timestamp": _iso(_now())})
    fd, tmp = tempfile.mkstemp(prefix=".last-pick-", suffix=".json.tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Pick log (audit trail)
# ---------------------------------------------------------------------------


def append_pick_log(
    name: str,
    strategy: Strategy,
    score: float,
    path: Path | None = None,
) -> None:
    target = path or pick_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    line = f"{_iso(_now())}\t{name}\t{strategy.value}\tscore={score:.2f}\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line)
    _rotate_pick_log_if_needed(target)


def _rotate_pick_log_if_needed(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= PICK_LOG_MAX_BYTES:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return
    keep = lines[len(lines) // 2:]
    fd, tmp = tempfile.mkstemp(prefix=".picks-", suffix=".log.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Filter ladder
# ---------------------------------------------------------------------------


def _is_selectable(entry: ProfileHealth, now: datetime, require_ok: bool) -> bool:
    """Apply the filter ladder (SPEC §9). Returns True if the profile is
    still in play after dropping auth_dead / exhausted / throttled entries."""
    if entry.health is Health.AUTH_DEAD:
        return False
    if entry.health is Health.AUTH_EXPIRED:
        return False
    if entry.health is Health.WEEKLY_LIMIT:
        reset = entry.weekly_reset_at
        if reset is None or reset > now:
            return False
    if entry.health is Health.SESSION_LIMIT:
        reset = entry.session_reset_at
        if reset is None or reset > now:
            return False
    if entry.health is Health.RATE_LIMITED:
        exp = entry.expires_at
        if exp is None or exp > now:
            return False
    if require_ok and entry.health is not Health.OK:
        return False
    # UNKNOWN and NETWORK_ERROR pass the ladder; they're rare and we want
    # to try them rather than block a caller on transient conditions.
    return True


def _earliest_recovery(entries: list[ProfileHealth]) -> datetime | None:
    candidates: list[datetime] = []
    for e in entries:
        for ts in (e.weekly_reset_at, e.session_reset_at, e.expires_at):
            if ts is not None:
                candidates.append(ts)
    return min(candidates) if candidates else None


# ---------------------------------------------------------------------------
# Strategy sorts
# ---------------------------------------------------------------------------


def _sort_least_used(entries: list[ProfileHealth]) -> list[ProfileHealth]:
    def key(e: ProfileHealth) -> tuple[int, int, float]:
        health_rank = 0 if e.health is Health.OK else 1
        weekly_pct = (
            e.usage.weekly_pct if (e.usage and e.usage.weekly_pct is not None) else 0
        )
        # Prefer more recently probed to break ties (newer = negative for ascending sort)
        probed_ts = -(e.probed_at.timestamp())
        return (health_rank, weekly_pct, probed_ts)

    return sorted(entries, key=key)


def _sort_first_healthy(entries: list[ProfileHealth]) -> list[ProfileHealth]:
    return sorted(entries, key=lambda e: 0 if e.health is Health.OK else 1)


def _sort_weighted(entries: list[ProfileHealth]) -> list[ProfileHealth]:
    def key(e: ProfileHealth) -> tuple[int, float]:
        health_rank = 0 if e.health is Health.OK else 1
        weekly = float(e.usage.weekly_pct) if (e.usage and e.usage.weekly_pct is not None) else 0.0
        session = float(e.usage.session_pct) if (e.usage and e.usage.session_pct is not None) else 0.0
        # Lower score is better: divide weekly by (session+1) so low-session beats low-weekly alone.
        score = weekly / (session + 1.0)
        return (health_rank, score)

    return sorted(entries, key=key)


def _sort_round_robin(
    entries: list[ProfileHealth],
    last_pick_ts: datetime | None,
    last_pick_name: str | None,
) -> list[ProfileHealth]:
    """Push the last-picked profile to the back; preserve prior order otherwise."""
    # Health first, then de-prioritise the previously-picked name.
    def key(e: ProfileHealth) -> tuple[int, int]:
        health_rank = 0 if e.health is Health.OK else 1
        stale = 1 if (last_pick_name and e.name == last_pick_name) else 0
        return (health_rank, stale)

    return sorted(entries, key=key)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def pick(
    cache: HealthCache,
    discovered_names: list[str],
    *,
    strategy: Strategy = Strategy.STICKY,
    stickiness_s: int | None = None,
    require_ok: bool = False,
    count: int = 1,
    now: datetime | None = None,
    last_pick_path_override: Path | None = None,
) -> PickOutcome:
    """Choose the best profile given cache + discovered names.

    `discovered_names` provides the authoritative list; entries absent from
    the cache are treated as health=UNKNOWN but still considered pickable
    (callers should probe first for good results).

    When `count > 1`, returns up to `count` profiles in `chosen_many` (strategy
    order). Stickiness is ignored for count > 1 — sticky is a "keep returning
    the same profile" semantic that doesn't compose with multi-pick. If fewer
    than `count` candidates pass the filter ladder, returns what we have; the
    caller decides whether partial fulfilment is acceptable.
    """
    now = now or _now()
    if count < 1:
        count = 1

    # Materialise ProfileHealth entries in discovery order, including stubs
    # for profiles without a cache record.
    entries: list[ProfileHealth] = []
    for name in discovered_names:
        entry = cache.profiles.get(name)
        if entry is None:
            entry = ProfileHealth(name=name, health=Health.UNKNOWN, probed_at=now)
        entries.append(entry)

    if not entries:
        return PickOutcome(reason=PickFailureReason.NO_PROFILES)

    # Failure-mode diagnostics if no one passes the ladder.
    selectable = [e for e in entries if _is_selectable(e, now, require_ok)]

    if not selectable:
        return _diagnose_failure(entries, require_ok)

    # Stickiness pre-check — only when strategy is STICKY AND single-pick.
    # Multi-pick (count > 1) ignores stickiness: "keep returning the same
    # profile" doesn't compose with "give me N distinct profiles."
    if strategy is Strategy.STICKY and count == 1:
        stickiness = _stickiness_seconds(stickiness_s)
        if stickiness > 0:
            last = read_last_pick(last_pick_path_override)
            if last is not None:
                last_name, last_ts = last
                delta = (now - last_ts).total_seconds()
                if 0 <= delta < stickiness:
                    for entry in selectable:
                        if entry.name == last_name and entry.health is Health.OK:
                            return PickOutcome(
                                chosen=entry,
                                chosen_many=[entry],
                                strategy_used=Strategy.STICKY,
                                rationale=(
                                    f"sticky: last pick within {stickiness}s window"
                                ),
                            )

    # Fall through to the underlying sort.
    fallback = Strategy.LEAST_USED if strategy is Strategy.STICKY else strategy

    if fallback is Strategy.ROUND_ROBIN:
        rr_last = read_last_pick(last_pick_path_override)
        rr_last_name = rr_last[0] if rr_last else None
        ordered = _sort_round_robin(selectable, None, rr_last_name)
        rationale = "round-robin: rotated past last pick"
    elif fallback is Strategy.WEIGHTED:
        ordered = _sort_weighted(selectable)
        rationale = "weighted: combined weekly + session usage"
    elif fallback is Strategy.FIRST_HEALTHY:
        ordered = _sort_first_healthy(selectable)
        rationale = "first-healthy: first ok in discovery order"
    else:
        ordered = _sort_least_used(selectable)
        rationale = "least-used: lowest weekly usage among healthy"

    top = ordered[:count]
    chosen = top[0]
    return PickOutcome(
        chosen=chosen,
        chosen_many=top,
        strategy_used=fallback if strategy is Strategy.STICKY else strategy,
        rationale=rationale,
    )


def _diagnose_failure(
    entries: list[ProfileHealth],
    require_ok: bool,
) -> PickOutcome:
    """Pick couldn't choose — figure out why, for a useful exit code."""
    if not entries:
        return PickOutcome(reason=PickFailureReason.NO_PROFILES)
    if require_ok:
        return PickOutcome(
            reason=PickFailureReason.REQUIRE_OK_NONE,
            earliest_recovery_at=_earliest_recovery(entries),
        )
    if all(e.health is Health.AUTH_DEAD for e in entries):
        return PickOutcome(reason=PickFailureReason.ALL_AUTH_DEAD)
    if all(e.health is Health.AUTH_EXPIRED for e in entries):
        return PickOutcome(reason=PickFailureReason.ALL_AUTH_EXPIRED)
    if all(e.health in (Health.AUTH_DEAD, Health.AUTH_EXPIRED) for e in entries):
        return PickOutcome(reason=PickFailureReason.ALL_AUTH_EXPIRED)
    if all(e.health is Health.WEEKLY_LIMIT for e in entries):
        return PickOutcome(
            reason=PickFailureReason.ALL_WEEKLY,
            earliest_recovery_at=_earliest_recovery(entries),
        )
    if all(e.health in (Health.RATE_LIMITED, Health.SESSION_LIMIT) for e in entries):
        return PickOutcome(
            reason=PickFailureReason.ALL_THROTTLED,
            earliest_recovery_at=_earliest_recovery(entries),
        )
    return PickOutcome(
        reason=PickFailureReason.ALL_TERMINAL,
        earliest_recovery_at=_earliest_recovery(entries),
    )
