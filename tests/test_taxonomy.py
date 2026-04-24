"""Taxonomy tests — every one of the 7 states, driven by real-ish fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_lb.models import Health
from claude_lb.taxonomy import ProbeInput, classify, compute_expires_at

FIXTURES = Path(__file__).parent / "fixtures"
USAGE_FIXTURES = FIXTURES / "oauth-usage"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


FIXED_NOW = datetime(2026, 4, 24, 9, 45, 12, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Happy path — 200 with usage numbers under the limit
# ---------------------------------------------------------------------------


def test_200_ok_populates_usage() -> None:
    body = load_json(USAGE_FIXTURES / "ok.json")
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.OK
    assert result.error is None
    assert result.usage is not None
    assert result.usage.session_pct == 9
    assert result.usage.weekly_pct == 6
    assert result.usage.sonnet_pct == 0
    assert result.session_reset_at == datetime(2026, 4, 24, 14, 0, 0, 19360, tzinfo=UTC)
    assert result.weekly_reset_at == datetime(2026, 4, 25, 4, 0, 0, 19375, tzinfo=UTC)


def test_200_empty_body_still_ok() -> None:
    result = classify(ProbeInput(status_code=200, body={}), probed_at=FIXED_NOW)
    assert result.health is Health.OK
    assert result.usage is not None
    assert result.usage.session_pct is None


def test_200_extracts_extra_usage_block() -> None:
    body = load_json(USAGE_FIXTURES / "ok-with-extra-usage.json")
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.OK
    assert result.usage is not None
    extra = result.usage.extra
    assert extra is not None
    assert extra.is_enabled is True
    assert extra.monthly_limit == 31000.0
    assert extra.used_credits == 31280.0
    assert extra.utilization == 100
    assert extra.currency == "AUD"
    assert extra.is_exhausted is True


def test_200_without_extra_usage_leaves_field_none() -> None:
    body = {
        "five_hour": {"utilization": 12.0},
        "seven_day": {"utilization": 3.0},
    }
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.usage is not None
    assert result.usage.extra is None


def test_extra_usage_with_non_dict_is_ignored() -> None:
    body = {"five_hour": {"utilization": 10.0}, "extra_usage": "not a dict"}
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.usage is not None
    assert result.usage.extra is None


def test_extra_usage_is_enabled_false_keeps_exhaustion_false() -> None:
    body = {"five_hour": {}, "extra_usage": {"is_enabled": False}}
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.usage is not None
    assert result.usage.extra is not None
    assert result.usage.extra.is_enabled is False
    assert result.usage.extra.is_exhausted is False


# ---------------------------------------------------------------------------
# Utilization-based session / weekly limits (200 + >= 100%)
# ---------------------------------------------------------------------------


def test_200_session_exhausted_promotes_to_session_limit() -> None:
    body = load_json(USAGE_FIXTURES / "session-exhausted.json")
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.SESSION_LIMIT
    assert result.usage is not None
    assert result.usage.session_pct == 100
    assert result.session_reset_at == datetime(2026, 4, 24, 14, 20, 0, tzinfo=UTC)


def test_200_weekly_exhausted_promotes_to_weekly_limit() -> None:
    body = load_json(USAGE_FIXTURES / "weekly-exhausted.json")
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.WEEKLY_LIMIT
    assert result.usage is not None
    assert result.usage.weekly_pct == 100
    assert result.weekly_reset_at == datetime(2026, 4, 28, 3, 0, 0, tzinfo=UTC)


def test_200_both_exhausted_prefers_weekly_limit() -> None:
    body = load_json(USAGE_FIXTURES / "both-exhausted.json")
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.WEEKLY_LIMIT


def test_session_limit_missing_reset_falls_back_to_probed_plus_5h() -> None:
    body = {"five_hour": {"utilization": 100.0}, "seven_day": {"utilization": 42.0}}
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.SESSION_LIMIT
    assert result.session_reset_at == FIXED_NOW + timedelta(hours=5)


def test_weekly_limit_missing_reset_falls_back_to_probed_plus_7d() -> None:
    body = {"five_hour": {"utilization": 20.0}, "seven_day": {"utilization": 100.0}}
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    assert result.health is Health.WEEKLY_LIMIT
    assert result.weekly_reset_at == FIXED_NOW + timedelta(days=7)


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
# 403 setup-token scope fallback — profile is valid for inference
# ---------------------------------------------------------------------------


def test_403_scope_missing_is_ok_with_null_usage() -> None:
    body = load_json(USAGE_FIXTURES / "403-scope-missing.json")
    result = classify(ProbeInput(status_code=403, body=body))
    assert result.health is Health.OK
    assert result.usage is None
    assert result.error is not None
    assert "scope" in (result.error.message or "").lower()


def test_403_other_is_unknown() -> None:
    body = {"error": {"type": "permission_error", "message": "forbidden"}}
    result = classify(ProbeInput(status_code=403, body=body))
    assert result.health is Health.UNKNOWN


# ---------------------------------------------------------------------------
# Rate limiting — the usage endpoint itself, not downstream messages
# ---------------------------------------------------------------------------


def test_429_is_rate_limited() -> None:
    body = {"error": {"type": "rate_limit_error", "message": "slow down"}}
    result = classify(ProbeInput(status_code=429, body=body, headers={"retry-after": "30"}))
    assert result.health is Health.RATE_LIMITED
    assert result.retry_after_s == 30


def test_429_without_retry_after_is_rate_limited() -> None:
    result = classify(ProbeInput(status_code=429, body=None, headers={}))
    assert result.health is Health.RATE_LIMITED
    assert result.retry_after_s is None


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
    result = classify(ProbeInput(status_code=200, body={}), probed_at=FIXED_NOW)
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == FIXED_NOW + timedelta(minutes=5)


def test_compute_expires_at_auth_dead_is_none() -> None:
    body = load_json(FIXTURES / "401-auth-error.json")
    result = classify(ProbeInput(status_code=401, body=body))
    assert compute_expires_at(result, FIXED_NOW) is None


def test_compute_expires_at_session_limit_uses_body_reset() -> None:
    body = load_json(USAGE_FIXTURES / "session-exhausted.json")
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == datetime(2026, 4, 24, 14, 20, 0, tzinfo=UTC)


def test_compute_expires_at_weekly_limit_uses_body_reset() -> None:
    body = load_json(USAGE_FIXTURES / "weekly-exhausted.json")
    result = classify(ProbeInput(status_code=200, body=body), probed_at=FIXED_NOW)
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == datetime(2026, 4, 28, 3, 0, 0, tzinfo=UTC)


def test_compute_expires_at_rate_limited_uses_retry_after() -> None:
    result = classify(ProbeInput(status_code=429, body=None, headers={"retry-after": "120"}))
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == FIXED_NOW + timedelta(seconds=120)


def test_compute_expires_at_rate_limited_defaults_60s_without_retry_after() -> None:
    result = classify(ProbeInput(status_code=429, body=None, headers={}))
    expires = compute_expires_at(result, FIXED_NOW)
    assert expires == FIXED_NOW + timedelta(seconds=60)


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
        ({"status_code": 429, "body": None}, Health.RATE_LIMITED),
        ({"status_code": 500, "body": None}, Health.UNKNOWN),
    ],
)
def test_classification_priority(probe_kwargs: dict, expected: Health) -> None:
    result = classify(ProbeInput(**probe_kwargs))
    assert result.health is expected
