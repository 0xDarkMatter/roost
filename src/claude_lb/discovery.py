"""Profile discovery + OAuth token extraction (SPEC §10).

Walks the configured profiles directory, parses each `.credentials.json`,
and extracts an access token using a three-shape fallback order. Also
provides directory-level mutation helpers (`remove_profile_dir`,
`rename_profile_dir`) used by the `remove` and `rename` CLI commands —
both keep the multi-profile layout in sync with the cache and last-pick
state by leaving cleanup to the caller (see cli.py).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Profile
from .paths import profiles_dir, single_profile_fallback

log = logging.getLogger(__name__)

# Profile directory names must match [a-zA-Z0-9_-]+ (same constraint as the
# claude CLI's profile system). Non-matching dirs are skipped silently.
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Token extraction order (first match wins). Robust against format drift.
_TOKEN_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claudeAiOauth.accessToken", ("claudeAiOauth", "accessToken")),
    ("oauthAccessToken", ("oauthAccessToken",)),
    ("accessToken", ("accessToken",)),
)


def _get_nested(obj: Any, path: tuple[str, ...]) -> str | None:
    """Walk a dotted path through a nested dict; return a string or None."""
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, str) and cur else None


def _extract_token(payload: dict[str, Any]) -> tuple[str | None, str]:
    """Return (token, source_label) or (None, '') if no shape matched."""
    for label, path in _TOKEN_PATHS:
        token = _get_nested(payload, path)
        if token:
            return token, label
    return None, ""


def _read_credentials(path: Path) -> dict[str, Any] | None:
    """Read and parse a .credentials.json file. Return None on any failure."""
    try:
        with path.open("rb") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.debug("Failed to read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _expires_at_from(payload: dict[str, Any]) -> datetime | None:
    """Read `.claudeAiOauth.expiresAt` (unix ms) → UTC datetime, or None."""
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    value = oauth.get("expiresAt")
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _has_refresh_token(payload: dict[str, Any]) -> bool:
    """Detect a usable refresh token in the modern credentials shape."""
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return False
    rt = oauth.get("refreshToken")
    return isinstance(rt, str) and bool(rt.strip())


def _subscription_type(payload: dict[str, Any]) -> str | None:
    """Read `.claudeAiOauth.subscriptionType` (e.g. 'max', 'team', 'pro').

    Anthropic has been observed to return this lowercase; we normalise to
    lowercase defensively so table rendering stays consistent if the shape
    ever drifts.
    """
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    value = oauth.get("subscriptionType")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _make_profile(name: str, cred_path: Path) -> Profile | None:
    """Build a Profile from a credentials file, or return None if unusable."""
    payload = _read_credentials(cred_path)
    if payload is None:
        return None
    token, source = _extract_token(payload)
    if token is None:
        log.debug("No recognised token shape in %s", cred_path)
        return None
    try:
        mtime = cred_path.stat().st_mtime
    except OSError:  # pragma: no cover  -- race: file existed at is_file() but vanished by stat()
        mtime = 0.0
    return Profile(
        name=name,
        access_token=token,
        credentials_path=str(cred_path),
        credentials_mtime=mtime,
        token_source=source,
        access_token_expires_at=_expires_at_from(payload),
        refresh_token_present=_has_refresh_token(payload),
        subscription_type=_subscription_type(payload),
    )


def _discover_in(directory: Path) -> list[Profile]:
    """Discover all valid profiles in `~/.claude-profiles/`-shaped dir."""
    if not directory.is_dir():
        return []
    profiles: list[Profile] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        if not PROFILE_NAME_RE.match(entry.name):
            continue
        cred = entry / ".credentials.json"
        if not cred.is_file():
            continue
        profile = _make_profile(entry.name, cred)
        if profile is not None:
            profiles.append(profile)
    return profiles


def discover_profiles() -> list[Profile]:
    """Return all discovered profiles in priority order.

    1. $CLAUDE_LB_PROFILES_DIR (if set)
    2. ~/.claude-profiles/
    3. $CLAUDE_CONFIG_DIR/.credentials.json as a single `default` profile
    """
    primary = _discover_in(profiles_dir())
    if primary:
        return primary

    fallback_dir = single_profile_fallback()
    if fallback_dir is not None:
        cred = fallback_dir / ".credentials.json"
        profile = _make_profile("default", cred)
        if profile is not None:  # pragma: no branch  -- single_profile_fallback already validates is_file()
            return [profile]

    return []


def discover_names() -> list[str]:
    """Return just the names of discovered profiles (fast, no token reads)."""
    return [p.name for p in discover_profiles()]


def get_profile(name: str) -> Profile | None:
    """Look up a single profile by name. Returns None if not found."""
    for profile in discover_profiles():
        if profile.name == name:
            return profile
    return None


# ---------------------------------------------------------------------------
# Directory mutation (used by CLI `remove` / `rename`)
# ---------------------------------------------------------------------------


@dataclass
class MutationResult:
    """Outcome of a directory-level profile mutation.

    Designed to be JSON-serialisable from the CLI layer without inventing a
    second result type per command. `error_code` matches the `add` command's
    vocabulary (NOT_FOUND, CONFLICT, VALIDATION_ERROR, ERROR) so callers can
    map cleanly to exit codes.
    """

    ok: bool
    name: str
    path: Path | None = None
    error_code: str | None = None
    error_message: str | None = None


def remove_profile_dir(name: str) -> MutationResult:
    """Recursively delete `<profiles_dir>/<name>/`.

    Symmetric counterpart to the CLI `add` command. Validates the name shape
    against `PROFILE_NAME_RE` first so a typo can't accidentally rmtree the
    wrong location, then refuses if the directory is missing or actually a
    file. Cache invalidation and last-pick cleanup are the caller's job; this
    helper only owns the filesystem dir.
    """
    if not PROFILE_NAME_RE.match(name):
        return MutationResult(
            ok=False,
            name=name,
            error_code="VALIDATION_ERROR",
            error_message=f"invalid profile name: {name!r} (must match [A-Za-z0-9_-]+)",
        )
    target = profiles_dir() / name
    if not target.exists():
        return MutationResult(
            ok=False,
            name=name,
            error_code="NOT_FOUND",
            error_message=f"no such profile directory: {target}",
        )
    if not target.is_dir():
        return MutationResult(
            ok=False,
            name=name,
            path=target,
            error_code="VALIDATION_ERROR",
            error_message=f"profile path is not a directory: {target}",
        )
    try:
        shutil.rmtree(target)
    except OSError as exc:
        return MutationResult(
            ok=False,
            name=name,
            path=target,
            error_code="ERROR",
            error_message=f"rmtree failed: {exc}",
        )
    return MutationResult(ok=True, name=name, path=target)


def rename_profile_dir(old: str, new: str, *, force: bool = False) -> MutationResult:
    """Move `<profiles_dir>/<old>/` to `<profiles_dir>/<new>/`.

    Validates both names. Refuses if `old` is missing, if `new` already
    exists (unless `force`), or if either name fails the discovery regex.
    With `force`, an existing destination is removed first so the rename
    proceeds — this matches the symmetry of `add --force` overwrites.

    Cache invalidation is the caller's job. New name will discover fresh on
    the next probe cycle (mtime carries over via the move).
    """
    if not PROFILE_NAME_RE.match(old):
        return MutationResult(
            ok=False,
            name=old,
            error_code="VALIDATION_ERROR",
            error_message=f"invalid old name: {old!r} (must match [A-Za-z0-9_-]+)",
        )
    if not PROFILE_NAME_RE.match(new):
        return MutationResult(
            ok=False,
            name=new,
            error_code="VALIDATION_ERROR",
            error_message=f"invalid new name: {new!r} (must match [A-Za-z0-9_-]+)",
        )
    if old == new:
        return MutationResult(
            ok=False,
            name=new,
            error_code="VALIDATION_ERROR",
            error_message="old and new names are identical",
        )
    base = profiles_dir()
    src = base / old
    dst = base / new
    if not src.is_dir():
        return MutationResult(
            ok=False,
            name=old,
            error_code="NOT_FOUND",
            error_message=f"no such profile directory: {src}",
        )
    if dst.exists():
        if not force:
            return MutationResult(
                ok=False,
                name=new,
                path=dst,
                error_code="CONFLICT",
                error_message=(
                    f"destination already exists: {dst}. Pass --force to overwrite."
                ),
            )
        try:
            shutil.rmtree(dst)
        except OSError as exc:
            return MutationResult(
                ok=False,
                name=new,
                path=dst,
                error_code="ERROR",
                error_message=f"could not remove existing destination: {exc}",
            )
    try:
        src.rename(dst)
    except OSError as exc:
        return MutationResult(
            ok=False,
            name=new,
            path=dst,
            error_code="ERROR",
            error_message=f"rename failed: {exc}",
        )
    return MutationResult(ok=True, name=new, path=dst)
