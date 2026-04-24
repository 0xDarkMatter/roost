"""Async probe of Anthropic /api/oauth/usage (SPEC §7).

One `probe_profile()` per profile. `probe_many()` runs them concurrently
with a single `httpx.AsyncClient`.

The probe endpoint accepts Claude Code Max OAuth tokens (unlike /v1/* which
requires an API key) when the `anthropic-beta: oauth-2025-04-20` header is
set. The response body contains live utilization percentages and real reset
timestamps — no keyword scraping of 429 error messages required.
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

API_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_BETA = "oauth-2025-04-20"
DEFAULT_TIMEOUT_S = 10.0


def _now() -> datetime:
    return datetime.now(UTC)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": ANTHROPIC_BETA,
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
        usage=result.usage,
        probe_latency_ms=latency_ms,
        credentials_mtime=profile.credentials_mtime,
    )


def _local_auth_expired(profile: Profile) -> ProfileHealth | None:
    """Return an AUTH_EXPIRED record without hitting the network when the
    stored access token has already expired. Saves a round-trip and gives
    the caller a distinct signal from AUTH_DEAD (refresh token still valid).
    """
    exp = profile.access_token_expires_at
    if exp is None or exp > _now():
        return None
    from .models import ErrorInfo  # local to avoid cycle pressure
    probed_at = _now()
    delta_s = int((probed_at - exp).total_seconds())
    return ProfileHealth(
        name=profile.name,
        health=Health.AUTH_EXPIRED,
        probed_at=probed_at,
        expires_at=None,  # mtime bump on refresh invalidates it
        error=ErrorInfo(
            type="token_expired",
            message=(
                f"OAuth access token expired {delta_s}s ago. "
                f"Run: claude-lb refresh {profile.name}"
                if profile.refresh_token_present
                else f"OAuth access token expired {delta_s}s ago and no refresh "
                f"token is stored. Run: claude login --profile {profile.name}"
            ),
        ),
        probe_latency_ms=0,
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
    expired = _local_auth_expired(profile)
    if expired is not None:
        return expired
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
    """Probe all profiles concurrently; returns results in input order.

    Profiles whose stored access token has already expired are classified
    as AUTH_EXPIRED locally, without a network round-trip.
    """
    prev_health = prev_health or {}
    if not profiles:
        return []

    expired_results: dict[str, ProfileHealth] = {}
    to_probe: list[Profile] = []
    for p in profiles:
        expired = _local_auth_expired(p)
        if expired is not None:
            expired_results[p.name] = expired
        else:
            to_probe.append(p)

    network_results: dict[str, ProfileHealth] = {}
    if to_probe:
        async with httpx.AsyncClient() as client:
            tasks = [_probe_once(client, p, timeout) for p in to_probe]
            pairs = await asyncio.gather(*tasks, return_exceptions=False)
        for profile, (probe, latency_ms) in zip(to_probe, pairs):
            network_results[profile.name] = _classification_to_health(
                profile,
                probe,
                latency_ms,
                prev_health.get(profile.name),
            )

    return [
        expired_results.get(p.name) or network_results[p.name]
        for p in profiles
    ]


def probe_many_sync(
    profiles: list[Profile],
    *,
    prev_health: dict[str, Health] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[ProfileHealth]:
    """Blocking wrapper around probe_many(). Convenient for CLI callers."""
    return asyncio.run(probe_many(profiles, prev_health=prev_health, timeout=timeout))


async def probe_raw_many(
    profiles: list[Profile],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[tuple[str, int | None, dict[str, Any] | None, dict[str, str]]]:
    """Diagnostic: return raw (name, status_code, body, headers) tuples.

    Bypasses classification + caching. Intended for `claude-lb probe --raw`
    to help capture unknown response shapes during development, or to inspect
    what Anthropic is currently returning when a user reports weird behaviour.
    """
    if not profiles:
        return []
    out: list[tuple[str, int | None, dict[str, Any] | None, dict[str, str]]] = []
    async with httpx.AsyncClient() as client:
        tasks = [_probe_once(client, p, timeout) for p in profiles]
        pairs = await asyncio.gather(*tasks, return_exceptions=False)
    for profile, (probe, _latency) in zip(profiles, pairs):
        out.append(
            (
                profile.name,
                probe.status_code,
                probe.body,
                probe.headers or {},
            )
        )
    return out


def probe_raw_many_sync(
    profiles: list[Profile],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[tuple[str, int | None, dict[str, Any] | None, dict[str, str]]]:
    """Blocking wrapper around probe_raw_many()."""
    return asyncio.run(probe_raw_many(profiles, timeout=timeout))
