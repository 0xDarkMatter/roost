"""Pydantic models shared across claude-lb modules."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Health(str, Enum):
    """Eight-state health taxonomy (SPEC §6)."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"
    SESSION_LIMIT = "session_limit"
    WEEKLY_LIMIT = "weekly_limit"
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


class Usage(BaseModel):
    """Usage percentages sourced from /api/oauth/usage (SPEC §7).

    Window percentages are 0-100 integers, rounded from the upstream float.
    `None` means the corresponding window was absent or the probe could not
    read it (e.g. 403 scope-missing fallback).
    """

    session_pct: int | None = None
    weekly_pct: int | None = None
    sonnet_pct: int | None = None
    opus_pct: int | None = None
    extra: ExtraUsage | None = None


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
    usage: Usage | None = None
    probe_latency_ms: int | None = None
    credentials_mtime: float | None = None
    subscription_type: str | None = None  # e.g. "max", "team", "pro"


class HealthCache(BaseModel):
    """On-disk cache shape for ~/.config/claude-lb/health.json."""

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
    usage: Usage | None = None
