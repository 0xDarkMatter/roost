"""Health cache read/write (SPEC §8).

Schema:
    {
      "schema_version": 1,
      "updated_at": "<iso-utc>",
      "profiles": { "<name>": { ... ProfileHealth ... } }
    }

Writes are atomic (tempfile + os.replace in the same directory).
Reads are unlocked — worst case, a reader sees slightly-stale data and the
caller re-probes. AUTH_DEAD entries never expire; a credentials.json mtime
bump implicitly invalidates that profile's entry.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .models import HealthCache, ProfileHealth
from .paths import cache_path, ensure_config_dir

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _empty_cache() -> HealthCache:
    return HealthCache(updated_at=_now(), profiles={})


def load_cache(path: Path | None = None) -> HealthCache:
    """Load the cache from disk. Returns an empty cache on any error."""
    target = path or cache_path()
    if not target.is_file():
        return _empty_cache()
    try:
        with target.open("rb") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Failed to read cache at %s (%s); starting fresh", target, exc)
        return _empty_cache()
    try:
        return HealthCache.model_validate(raw)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Cache at %s has invalid schema (%s); starting fresh", target, exc)
        return _empty_cache()


def save_cache(cache: HealthCache, path: Path | None = None) -> None:
    """Write the cache to disk atomically (tempfile + os.replace)."""
    target = path or cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    cache.updated_at = _now()
    payload = cache.model_dump_json(indent=2)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".health-",
        suffix=".json.tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover  -- fsync is best-effort; OS-dependent
                pass
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover  -- cleanup-of-cleanup
            pass
        raise


def upsert_profile(
    name: str,
    health: ProfileHealth,
    path: Path | None = None,
) -> HealthCache:
    """Load cache, set one profile's entry, save, return the updated cache."""
    ensure_config_dir()
    cache = load_cache(path)
    cache.profiles[name] = health
    save_cache(cache, path)
    return cache


def remove_profile(name: str, path: Path | None = None) -> bool:
    """Drop a profile's cache entry. Returns True if removed, False if absent."""
    cache = load_cache(path)
    if name not in cache.profiles:
        return False
    del cache.profiles[name]
    save_cache(cache, path)
    return True


# Exponential backoff for NETWORK_ERROR. Doubles per consecutive failure,
# starting at 30s and capped at 480s (8 min). Keeps the "fail open, retry
# eventually" spirit but prevents thundering-herd against a flapping endpoint
# without forcing the operator to invalidate the cache by hand.
NETWORK_BACKOFF_BASE_S = 30
NETWORK_BACKOFF_CAP_S = 480


def network_backoff_seconds(consecutive_failures: int) -> int:
    """Return the TTL (seconds) for a NETWORK_ERROR entry given its failure
    counter. 30s, 60s, 120s, 240s, 480s, 480s, ... — capped at 480s."""
    n = max(0, consecutive_failures)
    return min(NETWORK_BACKOFF_BASE_S * (2 ** n), NETWORK_BACKOFF_CAP_S)


def is_entry_fresh(
    entry: ProfileHealth,
    credentials_mtime: float | None = None,
    now: datetime | None = None,
) -> bool:
    """An entry is fresh iff:
      - expires_at is in the future (or None, meaning infinite — auth_dead),
      - AND the credentials file's mtime hasn't changed since the entry was cached.
      - For NETWORK_ERROR, exponential backoff overrides the static 30s TTL
        once consecutive_failures > 0 — a flapping endpoint doesn't get
        re-probed every 30s.

    A None expires_at ONLY means "never expires" for AUTH_DEAD. For transient
    states with explicit None expires_at (e.g. shouldn't happen but defensive),
    treat as stale.
    """
    now = now or _now()

    # mtime-based implicit invalidation
    if (
        credentials_mtime is not None
        and entry.credentials_mtime is not None
        and credentials_mtime != entry.credentials_mtime
    ):
        return False

    # AUTH_DEAD: infinite TTL
    from .models import Health

    if entry.health is Health.AUTH_DEAD:
        return True

    # NETWORK_ERROR with backoff: extend TTL based on consecutive_failures.
    # Treats `probed_at + backoff_seconds` as the effective expiry, so a
    # repeated network failure waits longer before the next probe attempt.
    if entry.health is Health.NETWORK_ERROR and entry.consecutive_failures > 0:
        backoff = network_backoff_seconds(entry.consecutive_failures)
        probed = entry.probed_at
        if probed.tzinfo is None:
            probed = probed.replace(tzinfo=UTC)
        return (now - probed).total_seconds() < backoff

    if entry.expires_at is None:
        return False

    expires = entry.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return now < expires
