"""Taxonomy tests — every one of the 7 states, driven by real-ish fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_lb.models import Health
from claude_lb.taxonomy import ProbeInput, classify, compute_expires_at

FIXTURES = Path(__file__).parent / "fixtures"
RL_FIXTURES = FIXTURES / "429-responses"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


FIXED_NOW = datetime(2026, 4, 24, 9, 45, 12, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_200_classifies_as_ok() -> None:
    result = classify(ProbeInput(status_code=200, body={"data": []}))
    assert result.health is Health.OK
    assert result.error is None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_401_classifies_as_auth_dead() -> None:
    body = load_json(FIXTURES / "401-auth-error.json")
    result = classify(ProbeInput(status_code=401, body=body))
    assert result.health is Health.AUTH_DEAD
    assert result.error is not None
    assert result.error.type == "authentication_error"


def test_401_missing_body_still_auth_dead() -> None:
    result = classify(ProbeInput(status_code=401, body=None))
    assert result.health is Health.AUTH_DEAD


# ---------------------------------------------------------------------------
# 429 subtypes
# ---------------------------------------------------------------------------


def test_plain_429_is_rate_limited() -> None:
    body = load_json(RL_FIXTURES / "plain-rate-limit.json")
    headers = {"retry-after": "60"}
    result = classify(ProbeInput(status_code=429, body=body, headers=headers))
    assert result.health is Health.RATE_LIMITED
    assert result.retry_after_s == 60


def test_429_session_limit() -> None:
    body = load_json(RL_FIXTURES / "session-limit.json")
    result = classify(
        ProbeInput(status_code=429, body=body, headers={}), probed_at=FIXED_NOW
    )
    assert result.health is Health.SESSION_LIMIT
    assert result.session_reset_at is not None
    # Message had an embedded timestamp — we should parse it.
    assert result.session_reset_at.year == 2026


def test_429_session_limit_defaults_to_5h_if_no_timestamp() -> None:
    body = {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "Session limit reached. Try again later.",
        },
    }
    result = classify(ProbeInput(status_code=429, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.SESSION_LIMIT
    assert result.session_reset_at == FIXED_NOW + timedelta(hours=5)


def test_429_weekly_limit() -> None:
    body = load_json(RL_FIXTURES / "weekly-limit.json")
    result = classify(ProbeInput(status_code=429, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.WEEKLY_LIMIT
    assert result.weekly_reset_at is not None


def test_429_weekly_limit_short_variant() -> None:
    body = load_json(RL_FIXTURES / "weekly-limit-short.json")
    result = classify(ProbeInput(status_code=429, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.WEEKLY_LIMIT


def test_429_weekly_defaults_to_next_sunday() -> None:
    body = {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "Weekly plan limit reached.",
        },
    }
    # Fixed date is Friday 2026-04-24.
    result = classify(ProbeInput(status_code=429, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.WEEKLY_LIMIT
    # Next Sunday at 02:00 UTC is 2026-04-26T02:00Z
    assert result.weekly_reset_at == datetime(2026, 4, 26, 2, 0, tzinfo=UTC)


def test_429_weekly_wins_over_session_when_both_keywords_present() -> None:
    """Weekly is checked first — important, since 'weekly' + 'session' overlap
    is possible in some error messages."""
    body = {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "Weekly plan limit exceeded; your current session is paused.",
        },
    }
    result = classify(ProbeInput(status_code=429, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.WEEKLY_LIMIT


def test_429_non_rate_limit_type_falls_through_to_plain() -> None:
    body = {
        "type": "error",
        "error": {
            "type": "overloaded_error",
            "message": "Server is overloaded; retry shortly.",
        },
    }
    result = classify(ProbeInput(status_code=429, body=body))
    assert result.health is Health.RATE_LIMITED


# ---------------------------------------------------------------------------
# 403
# ---------------------------------------------------------------------------


def test_403_on_previously_ok_profile_is_weekly_limit() -> None:
    body = {"type": "error", "error": {"type": "permission_error", "message": "Access denied."}}
    result = classify(
        ProbeInput(status_code=403, body=body),
        probed_at=FIXED_NOW,
        prev_health=Health.OK,
    )
    assert result.health is Health.WEEKLY_LIMIT


def test_403_without_prior_context_is_unknown() -> None:
    body = {"type": "error", "error": {"type": "permission_error", "message": "Access denied."}}
    result = classify(ProbeInput(status_code=403, body=body))
    assert result.health is Health.UNKNOWN


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


def test_timeout_exception_is_network_error() -> None:
    probe = ProbeInput(exception_kind="timeout", exception_message="Read timed out")
    result = classify(probe)
    assert result.health is Health.NETWORK_ERROR
    assert result.error is not None
    assert result.error.type == "timeout"


def test_refused_exception_is_network_error() -> None:
    probe = ProbeInput(exception_kind="refused", exception_message="Connection refused")
    result = classify(probe)
    assert result.health is Health.NETWORK_ERROR


# ---------------------------------------------------------------------------
# Unknown
# ---------------------------------------------------------------------------


def test_500_is_unknown() -> None:
    result = classify(ProbeInput(status_code=500, body={"error": {"message": "oops"}}))
    assert result.health is Health.UNKNOWN


def test_missing_status_code_is_unknown() -> None:
    result = classify(ProbeInput())
    assert result.health is Health.UNKNOWN


# ---------------------------------------------------------------------------
# Expiry / TTL
# ---------------------------------------------------------------------------


def test_compute_expires_at_ok_is_5min() -> None:
    result = classify(ProbeInput(status_code=200, body={"data": []}))
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == FIXED_NOW + timedelta(minutes=5)


def test_compute_expires_at_auth_dead_is_none() -> None:
    body = load_json(FIXTURES / "401-auth-error.json")
    result = classify(ProbeInput(status_code=401, body=body))
    assert compute_expires_at(result, FIXED_NOW) is None


def test_compute_expires_at_rate_limited_uses_retry_after() -> None:
    body = load_json(RL_FIXTURES / "plain-rate-limit.json")
    result = classify(ProbeInput(status_code=429, body=body, headers={"retry-after": "120"}))
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == FIXED_NOW + timedelta(seconds=120)


def test_compute_expires_at_rate_limited_defaults_60s_without_retry_after() -> None:
    body = load_json(RL_FIXTURES / "plain-rate-limit.json")
    result = classify(ProbeInput(status_code=429, body=body, headers={}))
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == FIXED_NOW + timedelta(seconds=60)


def test_compute_expires_at_rate_limited_honours_explicit_zero() -> None:
    """Regression: retry-after=0 means 'retry now', not fall-through to 60s default."""
    body = load_json(RL_FIXTURES / "plain-rate-limit.json")
    result = classify(ProbeInput(status_code=429, body=body, headers={"retry-after": "0"}))
    assert result.retry_after_s == 0
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == FIXED_NOW  # immediate retry, not +60s


def test_compute_expires_at_network_error_is_30s() -> None:
    result = classify(ProbeInput(exception_kind="timeout", exception_message="t"))
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == FIXED_NOW + timedelta(seconds=30)


def test_compute_expires_at_unknown_is_60s() -> None:
    result = classify(ProbeInput(status_code=500, body=None))
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == FIXED_NOW + timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Property-ish coverage: classification order is deterministic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "probe_kwargs,expected",
    [
        ({"exception_kind": "timeout", "exception_message": "t"}, Health.NETWORK_ERROR),
        ({"status_code": 200, "body": {}}, Health.OK),
        ({"status_code": 401, "body": {}}, Health.AUTH_DEAD),
        ({"status_code": 500, "body": None}, Health.UNKNOWN),
    ],
)
def test_classification_priority(probe_kwargs: dict, expected: Health) -> None:
    result = classify(ProbeInput(**probe_kwargs))
    assert result.health is expected
