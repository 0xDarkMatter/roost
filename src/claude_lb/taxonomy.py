"""Seven-state health classifier (SPEC §6).

This module is the heart of claude-lb. Every probe response from the Anthropic
API is funnelled through `classify()`, which returns one of seven `Health`
states plus supporting metadata (retry-after, reset timestamps, error detail).

Classification order (first match wins):

    1. Network-level exception              → NETWORK_ERROR
    2. HTTP 200                              → OK
    3. HTTP 401                              → AUTH_DEAD
    4. HTTP 429
         body.error.type == rate_limit_error
         + weekly keywords                   → WEEKLY_LIMIT
         + session keywords                  → SESSION_LIMIT
         else / retry-after present          → RATE_LIMITED
    5. HTTP 403                              → WEEKLY_LIMIT (plan-quota guess)
                                               otherwise UNKNOWN
    6. Anything else                         → UNKNOWN

The input is a normalised `ProbeInput` dataclass rather than a raw httpx
Response so the classifier is trivially unit-testable from JSON fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import ClassificationResult, ErrorInfo, Health
from .patterns import SESSION_KEYWORDS, WEEKLY_KEYWORDS, contains_any


@dataclass
class ProbeInput:
    """Normalised probe outcome for classification.

    Either `status_code` + `body` are set (HTTP response received), or
    `exception_kind` is set (network-level failure).
    """

    status_code: int | None = None
    body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    exception_kind: str | None = None  # "timeout" | "dns" | "tls" | "refused" | "other"
    exception_message: str = ""


# Regex for ISO-ish timestamps embedded in rate-limit messages.
# Example: "resets at 2026-04-26T16:00:00Z"
ISO_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_retry_after(header_value: str | None) -> int | None:
    """Parse HTTP Retry-After header (integer seconds form only)."""
    if header_value is None:
        return None
    try:
        value = int(header_value.strip())
    except (ValueError, AttributeError):
        return None
    return value if value >= 0 else None


def _extract_error(body: dict[str, Any] | None) -> ErrorInfo:
    """Safely extract {type, message} from an Anthropic error body."""
    if not isinstance(body, dict):
        return ErrorInfo(type="unknown", message="")
    err = body.get("error")
    if isinstance(err, dict):
        return ErrorInfo(
            type=str(err.get("type", "unknown")),
            message=str(err.get("message", "")),
        )
    return ErrorInfo(type="unknown", message=str(body.get("message", "")))


def _try_parse_reset_timestamp(message: str) -> datetime | None:
    """Extract an ISO 8601 timestamp from a rate-limit message, if present."""
    if not message:
        return None
    m = ISO_TS_RE.search(message)
    if not m:
        return None
    raw = m.group(1).replace(" ", "T")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _default_session_reset(probed_at: datetime) -> datetime:
    """Session windows are 5h. Default reset = probed_at + 5h."""
    return probed_at + timedelta(hours=5)


def _default_weekly_reset(probed_at: datetime) -> datetime:
    """Weekly windows reset on Sunday. Default = next Sunday 02:00 UTC."""
    days_until_sunday = (6 - probed_at.weekday()) % 7
    if days_until_sunday == 0:
        days_until_sunday = 7
    base = probed_at + timedelta(days=days_until_sunday)
    return base.replace(hour=2, minute=0, second=0, microsecond=0)


def _classify_429(
    body: dict[str, Any] | None,
    headers: dict[str, str] | None,
    probed_at: datetime,
) -> ClassificationResult:
    """Classify a 429 response into rate_limited / session_limit / weekly_limit."""
    err = _extract_error(body)
    msg = err.message or ""

    retry_after = None
    if headers:
        retry_after = _parse_retry_after(headers.get("retry-after") or headers.get("Retry-After"))

    # Only use the rate_limit_error type as a signal to look for keywords.
    is_rl_type = err.type == "rate_limit_error"

    if is_rl_type and contains_any(msg, WEEKLY_KEYWORDS):
        reset = _try_parse_reset_timestamp(msg) or _default_weekly_reset(probed_at)
        return ClassificationResult(
            health=Health.WEEKLY_LIMIT,
            error=err,
            retry_after_s=retry_after,
            weekly_reset_at=reset,
        )

    if is_rl_type and contains_any(msg, SESSION_KEYWORDS):
        reset = _try_parse_reset_timestamp(msg) or _default_session_reset(probed_at)
        return ClassificationResult(
            health=Health.SESSION_LIMIT,
            error=err,
            retry_after_s=retry_after,
            session_reset_at=reset,
        )

    return ClassificationResult(
        health=Health.RATE_LIMITED,
        error=err,
        retry_after_s=retry_after,
    )


def classify(
    probe: ProbeInput,
    probed_at: datetime | None = None,
    prev_health: Health | None = None,
) -> ClassificationResult:
    """Classify a probe outcome into one of seven Health states.

    Args:
        probe: the normalised probe outcome.
        probed_at: when the probe occurred (default: now, UTC).
        prev_health: previously-cached health for this profile. Used only to
            promote 403 from UNKNOWN to WEEKLY_LIMIT when the prior state was
            OK (the heuristic is that a previously-healthy profile returning
            403 is almost certainly plan-quota-exhausted).
    """
    probed_at = probed_at or _now()

    # 1. Network-level failures
    if probe.exception_kind is not None:
        return ClassificationResult(
            health=Health.NETWORK_ERROR,
            error=ErrorInfo(type=probe.exception_kind, message=probe.exception_message),
        )

    status = probe.status_code
    if status is None:
        return ClassificationResult(
            health=Health.UNKNOWN,
            error=ErrorInfo(type="no_response", message="Probe returned no status code"),
        )

    # 2. Happy path
    if status == 200:
        return ClassificationResult(health=Health.OK)

    # 3. Auth
    if status == 401:
        err = _extract_error(probe.body)
        if not err.message:
            err = ErrorInfo(type=err.type or "authentication_error", message="Unauthorized")
        return ClassificationResult(health=Health.AUTH_DEAD, error=err)

    # 4. Rate limiting
    if status == 429:
        return _classify_429(probe.body, probe.headers, probed_at)

    # 5. Forbidden — heuristic: previously-OK profile returning 403 is plan-exhausted
    if status == 403:
        if prev_health is Health.OK:
            reset = _default_weekly_reset(probed_at)
            return ClassificationResult(
                health=Health.WEEKLY_LIMIT,
                error=_extract_error(probe.body),
                weekly_reset_at=reset,
            )
        return ClassificationResult(health=Health.UNKNOWN, error=_extract_error(probe.body))

    # 6. Everything else
    return ClassificationResult(
        health=Health.UNKNOWN,
        error=_extract_error(probe.body),
    )


# TTL table per state (seconds). auth_dead -> None means "never expires".
DEFAULT_TTL_SECONDS: dict[Health, int | None] = {
    Health.OK: 5 * 60,
    Health.RATE_LIMITED: None,  # computed from retry_after
    Health.SESSION_LIMIT: None,  # computed from session_reset_at
    Health.WEEKLY_LIMIT: None,  # computed from weekly_reset_at
    Health.AUTH_DEAD: None,  # never expires; manual invalidate
    Health.NETWORK_ERROR: 30,
    Health.UNKNOWN: 60,
}


def compute_expires_at(
    result: ClassificationResult,
    probed_at: datetime,
) -> datetime | None:
    """Compute the cache expiry timestamp for a classification result.

    Returns None for states that never expire (AUTH_DEAD) — these are
    invalidated manually via `claude-lb invalidate <name>` or by a mtime
    bump on the profile's credentials.json.
    """
    h = result.health
    if h is Health.AUTH_DEAD:
        return None
    if h is Health.WEEKLY_LIMIT:
        return result.weekly_reset_at or _default_weekly_reset(probed_at)
    if h is Health.SESSION_LIMIT:
        return result.session_reset_at or _default_session_reset(probed_at)
    if h is Health.RATE_LIMITED:
        # `retry_after_s is None` means server didn't tell us — default 60s.
        # Explicit 0 means "retry now" and must not fall through to the default.
        retry_seconds = result.retry_after_s if result.retry_after_s is not None else 60
        return probed_at + timedelta(seconds=retry_seconds)
    ttl = DEFAULT_TTL_SECONDS.get(h)
    if ttl is None:
        return None
    return probed_at + timedelta(seconds=ttl)
