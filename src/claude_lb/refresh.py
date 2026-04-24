"""OAuth token refresh (SPEC §10).

POSTs to Anthropic's OAuth token endpoint with the stored refresh_token,
receives a new access_token + rotated refresh_token, and atomically rewrites
`.credentials.json` in place. All other fields in the credentials file are
preserved — we only touch `claudeAiOauth.accessToken`, `.refreshToken`, and
`.expiresAt`.

Token URL and client_id come from the Claude Code OAuth flow (observed in
`~/.claude/` state and corroborated by open-source Claude proxy clients).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .models import Profile

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code public OAuth client
ANTHROPIC_BETA = "oauth-2025-04-20"
DEFAULT_TIMEOUT_S = 10.0


@dataclass
class RefreshResult:
    """Outcome of a single profile refresh."""

    name: str
    refreshed: bool
    previous_expires_at: datetime | None = None
    new_expires_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"claude-lb/{__version__}",
        "anthropic-beta": ANTHROPIC_BETA,
    }


def _read_credentials(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.debug("Failed to read %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _extract_refresh_token(payload: dict[str, Any]) -> str | None:
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    rt = oauth.get("refreshToken")
    return rt.strip() if isinstance(rt, str) and rt.strip() else None


def _atomic_write_credentials(path: Path, payload: dict[str, Any]) -> None:
    """Atomically rewrite .credentials.json preserving all non-oauth fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".credentials-",
        suffix=".json.tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _apply_token_response(
    payload: dict[str, Any],
    token_response: dict[str, Any],
) -> tuple[dict[str, Any], datetime | None]:
    """Mutate the credentials payload with the new token, return (payload, new_expires_at)."""
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = {}
    access = token_response.get("access_token")
    refresh = token_response.get("refresh_token")
    expires_in = token_response.get("expires_in")
    if isinstance(access, str) and access:
        oauth["accessToken"] = access
    if isinstance(refresh, str) and refresh:
        oauth["refreshToken"] = refresh
    new_expires_at: datetime | None = None
    if isinstance(expires_in, (int, float)):
        new_expires_at = _now().replace(microsecond=0)
        oauth["expiresAt"] = int((new_expires_at.timestamp() + float(expires_in)) * 1000)
        new_expires_at = datetime.fromtimestamp(
            oauth["expiresAt"] / 1000.0, tz=UTC
        )
    payload["claudeAiOauth"] = oauth
    return payload, new_expires_at


async def _post_refresh(
    client: httpx.AsyncClient,
    refresh_token: str,
    timeout: float,
) -> tuple[int, dict[str, Any] | None, str]:
    """POST the refresh request. Returns (status_code, json_body_or_none, raw_text)."""
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    try:
        resp = await client.post(
            TOKEN_URL,
            json=body,
            headers=_headers(),
            timeout=timeout,
        )
    except httpx.TimeoutException:
        return 0, None, "timeout"
    except httpx.HTTPError as exc:
        return 0, None, f"network:{type(exc).__name__}:{exc}"
    text = resp.text
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        parsed = None
    return resp.status_code, parsed, text


async def refresh_profile(
    profile: Profile,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> RefreshResult:
    """Refresh one profile's OAuth token. Rewrites .credentials.json on success."""
    path = Path(profile.credentials_path)
    payload = _read_credentials(path)
    if payload is None:
        return RefreshResult(
            name=profile.name,
            refreshed=False,
            previous_expires_at=profile.access_token_expires_at,
            error_code="UNREADABLE",
            error_message=f"Could not read {path}",
        )
    rt = _extract_refresh_token(payload)
    if rt is None:
        return RefreshResult(
            name=profile.name,
            refreshed=False,
            previous_expires_at=profile.access_token_expires_at,
            error_code="NO_REFRESH_TOKEN",
            error_message="No refresh token stored; run `claude login`",
        )

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        status, body, raw = await _post_refresh(client, rt, timeout)
    finally:
        if owns_client:
            await client.aclose()

    if status == 0:
        return RefreshResult(
            name=profile.name,
            refreshed=False,
            previous_expires_at=profile.access_token_expires_at,
            error_code="NETWORK_ERROR",
            error_message=raw or "network error",
        )
    if status == 401 or status == 400:
        err = (body or {}).get("error") if isinstance(body, dict) else None
        msg = ""
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("type") or "")
        elif isinstance(err, str):
            msg = err
        return RefreshResult(
            name=profile.name,
            refreshed=False,
            previous_expires_at=profile.access_token_expires_at,
            error_code="REFRESH_REJECTED",
            error_message=msg or f"HTTP {status}: refresh token rejected — run `claude login`",
        )
    if status != 200 or body is None:
        return RefreshResult(
            name=profile.name,
            refreshed=False,
            previous_expires_at=profile.access_token_expires_at,
            error_code="UNEXPECTED_RESPONSE",
            error_message=f"HTTP {status}",
        )

    new_payload, new_expires = _apply_token_response(payload, body)
    try:
        _atomic_write_credentials(path, new_payload)
    except OSError as exc:
        return RefreshResult(
            name=profile.name,
            refreshed=False,
            previous_expires_at=profile.access_token_expires_at,
            error_code="WRITE_FAILED",
            error_message=str(exc),
        )

    return RefreshResult(
        name=profile.name,
        refreshed=True,
        previous_expires_at=profile.access_token_expires_at,
        new_expires_at=new_expires,
    )


async def refresh_many(
    profiles: list[Profile],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[RefreshResult]:
    """Refresh multiple profiles concurrently."""
    if not profiles:
        return []
    async with httpx.AsyncClient() as client:
        tasks = [refresh_profile(p, timeout=timeout, client=client) for p in profiles]
        return list(await asyncio.gather(*tasks, return_exceptions=False))


def refresh_many_sync(
    profiles: list[Profile],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[RefreshResult]:
    """Blocking wrapper for CLI callers."""
    return asyncio.run(refresh_many(profiles, timeout=timeout))
