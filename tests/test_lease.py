"""Lease tests — acquire/release/get_active lifecycle + refresh integration."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import respx

from claude_lb.lease import (
    Lease,
    acquire,
    get_active,
    list_leases,
    parse_duration,
    purge_expired,
    release,
    release_by_profile,
)
from claude_lb.models import Profile
from claude_lb.refresh import TOKEN_URL, refresh_profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lease_path(tmp_path: Path) -> Path:
    return tmp_path / "leases.json"


def _write_credentials(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "claudeAiOauth": {
            "accessToken": "old-access",
            "refreshToken": "old-refresh",
            "expiresAt": 1_700_000_000_000,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
    }
    path.write_text(json.dumps(payload))


def _profile(cred: Path) -> Profile:
    return Profile(
        name="account-a",
        access_token="old-access",
        credentials_path=str(cred),
        credentials_mtime=1000.0,
        access_token_expires_at=datetime.now(UTC) - timedelta(minutes=10),
        refresh_token_present=True,
    )


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


def test_parse_duration_seconds() -> None:
    assert parse_duration("90s") == 90


def test_parse_duration_minutes() -> None:
    assert parse_duration("30m") == 1800


def test_parse_duration_hours() -> None:
    assert parse_duration("2h") == 7200


def test_parse_duration_bare_int() -> None:
    assert parse_duration("120") == 120


def test_parse_duration_invalid() -> None:
    with pytest.raises(ValueError):
        parse_duration("5d")


def test_parse_duration_empty() -> None:
    with pytest.raises(ValueError):
        parse_duration("")


# ---------------------------------------------------------------------------
# acquire / release lifecycle
# ---------------------------------------------------------------------------


def test_acquire_creates_lease(tmp_path: Path) -> None:
    p = _lease_path(tmp_path)
    lease = acquire("account-a", 60, path=p)
    assert lease.profile == "account-a"
    assert lease.lease_id.startswith("lease-account-a-")
    assert lease.is_active()


def test_get_active_returns_lease(tmp_path: Path) -> None:
    p = _lease_path(tmp_path)
    lease = acquire("account-a", 60, path=p)
    found = get_active("account-a", path=p)
    assert found is not None
    assert found.lease_id == lease.lease_id


def test_release_removes_lease(tmp_path: Path) -> None:
    p = _lease_path(tmp_path)
    lease = acquire("account-a", 60, path=p)
    removed = release(lease.lease_id, path=p)
    assert removed is True
    assert get_active("account-a", path=p) is None


def test_release_idempotent(tmp_path: Path) -> None:
    p = _lease_path(tmp_path)
    lease = acquire("account-a", 60, path=p)
    release(lease.lease_id, path=p)
    # Second release should return False (not found) but not raise.
    removed = release(lease.lease_id, path=p)
    assert removed is False


def test_release_by_profile(tmp_path: Path) -> None:
    p = _lease_path(tmp_path)
    acquire("account-a", 60, path=p)
    removed = release_by_profile("account-a", path=p)
    assert removed is True
    assert get_active("account-a", path=p) is None


def test_expired_lease_is_not_active(tmp_path: Path) -> None:
    p = _lease_path(tmp_path)
    # Write a lease that has already expired.
    expired = Lease(
        lease_id="lease-account-a-expired",
        profile="account-a",
        expires_at=datetime.now(UTC) - timedelta(seconds=5),
        created_at=datetime.now(UTC) - timedelta(seconds=65),
        creator="test",
    )
    p.write_text(json.dumps({"account-a": expired.to_dict()}))
    found = get_active("account-a", path=p)
    assert found is None


def test_list_leases_active_only(tmp_path: Path) -> None:
    p = _lease_path(tmp_path)
    acquire("account-a", 60, path=p)
    acquire("account-b", 60, path=p)
    leases = list_leases(path=p)
    assert len(leases) == 2
    assert all(l.is_active() for l in leases)


def test_list_leases_include_expired(tmp_path: Path) -> None:
    p = _lease_path(tmp_path)
    expired = Lease(
        lease_id="lease-account-a-old",
        profile="account-a",
        expires_at=datetime.now(UTC) - timedelta(seconds=5),
        created_at=datetime.now(UTC) - timedelta(seconds=65),
        creator="test",
    )
    p.write_text(json.dumps({"account-a": expired.to_dict()}))
    acquire("account-b", 60, path=p)
    active = list_leases(include_expired=False, path=p)
    assert len(active) == 1
    assert active[0].profile == "account-b"
    all_ = list_leases(include_expired=True, path=p)
    assert len(all_) == 2


def test_purge_expired(tmp_path: Path) -> None:
    p = _lease_path(tmp_path)
    expired = Lease(
        lease_id="lease-account-a-old",
        profile="account-a",
        expires_at=datetime.now(UTC) - timedelta(seconds=5),
        created_at=datetime.now(UTC) - timedelta(seconds=65),
        creator="test",
    )
    p.write_text(json.dumps({"account-a": expired.to_dict()}))
    acquire("account-b", 60, path=p)
    count = purge_expired(path=p)
    assert count == 1
    remaining = list_leases(path=p)
    assert len(remaining) == 1
    assert remaining[0].profile == "account-b"


# ---------------------------------------------------------------------------
# refresh integration — LEASE_HELD blocks rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_blocked_by_active_lease(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred)
    p = _lease_path(tmp_path)
    acquire("account-a", 60, path=p)

    # Patch claude_lb.lease.get_active to use our tmp leases file.
    with patch("claude_lb.lease.get_active", side_effect=lambda name: get_active(name, path=p)):
        result = await refresh_profile(_profile(cred))

    assert result.refreshed is False
    assert result.error_code == "LEASE_HELD"
    assert result.error_message is not None and "account-a" in result.error_message
    # Credentials must be untouched.
    data = json.loads(cred.read_text())
    assert data["claudeAiOauth"]["refreshToken"] == "old-refresh"


@pytest.mark.asyncio
async def test_refresh_proceeds_after_lease_expires(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    cred = tmp_path / "account-a" / ".credentials.json"
    _write_credentials(cred)
    p = _lease_path(tmp_path)
    # Write an already-expired lease (expired 5s ago).
    expired_lease = Lease(
        lease_id="lease-account-a-old",
        profile="account-a",
        expires_at=datetime.now(UTC) - timedelta(seconds=5),
        created_at=datetime.now(UTC) - timedelta(seconds=65),
        creator="test",
    )
    p.write_text(json.dumps({"account-a": expired_lease.to_dict()}))

    respx_mock.post(TOKEN_URL).respond(
        200,
        json={"access_token": "new-A", "refresh_token": "new-R", "expires_in": 3600},
    )

    with patch("claude_lb.lease.get_active", side_effect=lambda name: get_active(name, path=p)):
        result = await refresh_profile(_profile(cred))

    assert result.refreshed is True


# ---------------------------------------------------------------------------
# exec lease context manager
# ---------------------------------------------------------------------------


def test_exec_lease_acquired_and_released(tmp_path: Path) -> None:
    """run_child with lease_profile=True acquires then releases the lease."""
    from unittest.mock import MagicMock, patch

    p = _lease_path(tmp_path)
    acquired_ids: list[str] = []
    released_ids: list[str] = []

    def _fake_acquire(profile, duration_s=1800, creator=None, path=None):
        l = acquire(profile, duration_s, creator=creator, path=p)
        acquired_ids.append(l.lease_id)
        return l

    def _fake_release(lease_id, path=None):
        released_ids.append(lease_id)
        return release(lease_id, path=p)

    # Patch at the lease module level — _maybe_lease imports from there.
    with (
        patch("claude_lb.lease.acquire", side_effect=_fake_acquire),
        patch("claude_lb.lease.release", side_effect=_fake_release),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        from claude_lb.exec_cmd import run_child
        result = run_child(
            ["echo", "hello"],
            env_var_name="AXIOM_CLAUDE_PROFILE",
            profile_name="account-a",
            timeout=None,
            lease_profile=True,
            lease_duration_s=60,
        )

    assert result.rc == 0
    assert len(acquired_ids) == 1
    assert acquired_ids == released_ids


def test_exec_no_lease_skips_lease() -> None:
    """run_child with lease_profile=False never touches the lease store."""
    from unittest.mock import MagicMock, patch

    with (
        patch("claude_lb.lease.acquire") as mock_acquire,
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        from claude_lb.exec_cmd import run_child
        run_child(
            ["echo", "hello"],
            env_var_name="AXIOM_CLAUDE_PROFILE",
            profile_name="account-a",
            timeout=None,
            lease_profile=False,
        )

    mock_acquire.assert_not_called()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_acquire_replaces_existing_lease(tmp_path: Path) -> None:
    """Acquiring a second lease on the same profile replaces the first."""
    p = _lease_path(tmp_path)
    first = acquire("account-a", 60, path=p)
    second = acquire("account-a", 60, path=p)
    assert first.lease_id != second.lease_id
    # Only the second lease is stored.
    found = get_active("account-a", path=p)
    assert found is not None
    assert found.lease_id == second.lease_id
    # Exactly one entry in the store.
    all_leases = list_leases(include_expired=True, path=p)
    assert len(all_leases) == 1


def test_acquire_with_zero_duration_is_immediately_expired(tmp_path: Path) -> None:
    """A lease with duration_s=0 is expired as soon as it's read back."""
    p = _lease_path(tmp_path)
    acquire("account-a", 0, path=p)
    found = get_active("account-a", path=p)
    assert found is None


def test_load_raw_survives_corrupted_json(tmp_path: Path) -> None:
    """A corrupted leases.json is treated as empty; acquire proceeds cleanly."""
    p = _lease_path(tmp_path)
    p.write_text("not valid json {{{{")
    # acquire should still succeed, overwriting the corrupted file.
    lease = acquire("account-a", 60, path=p)
    assert lease.is_active()
    found = get_active("account-a", path=p)
    assert found is not None
    assert found.lease_id == lease.lease_id


def test_load_raw_survives_wrong_type(tmp_path: Path) -> None:
    """A leases.json that contains a JSON array (not object) is treated as empty."""
    p = _lease_path(tmp_path)
    p.write_text("[1, 2, 3]")
    lease = acquire("account-a", 60, path=p)
    assert lease.is_active()


def test_get_active_ignores_malformed_record(tmp_path: Path) -> None:
    """A record missing required keys is treated as no lease."""
    p = _lease_path(tmp_path)
    import json as _json
    p.write_text(_json.dumps({"account-a": {"garbage": True}}))
    found = get_active("account-a", path=p)
    assert found is None


def test_release_by_profile_no_lease_returns_false(tmp_path: Path) -> None:
    """release_by_profile on a profile with no lease returns False without error."""
    p = _lease_path(tmp_path)
    removed = release_by_profile("account-a", path=p)
    assert removed is False


def test_parse_duration_zero_seconds() -> None:
    assert parse_duration("0s") == 0


def test_parse_duration_zero_bare() -> None:
    assert parse_duration("0") == 0


def test_parse_duration_negative_returns_negative() -> None:
    """Negative values parse successfully (int('-5') works); a negative-TTL lease
    is immediately expired — no separate rejection needed."""
    assert parse_duration("-5m") == -300


def test_purge_expired_noop_on_empty_store(tmp_path: Path) -> None:
    """purge_expired on a non-existent store returns 0 without creating the file."""
    p = _lease_path(tmp_path)
    count = purge_expired(path=p)
    assert count == 0
    assert not p.exists()
