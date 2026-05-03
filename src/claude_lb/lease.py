"""Profile lease registry — prevents rotation of in-flight credentials.

A lease pins a profile against `roost refresh` for a bounded TTL. Long-lived
consumers (e.g. a 30-min Axiom trial) acquire a lease before starting so that
a background probe cadence cannot rotate the refresh_token out from under them.

Storage: `<config>/leases.json` — atomic tempfile+os.replace writes, same
pattern as health.json. Reads are unlocked; the worst case is a reader seeing
a slightly-stale snapshot, which is safe because leases are checked at the
start of refresh (not mid-write).

Short-lived consumers (one-shot pick → exec in seconds) don't need leases —
the access token is valid for hours, no rotation can happen that fast.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import ensure_config_dir, leases_path

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

DEFAULT_DURATION_S = 30 * 60  # 30 minutes


@dataclass
class Lease:
    """A single active lease record."""

    lease_id: str
    profile: str
    expires_at: datetime
    created_at: datetime
    creator: str  # sys.argv[0] or explicit label

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return now < exp

    def to_dict(self) -> dict:
        return {
            "lease_id": self.lease_id,
            "profile": self.profile,
            "expires_at": self.expires_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "creator": self.creator,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        def _parse_dt(v: str) -> datetime:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt

        return cls(
            lease_id=d["lease_id"],
            profile=d["profile"],
            expires_at=_parse_dt(d["expires_at"]),
            created_at=_parse_dt(d["created_at"]),
            creator=d.get("creator", ""),
        )


def _load_raw(path: Path | None = None) -> dict[str, dict]:
    """Load the lease store; returns {} on any error."""
    target = path or leases_path()
    if not target.is_file():
        return {}
    try:
        with target.open("rb") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return {}
        return raw
    except (OSError, ValueError) as exc:
        log.warning("Failed to read leases at %s (%s); treating as empty", target, exc)
        return {}


def _save_raw(data: dict[str, dict], path: Path | None = None) -> None:
    """Atomically write the lease store."""
    target = path or leases_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".leases-", suffix=".json.tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def acquire(
    profile: str,
    duration_s: int = DEFAULT_DURATION_S,
    creator: str | None = None,
    path: Path | None = None,
) -> Lease:
    """Acquire a lease on *profile* for *duration_s* seconds.

    Always succeeds — if a lease already exists for the profile it is
    replaced (callers that need to detect double-acquisition should call
    get_active first). The old lease is simply superseded.
    """
    ensure_config_dir()
    now = datetime.now(UTC)
    lease = Lease(
        lease_id=f"lease-{profile}-{uuid.uuid4().hex[:8]}",
        profile=profile,
        expires_at=now + timedelta(seconds=duration_s),
        created_at=now,
        creator=creator or _default_creator(),
    )
    data = _load_raw(path)
    # Index by profile so each profile has at most one active lease.
    # Multiple concurrent leases on the same profile would only be useful
    # with sub-TTL granularity, which isn't a real use case here.
    data[profile] = lease.to_dict()
    _save_raw(data, path)
    log.debug("Lease acquired: %s on %s for %ds", lease.lease_id, profile, duration_s)
    return lease


def release(lease_id: str, path: Path | None = None) -> bool:
    """Release a lease by ID. Idempotent — returns False if not found/already gone."""
    data = _load_raw(path)
    # Find which profile this lease_id belongs to.
    to_remove = [
        prof for prof, rec in data.items() if rec.get("lease_id") == lease_id
    ]
    if not to_remove:
        return False
    for prof in to_remove:
        del data[prof]
    _save_raw(data, path)
    log.debug("Lease released: %s", lease_id)
    return True


def release_by_profile(profile: str, path: Path | None = None) -> bool:
    """Release whatever lease is held on *profile*, if any."""
    data = _load_raw(path)
    if profile not in data:
        return False
    del data[profile]
    _save_raw(data, path)
    return True


def get_active(profile: str, path: Path | None = None) -> Lease | None:
    """Return the active lease for *profile*, or None if none/expired."""
    data = _load_raw(path)
    rec = data.get(profile)
    if not rec:
        return None
    try:
        lease = Lease.from_dict(rec)
    except (KeyError, ValueError):
        return None
    return lease if lease.is_active() else None


def list_leases(
    include_expired: bool = False, path: Path | None = None
) -> list[Lease]:
    """Return all leases; by default only active ones."""
    data = _load_raw(path)
    leases = []
    for rec in data.values():
        try:
            lease = Lease.from_dict(rec)
        except (KeyError, ValueError):
            continue
        if include_expired or lease.is_active():
            leases.append(lease)
    return sorted(leases, key=lambda l: l.expires_at)


def purge_expired(path: Path | None = None) -> int:
    """Remove expired leases from the store. Returns count removed."""
    data = _load_raw(path)
    now = datetime.now(UTC)
    expired_keys = []
    for prof, rec in data.items():
        try:
            lease = Lease.from_dict(rec)
        except (KeyError, ValueError):
            expired_keys.append(prof)
            continue
        if not lease.is_active(now):
            expired_keys.append(prof)
    if not expired_keys:
        return 0
    for k in expired_keys:
        del data[k]
    _save_raw(data, path)
    return len(expired_keys)


def _default_creator() -> str:
    try:
        return os.path.basename(sys.argv[0])
    except Exception:
        return "roost"


def parse_duration(s: str) -> int:
    """Parse a duration string like '30m', '2h', '90s' into seconds.

    Accepts plain integers (treated as seconds), or a number followed by
    s/m/h. Raises ValueError on unrecognised format.
    """
    s = s.strip()
    if not s:
        raise ValueError("empty duration")
    if s.isdigit():
        return int(s)
    unit = s[-1].lower()
    try:
        value = int(s[:-1])
    except ValueError:
        raise ValueError(f"invalid duration: {s!r}") from None
    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    raise ValueError(f"unknown duration unit {unit!r} in {s!r}; use s/m/h")
