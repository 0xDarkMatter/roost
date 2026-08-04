"""Eight-state health classifier (SPEC §6).

Every probe response from Anthropic's `/api/oauth/usage` endpoint is funnelled
through `classify()`, which returns one of eight `Health` states plus the
supporting usage metadata (`session_pct`, `weekly_pct`, reset timestamps). The
ninth `Health` state, AUTH_EXPIRED, is never returned by `classify()` — it is
assigned locally by `probe.py` from credential expiry, before a network probe
is even attempted.

Classification order (first match wins):

    1. Network-level exception                 → NETWORK_ERROR
    2. HTTP 200
         seven_day.utilization >= 100          → WEEKLY_LIMIT
         five_hour.utilization >= 100          → SESSION_LIMIT
         any active limits[] entry >= 100%     → MODEL_LIMIT
         else                                  → OK
    3. HTTP 401                                → AUTH_DEAD
    4. HTTP 403 with "scope requirement"       → OK (usage: null)
       HTTP 403 other                          → UNKNOWN
    5. HTTP 429                                → RATE_LIMITED
    6. Anything else                           → UNKNOWN

Usage numbers come straight from the `/api/oauth/usage` response — no 429
keyword matching, no "next Sunday" heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import (
    ClassificationResult,
    ErrorInfo,
    ExtraUsage,
    Health,
    ScopedLimit,
    Spend,
    Usage,
)


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


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp (with timezone) from the response body."""
    if not isinstance(value, str) or not value:
        return None
    raw = value
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _utilization_to_pct(value: Any) -> int | None:
    """Convert a float 0.0–100.0 utilization to a rounded int percent."""
    if not isinstance(value, (int, float)):
        return None
    return max(0, min(100, int(round(float(value)))))


def _window(body: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    """Extract a usage window dict from the response body, if present."""
    if not isinstance(body, dict):
        return None
    window = body.get(key)
    return window if isinstance(window, dict) else None


def _build_extra_usage(body: dict[str, Any] | None) -> ExtraUsage | None:
    """Extract the `extra_usage` block (monthly overage credits) if present.

    Returns None when the field is absent or not a dict; otherwise an
    ExtraUsage with whatever fields were populated.
    """
    extra = _window(body, "extra_usage")
    if extra is None:
        return None
    monthly_limit = extra.get("monthly_limit")
    used_credits = extra.get("used_credits")
    return ExtraUsage(
        is_enabled=bool(extra.get("is_enabled", False)),
        monthly_limit=float(monthly_limit) if isinstance(monthly_limit, (int, float)) else None,
        used_credits=float(used_credits) if isinstance(used_credits, (int, float)) else None,
        utilization=_utilization_to_pct(extra.get("utilization")),
        currency=str(extra["currency"]) if isinstance(extra.get("currency"), str) else None,
    )


def _build_scoped_limits(body: dict[str, Any] | None) -> list[ScopedLimit]:
    """Parse the top-level `limits[]` array (model-scoped capacity, e.g. Fable).

    Defensive by design: a missing key, a non-list value, non-dict entries,
    and a null/missing `scope` must all degrade to an empty-or-partial list,
    never raise — this endpoint's shape has already changed once without
    notice and roost must keep classifying instead of crashing the probe.
    """
    if not isinstance(body, dict):
        return []
    raw = body.get("limits")
    if not isinstance(raw, list):
        return []
    limits: list[ScopedLimit] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_name: str | None = None
        surface: str | None = None
        scope = item.get("scope")
        if isinstance(scope, dict):
            model = scope.get("model")
            if isinstance(model, dict) and isinstance(model.get("display_name"), str):
                model_name = model["display_name"]
            if isinstance(scope.get("surface"), str):
                surface = scope["surface"]
        limits.append(
            ScopedLimit(
                kind=str(item.get("kind", "")),
                group=str(item["group"]) if isinstance(item.get("group"), str) else None,
                percent=_utilization_to_pct(item.get("percent")),
                severity=str(item["severity"]) if isinstance(item.get("severity"), str) else None,
                resets_at=_parse_iso(item.get("resets_at")),
                model=model_name,
                surface=surface,
                is_active=bool(item.get("is_active", False)),
            )
        )
    return limits


def _build_spend(body: dict[str, Any] | None) -> Spend | None:
    """Extract the top-level `spend` block (monthly overage in minor units).

    Returns None when the field is absent or not a dict, mirroring
    `_build_extra_usage`'s degrade-gracefully contract.
    """
    spend = _window(body, "spend")
    if spend is None:
        return None
    used = spend.get("used")
    used_minor: int | None = None
    currency: str | None = None
    exponent: int | None = None
    if isinstance(used, dict):
        if isinstance(used.get("amount_minor"), int):
            used_minor = used["amount_minor"]
        if isinstance(used.get("currency"), str):
            currency = used["currency"]
        if isinstance(used.get("exponent"), int):
            exponent = used["exponent"]
    limit = spend.get("limit")
    limit_minor: int | None = None
    if isinstance(limit, dict) and isinstance(limit.get("amount_minor"), int):
        limit_minor = limit["amount_minor"]
    return Spend(
        used_minor=used_minor,
        currency=currency,
        exponent=exponent,
        limit_minor=limit_minor,
        percent=_utilization_to_pct(spend.get("percent")),
        severity=str(spend["severity"]) if isinstance(spend.get("severity"), str) else None,
        enabled=bool(spend.get("enabled", False)),
    )


def _build_usage(body: dict[str, Any] | None) -> Usage:
    """Assemble a Usage model from the /api/oauth/usage response body."""
    five_hour = _window(body, "five_hour") or {}
    seven_day = _window(body, "seven_day") or {}
    sonnet = _window(body, "seven_day_sonnet") or {}
    opus = _window(body, "seven_day_opus") or {}
    return Usage(
        session_pct=_utilization_to_pct(five_hour.get("utilization")),
        weekly_pct=_utilization_to_pct(seven_day.get("utilization")),
        sonnet_pct=_utilization_to_pct(sonnet.get("utilization")),
        opus_pct=_utilization_to_pct(opus.get("utilization")),
        extra=_build_extra_usage(body),
        limits=_build_scoped_limits(body),
        spend=_build_spend(body),
    )


def _is_scope_missing_403(body: dict[str, Any] | None) -> bool:
    """Detect a 403 caused by a setup-token lacking user:profile scope.

    The token is still valid for inference — we just can't read usage.
    """
    err = _extract_error(body)
    msg = (err.message or "").lower()
    return "scope" in msg and ("user:profile" in msg or "user_profile" in msg)


def _default_session_reset(probed_at: datetime) -> datetime:
    """Fallback when the body omits five_hour.resets_at. 5-hour session window."""
    return probed_at + timedelta(hours=5)


def _default_weekly_reset(probed_at: datetime) -> datetime:
    """Fallback when the body omits seven_day.resets_at. 7 days from probe."""
    return probed_at + timedelta(days=7)


def _classify_200(
    body: dict[str, Any] | None,
    probed_at: datetime,
) -> ClassificationResult:
    """Classify a 200 usage response.

    Reads `five_hour.utilization` / `seven_day.utilization` and derives
    SESSION_LIMIT / WEEKLY_LIMIT from utilization >= 100, else OK.
    """
    usage = _build_usage(body)
    five_hour = _window(body, "five_hour") or {}
    seven_day = _window(body, "seven_day") or {}
    session_reset = _parse_iso(five_hour.get("resets_at"))
    weekly_reset = _parse_iso(seven_day.get("resets_at"))

    weekly_util = seven_day.get("utilization")
    if isinstance(weekly_util, (int, float)) and weekly_util >= 100:
        return ClassificationResult(
            health=Health.WEEKLY_LIMIT,
            usage=usage,
            session_reset_at=session_reset,
            weekly_reset_at=weekly_reset or _default_weekly_reset(probed_at),
        )

    session_util = five_hour.get("utilization")
    if isinstance(session_util, (int, float)) and session_util >= 100:
        return ClassificationResult(
            health=Health.SESSION_LIMIT,
            usage=usage,
            session_reset_at=session_reset or _default_session_reset(probed_at),
            weekly_reset_at=weekly_reset,
        )

    # Model-scoped capacity (e.g. Fable) exhausts independently of the
    # aggregate weekly/session windows — a profile can be well under its
    # weekly_all cap while its model-scoped allotment is spent (see
    # tests/fixtures/oauth-usage/limits-fable-exhausted.json: weekly_all=76,
    # Fable=100). Only ACTIVE limits gate selection; `is_active: false` means
    # the entry is informational, not currently enforced.
    for limit in usage.limits:
        if limit.is_active and limit.is_exhausted:
            return ClassificationResult(
                health=Health.MODEL_LIMIT,
                usage=usage,
                session_reset_at=session_reset,
                weekly_reset_at=weekly_reset,
                model_reset_at=limit.resets_at,
            )

    return ClassificationResult(
        health=Health.OK,
        usage=usage,
        session_reset_at=session_reset,
        weekly_reset_at=weekly_reset,
    )


def classify(
    probe: ProbeInput,
    probed_at: datetime | None = None,
    prev_health: Health | None = None,
) -> ClassificationResult:
    """Classify a probe outcome into one of eight Health states.

    `prev_health` is accepted for call-site stability but no longer used —
    the /api/oauth/usage endpoint provides real utilization numbers, so the
    "403 on a previously-ok profile means weekly_limit" heuristic is retired.
    """
    del prev_health
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

    # 2. Happy path — parse usage and maybe promote to session/weekly limit
    if status == 200:
        return _classify_200(probe.body, probed_at)

    # 3. Auth dead
    if status == 401:
        err = _extract_error(probe.body)
        if not err.message:
            err = ErrorInfo(type=err.type or "authentication_error", message="Unauthorized")
        return ClassificationResult(health=Health.AUTH_DEAD, error=err)

    # 4. Forbidden — scope-missing 403 means the token works for inference
    #    but lacks user:profile, so we can't read usage. Classify as OK.
    if status == 403:
        if _is_scope_missing_403(probe.body):
            return ClassificationResult(
                health=Health.OK,
                error=_extract_error(probe.body),
            )
        return ClassificationResult(health=Health.UNKNOWN, error=_extract_error(probe.body))

    # 5. Rate limited (usage endpoint itself — rare)
    if status == 429:
        retry_after = None
        if probe.headers:
            retry_after = _parse_retry_after(
                probe.headers.get("retry-after") or probe.headers.get("Retry-After")
            )
        return ClassificationResult(
            health=Health.RATE_LIMITED,
            error=_extract_error(probe.body),
            retry_after_s=retry_after,
        )

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
    Health.MODEL_LIMIT: None,  # computed from model_reset_at
    Health.AUTH_DEAD: None,  # never expires; manual invalidate
    Health.NETWORK_ERROR: 30,
    Health.UNKNOWN: 60,
}


def compute_expires_at(
    result: ClassificationResult,
    probed_at: datetime,
) -> datetime | None:
    """Compute the cache expiry timestamp for a classification result."""
    h = result.health
    if h is Health.AUTH_DEAD:
        return None
    if h is Health.WEEKLY_LIMIT:
        return result.weekly_reset_at or _default_weekly_reset(probed_at)
    if h is Health.SESSION_LIMIT:
        return result.session_reset_at or _default_session_reset(probed_at)
    if h is Health.MODEL_LIMIT:
        # Fall back to the weekly default (not session) — a model-scoped
        # window mirrors the weekly cadence in every fixture observed so far.
        return result.model_reset_at or _default_weekly_reset(probed_at)
    if h is Health.RATE_LIMITED:
        retry_seconds: int = result.retry_after_s or 60
        return probed_at + timedelta(seconds=retry_seconds)
    ttl = DEFAULT_TTL_SECONDS.get(h)
    if ttl is None:
        return None
    return probed_at + timedelta(seconds=ttl)
