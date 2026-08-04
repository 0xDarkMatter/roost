"""Pydantic models shared across roost modules."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Health(str, Enum):
    """Nine-state health taxonomy (SPEC §6)."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"
    SESSION_LIMIT = "session_limit"
    WEEKLY_LIMIT = "weekly_limit"
    MODEL_LIMIT = "model_limit"
    AUTH_EXPIRED = "auth_expired"
    AUTH_DEAD = "auth_dead"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"

    @property
    def is_healthy(self) -> bool:
        return self is Health.OK

    @property
    def is_terminal(self) -> bool:
        """True for states that require operator intervention or long waits."""
        return self in (Health.AUTH_DEAD, Health.WEEKLY_LIMIT)

    @property
    def is_transient(self) -> bool:
        """True for states that typically recover without operator action."""
        return self in (
            Health.RATE_LIMITED,
            Health.SESSION_LIMIT,
            Health.MODEL_LIMIT,
            Health.AUTH_EXPIRED,
            Health.NETWORK_ERROR,
            Health.UNKNOWN,
        )


class ErrorInfo(BaseModel):
    """Structured error detail captured during probing."""

    type: str
    message: str = ""


class ExtraUsage(BaseModel):
    """Monthly overage quota from `/api/oauth/usage` (extra_usage block).

    Anthropic Max plans support pay-as-you-go overage when the weekly window
    is exhausted. `is_enabled` indicates the account has overage turned on;
    `utilization >= 100` means the monthly overage budget itself is spent
    (the account falls back to the hard weekly/session caps).
    """

    is_enabled: bool = False
    monthly_limit: float | None = None
    used_credits: float | None = None
    utilization: int | None = None
    currency: str | None = None

    @property
    def is_exhausted(self) -> bool:
        return self.utilization is not None and self.utilization >= 100


class ScopedLimit(BaseModel):
    """One entry from the top-level `limits[]` array (new response shape).

    Model-scoped capacity (currently Fable) lives here now, replacing the
    `seven_day_sonnet`/`seven_day_opus` windows that are `null` on every
    profile under the new shape. `scope.model.id` is null upstream today —
    `display_name` ("Fable") is the only model identifier available, so
    lookups key off it case-insensitively (see `Usage.model_pct`).
    `kind` observed so far: "session", "weekly_all", "weekly_scoped".
    """

    kind: str
    group: str | None = None
    percent: int | None = None
    severity: str | None = None
    resets_at: datetime | None = None
    model: str | None = None
    surface: str | None = None
    is_active: bool = False

    @property
    def is_exhausted(self) -> bool:
        return self.percent is not None and self.percent >= 100


class Spend(BaseModel):
    """Monthly overage spend block (top-level `spend`, sibling to `extra_usage`).

    `exponent` is the minor-currency-unit power (e.g. 2 => cents); previously
    this was undocumented and empirically x100 (docs/findings.md §5) — the
    API now states it explicitly via this field.
    """

    used_minor: int | None = None
    currency: str | None = None
    exponent: int | None = None
    limit_minor: int | None = None
    percent: int | None = None
    severity: str | None = None
    enabled: bool = False


class Usage(BaseModel):
    """Usage percentages sourced from /api/oauth/usage (SPEC §7).

    Window percentages are 0-100 integers, rounded from the upstream float.
    `None` means the corresponding window was absent or the probe could not
    read it (e.g. 403 scope-missing fallback).

    `sonnet_pct`/`opus_pct` are populated from the legacy `seven_day_sonnet`/
    `seven_day_opus` windows, which are `null` on every profile under the
    new response shape but may still populate for accounts that haven't
    migrated (or older Pro/Team responses) — kept for `usage_log.py`/
    `stats.py` backwards-compat. Model-scoped capacity now lives in `limits`;
    use `model_pct()`/`fable_pct` to read it with the same fallback.
    """

    session_pct: int | None = None
    weekly_pct: int | None = None
    sonnet_pct: int | None = None
    opus_pct: int | None = None
    extra: ExtraUsage | None = None
    limits: list[ScopedLimit] = Field(default_factory=list)
    spend: Spend | None = None

    def model_pct(self, name: str) -> int | None:
        """Active scoped-limit percent for `name` (case-insensitive).

        Prefers `limits[]`; falls back to the legacy `sonnet_pct`/`opus_pct`
        windows for those two names so accounts still returning them don't
        regress to None.
        """
        lname = name.lower()
        for limit in self.limits:
            if limit.is_active and limit.model is not None and limit.model.lower() == lname:
                return limit.percent
        if lname == "sonnet":
            return self.sonnet_pct
        if lname == "opus":
            return self.opus_pct
        return None

    @property
    def fable_pct(self) -> int | None:
        return self.model_pct("fable")


class ProfileHealth(BaseModel):
    """One profile's cached health record."""

    name: str
    health: Health
    probed_at: datetime
    expires_at: datetime | None = None
    error: ErrorInfo | None = None
    retry_after_s: int | None = None
    session_reset_at: datetime | None = None
    weekly_reset_at: datetime | None = None
    model_reset_at: datetime | None = None
    usage: Usage | None = None
    probe_latency_ms: int | None = None
    credentials_mtime: float | None = None
    subscription_type: str | None = None  # e.g. "max", "team", "pro"
    # Counter for per-profile network_error backoff. Incremented on every
    # consecutive NETWORK_ERROR probe, reset to 0 on any non-NETWORK_ERROR
    # outcome. Default 0 keeps backwards-compat with v0.3.0 cache files.
    consecutive_failures: int = 0


class HealthCache(BaseModel):
    """On-disk cache shape for ~/.config/roost/health.json."""

    schema_version: int = 1
    updated_at: datetime
    profiles: dict[str, ProfileHealth] = Field(default_factory=dict)


class Profile(BaseModel):
    """A discovered profile (name + token + credentials mtime)."""

    name: str
    access_token: str
    credentials_path: str
    credentials_mtime: float
    token_source: str = "claudeAiOauth.accessToken"
    access_token_expires_at: datetime | None = None
    refresh_token_present: bool = False
    subscription_type: str | None = None  # e.g. "max", "team", "pro"


class ClassificationResult(BaseModel):
    """Return shape from taxonomy.classify()."""

    health: Health
    error: ErrorInfo | None = None
    retry_after_s: int | None = None
    session_reset_at: datetime | None = None
    weekly_reset_at: datetime | None = None
    model_reset_at: datetime | None = None
    usage: Usage | None = None
