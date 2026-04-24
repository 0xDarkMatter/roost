"""Async probe of Anthropic /v1/models (SPEC §7).

One `probe_profile()` per profile. `probe_many()` runs them concurrently
with a single `httpx.AsyncClient`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from . import __version__
from .models import Health, Profile, ProfileHealth
from .taxonomy import ProbeInput, classify, compute_expires_at

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TIMEOUT_S = 10.0


def _now() -> datetime:
    return datetime.now(UTC)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "accept": "application/json",
        "user-agent": f"claude-lb/{__version__}",
    }


def _classify_exception(exc: BaseException) -> ProbeInput:
    """Map an httpx exception into a ProbeInput with an `exception_kind`."""
    name = type(exc).__name__
    message = str(exc)
    if isinstance(exc, httpx.TimeoutException):
        kind = "timeout"
    elif isinstance(exc, httpx.ConnectError):
        kind = "refused"
    elif isinstance(exc, (httpx.NetworkError, httpx.TransportError)):
        kind = "network"
    else:
        kind = "other"
    log.debug("Probe exception %s: %s", name, message)
    return ProbeInput(exception_kind=kind, exception_message=message or name)


async def _probe_once(
    client: httpx.AsyncClient,
    profile: Profile,
    timeout: float,
) -> tuple[ProbeInput, int]:
    """Execute one probe. Returns (ProbeInput, latency_ms)."""
    start = asyncio.get_event_loop().time()
    try:
        response = await client.get(
            API_URL,
            headers=_headers(profile.access_token),
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        return _classify_exception(exc), latency_ms

    latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)
    body: dict[str, Any] | None = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = None

    probe = ProbeInput(
        status_code=response.status_code,
        body=body,
        headers={k.lower(): v for k, v in response.headers.items()},
    )
    return probe, latency_ms


def _classification_to_health(
    profile: Profile,
    probe: ProbeInput,
    latency_ms: int,
    prev_health: Health | None,
) -> ProfileHealth:
    probed_at = _now()
    result = classify(probe, probed_at=probed_at, prev_health=prev_health)
    expires = compute_expires_at(result, probed_at)
    return ProfileHealth(
        name=profile.name,
        health=result.health,
        probed_at=probed_at,
        expires_at=expires,
        error=result.error,
        retry_after_s=result.retry_after_s,
        session_reset_at=result.session_reset_at,
        weekly_reset_at=result.weekly_reset_at,
        probe_latency_ms=latency_ms,
        credentials_mtime=profile.credentials_mtime,
    )


async def probe_profile(
    profile: Profile,
    *,
    prev_health: Health | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> ProfileHealth:
    """Probe a single profile, returning a ProfileHealth record."""
    if client is not None:
        probe, latency_ms = await _probe_once(client, profile, timeout)
    else:
        async with httpx.AsyncClient() as c:
            probe, latency_ms = await _probe_once(c, profile, timeout)
    return _classification_to_health(profile, probe, latency_ms, prev_health)


async def probe_many(
    profiles: list[Profile],
    *,
    prev_health: dict[str, Health] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[ProfileHealth]:
    """Probe all profiles concurrently; returns results in input order."""
    prev_health = prev_health or {}
    if not profiles:
        return []
    async with httpx.AsyncClient() as client:
        tasks = [
            _probe_once(client, p, timeout) for p in profiles
        ]
        pairs = await asyncio.gather(*tasks, return_exceptions=False)
    out: list[ProfileHealth] = []
    for profile, (probe, latency_ms) in zip(profiles, pairs):
        out.append(
            _classification_to_health(
                profile,
                probe,
                latency_ms,
                prev_health.get(profile.name),
            )
        )
    return out


def probe_many_sync(
    profiles: list[Profile],
    *,
    prev_health: dict[str, Health] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[ProfileHealth]:
    """Blocking wrapper around probe_many(). Convenient for CLI callers."""
    return asyncio.run(probe_many(profiles, prev_health=prev_health, timeout=timeout))
