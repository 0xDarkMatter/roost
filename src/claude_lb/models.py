"""Pydantic models shared across claude-lb modules."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Health(str, Enum):
    """Seven-state health taxonomy (SPEC §6)."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"
    SESSION_LIMIT = "session_limit"
    WEEKLY_LIMIT = "weekly_limit"
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
            Health.NETWORK_ERROR,
            Health.UNKNOWN,
        )


class ErrorInfo(BaseModel):
    """Structured error detail captured during probing."""

    type: str
    message: str = ""


class Usage(BaseModel):
    """Optional usage enrichment (may be None if no API available)."""

    session_pct: int | None = None
    weekly_pct: int | None = None


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


class ClassificationResult(BaseModel):
    """Return shape from taxonomy.classify()."""

    health: Health
    error: ErrorInfo | None = None
    retry_after_s: int | None = None
    session_reset_at: datetime | None = None
    weekly_reset_at: datetime | None = None
