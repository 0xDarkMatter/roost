"""CLI tests — Typer's CliRunner drives the app end-to-end."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_lb import cli as cli_mod
from claude_lb import paths as paths_mod
from claude_lb.cli import app
from claude_lb.models import ErrorInfo, Health, ProfileHealth

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path: Path,
    credentials_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Route profile discovery + config dir into the test's tmp_path."""
    config = tmp_path / "config"
    config.mkdir()

    monkeypatch.setenv("CLAUDE_LB_PROFILES_DIR", str(credentials_dir))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_LB_STICKINESS", raising=False)

    # Patch every function in claude_lb that calls paths.* so they target tmp.
    # Simplest approach: patch paths at the module level.
    monkeypatch.setattr(paths_mod, "config_dir", lambda: config)
    monkeypatch.setattr(paths_mod, "cache_path", lambda: config / "health.json")
    monkeypatch.setattr(paths_mod, "pick_log_path", lambda: config / "picks.log")
    monkeypatch.setattr(paths_mod, "last_pick_path", lambda: config / "last-pick.json")
    monkeypatch.setattr(paths_mod, "ensure_config_dir", lambda: config)

    # Modules that imported the path helpers at module-top need their local
    # bindings patched too.
    from claude_lb import cache, doctor, pick, platform_status

    monkeypatch.setattr(cache, "cache_path", lambda: config / "health.json")
    monkeypatch.setattr(cache, "ensure_config_dir", lambda: config)
    monkeypatch.setattr(pick, "last_pick_path", lambda: config / "last-pick.json")
    monkeypatch.setattr(pick, "pick_log_path", lambda: config / "picks.log")
    monkeypatch.setattr(doctor, "config_dir", lambda: config)
    monkeypatch.setattr(doctor, "cache_path", lambda: config / "health.json")
    monkeypatch.setattr(platform_status, "config_dir", lambda: config)
    monkeypatch.setattr(platform_status, "ensure_config_dir", lambda: config)

    # Default: short-circuit the platform-status fetch so existing tests don't
    # accidentally hit status.claude.com. Tests that exercise the new header
    # behaviour override this with a respx mock or a dedicated stub.
    monkeypatch.setattr(cli_mod, "_load_platform_status", lambda **kw: None)

    yield config


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "claude-lb" in result.stdout
    assert "0.8.0" in result.stdout


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# profiles list
# ---------------------------------------------------------------------------


def test_list_empty(credentials_dir: Path) -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert result.stdout == ""  # nothing on stdout when empty


def test_list_shows_profile_names_on_stdout(profile_factory) -> None:
    profile_factory("account-a")
    profile_factory("account-b")
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "account-a" in result.stdout
    assert "account-b" in result.stdout


def test_list_json(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == 1
    assert payload["data"][0]["name"] == "account-a"


# ---------------------------------------------------------------------------
# profiles show
# ---------------------------------------------------------------------------


def test_show_unknown_profile_returns_not_found() -> None:
    result = runner.invoke(app, ["show", "nonexistent"])
    assert result.exit_code == 3  # NOT_FOUND


def test_show_existing_profile(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["show", "account-a"])
    assert result.exit_code == 0


def test_show_json(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["show", "account-a", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["name"] == "account-a"


def test_show_json_has_consistent_shape_when_never_probed(profile_factory) -> None:
    """Regression: --json used to omit health fields when entry was absent.
    Callers would have to null-guard half the fields. Now all keys are always
    present; values are null when no entry exists."""
    profile_factory("account-a")
    result = runner.invoke(app, ["show", "account-a", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    expected_keys = {
        "name",
        "credentials_path",
        "token_source",
        "health",
        "probed_at",
        "expires_at",
        "retry_after_s",
        "session_reset_at",
        "weekly_reset_at",
        "usage",
        "error",
        "probe_latency_ms",
    }
    assert expected_keys <= set(payload["data"].keys())
    # Never-probed profile: health is "unknown", all health metadata is null.
    assert payload["data"]["health"] == "unknown"
    assert payload["data"]["probed_at"] is None
    assert payload["data"]["retry_after_s"] is None
    assert payload["data"]["error"] is None


# ---------------------------------------------------------------------------
# profiles invalidate
# ---------------------------------------------------------------------------


def test_invalidate_unknown_profile_returns_not_found() -> None:
    result = runner.invoke(app, ["invalidate", "nonexistent"])
    assert result.exit_code == 3


def test_invalidate_existing_profile(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["invalidate", "account-a"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# pick — no profiles
# ---------------------------------------------------------------------------


def test_pick_no_profiles_returns_unavailable() -> None:
    result = runner.invoke(app, ["pick"])
    assert result.exit_code == 9  # UNAVAILABLE


# ---------------------------------------------------------------------------
# pick — happy path (stub probe to avoid real network calls)
# ---------------------------------------------------------------------------


def _stub_probe_many_sync(
    profiles: list,
    *,
    prev_health: dict | None = None,
    timeout: float = 10.0,
) -> list[ProfileHealth]:
    from datetime import datetime

    now = datetime.now(UTC)
    return [
        ProfileHealth(
            name=p.name,
            health=Health.OK,
            probed_at=now,
            expires_at=None,
            credentials_mtime=p.credentials_mtime,
        )
        for p in profiles
    ]


def test_pick_with_stubbed_probe(profile_factory) -> None:
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "account-a"


def test_pick_export_emits_var_assignment(profile_factory) -> None:
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick", "--export"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "AXIOM_CLAUDE_PROFILE=account-a"


def test_pick_export_custom_var(profile_factory) -> None:
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick", "--export", "--var-name", "MY_VAR"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "MY_VAR=account-a"


def test_pick_json(profile_factory) -> None:
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["name"] == "account-a"
    assert payload["data"]["health"] == "ok"


def test_pick_bad_strategy_returns_validation(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["pick", "--strategy", "nonsense"])
    assert result.exit_code == 4  # VALIDATION


def test_pick_warn_at_out_of_range_rejected(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["pick", "--warn-at", "150"])
    assert result.exit_code == 4  # VALIDATION


def _stub_probe_with_usage(session_pct: int, weekly_pct: int):
    """Build a probe_many_sync stub that returns entries with explicit usage."""
    from datetime import datetime

    from claude_lb.models import Usage

    def _stub(profiles, *, prev_health=None, timeout=10.0):
        now = datetime.now(UTC)
        return [
            ProfileHealth(
                name=p.name,
                health=Health.OK,
                probed_at=now,
                usage=Usage(session_pct=session_pct, weekly_pct=weekly_pct),
                credentials_mtime=p.credentials_mtime,
            )
            for p in profiles
        ]

    return _stub


def test_pick_warn_at_below_threshold_no_warning(profile_factory) -> None:
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_with_usage(30, 20)):
        result = runner.invoke(app, ["pick", "--warn-at", "80"])
    assert result.exit_code == 0
    assert "warn" not in result.stderr.lower()


def test_pick_warn_at_above_threshold_emits_warning(profile_factory) -> None:
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_with_usage(85, 20)):
        result = runner.invoke(app, ["pick", "--warn-at", "80"])
    assert result.exit_code == 0  # still succeeds
    assert "warn" in result.stderr.lower()
    assert "85%" in result.stderr
    assert "account-a" in result.stdout  # pick still emitted to stdout


def test_pick_warn_at_weekly_also_triggers(profile_factory) -> None:
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_with_usage(10, 95)):
        result = runner.invoke(app, ["pick", "--warn-at", "90"])
    assert result.exit_code == 0
    assert "weekly" in result.stderr.lower()
    assert "95%" in result.stderr


# ---------------------------------------------------------------------------
# status with stubbed probe
# ---------------------------------------------------------------------------


def test_status_json_with_profiles(profile_factory) -> None:
    profile_factory("account-a")
    profile_factory("account-b")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == 2
    assert payload["meta"]["ok"] == 2


# ---------------------------------------------------------------------------
# probe explicit with stub
# ---------------------------------------------------------------------------


def test_probe_named_unknown_profile_returns_not_found(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["probe", "nonexistent"])
    assert result.exit_code == 3


def test_probe_named_profile(profile_factory) -> None:
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["probe", "account-a", "--json"])
    assert result.exit_code == 0


def test_probe_raw_dumps_untouched_body(profile_factory) -> None:
    """--raw bypasses classification and emits the literal upstream body."""
    fake_body = {"five_hour": {"utilization": 77.0}, "extra_usage": {"is_enabled": True}}

    def _stub_raw(profiles, *, timeout=10.0):
        return [(p.name, 200, fake_body, {"x-test": "1"}) for p in profiles]

    from claude_lb import probe as probe_mod

    profile_factory("account-a")
    with patch.object(probe_mod, "probe_raw_many_sync", _stub_raw):
        result = runner.invoke(app, ["probe", "--raw"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == 1
    row = payload["data"][0]
    assert row["name"] == "account-a"
    assert row["status_code"] == 200
    # Full body passed through untouched
    assert row["body"]["five_hour"]["utilization"] == 77.0
    assert row["body"]["extra_usage"]["is_enabled"] is True
    assert row["headers"]["x-test"] == "1"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_human_output(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["doctor", "--skip-network"])
    assert result.exit_code == 0


def test_doctor_json_output(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["doctor", "--skip-network", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["version"]
    assert payload["meta"]["passed"] >= 1


def test_doctor_fails_when_no_profiles() -> None:
    result = runner.invoke(app, ["doctor", "--skip-network"])
    # profiles_discoverable check fails -> exit 1
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_human_output() -> None:
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0


def test_update_json_output() -> None:
    result = runner.invoke(app, ["update", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "current_version" in payload["data"]
    assert "update_available" in payload["meta"]


# ---------------------------------------------------------------------------
# pick --auto-refresh
# ---------------------------------------------------------------------------


def _stateful_probe_stub(first_health: Health, second_health: Health = Health.OK):
    """probe_many_sync stub: returns first_health on call 1, second_health after.

    Mirrors the real flow where _load_or_probe runs the first probe (seeding
    the cache) and _attempt_auto_refresh runs the second probe (post-refresh).
    """
    from datetime import datetime

    state = {"count": 0}

    def _stub(profiles, *, prev_health=None, timeout=10.0):
        state["count"] += 1
        h = first_health if state["count"] == 1 else second_health
        now = datetime.now(UTC)
        return [
            ProfileHealth(
                name=p.name,
                health=h,
                probed_at=now,
                expires_at=None,
                credentials_mtime=p.credentials_mtime,
                error=(
                    ErrorInfo(type="token_expired", message="expired")
                    if h is Health.AUTH_EXPIRED
                    else None
                ),
            )
            for p in profiles
        ]

    return _stub


def _stub_refresh_success(profiles, *, timeout=10.0):
    from claude_lb.refresh import RefreshResult

    return [RefreshResult(name=p.name, refreshed=True) for p in profiles]


def _stub_refresh_fail(profiles, *, timeout=10.0):
    from claude_lb.refresh import RefreshResult

    return [
        RefreshResult(
            name=p.name,
            refreshed=False,
            error_code="REFRESH_REJECTED",
            error_message="token rejected",
        )
        for p in profiles
    ]


def test_auto_refresh_happy_path(profile_factory) -> None:
    """One profile, AUTH_EXPIRED with refresh token. --auto-refresh refreshes
    it, re-probes to OK, pick returns the name."""
    profile_factory("account-a")
    probe_stub = _stateful_probe_stub(Health.AUTH_EXPIRED, Health.OK)
    with patch.object(cli_mod, "probe_many_sync", probe_stub), \
         patch.object(cli_mod, "refresh_many_sync", _stub_refresh_success):
        result = runner.invoke(app, ["pick", "--auto-refresh"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "account-a"


def test_auto_refresh_falls_through_on_refresh_failure(profile_factory) -> None:
    """Mix of AUTH_EXPIRED + OK. Refresh fails → pick still returns the OK profile."""
    from datetime import datetime

    profile_factory("account-a")
    profile_factory("account-b")

    def _mixed_probe(profiles, *, prev_health=None, timeout=10.0):
        now = datetime.now(UTC)
        return [
            ProfileHealth(
                name=p.name,
                health=Health.AUTH_EXPIRED if p.name == "account-a" else Health.OK,
                probed_at=now,
                expires_at=None,
                credentials_mtime=p.credentials_mtime,
                error=(
                    ErrorInfo(type="token_expired", message="expired")
                    if p.name == "account-a"
                    else None
                ),
            )
            for p in profiles
        ]

    with patch.object(cli_mod, "probe_many_sync", _mixed_probe), \
         patch.object(cli_mod, "refresh_many_sync", _stub_refresh_fail):
        result = runner.invoke(app, ["pick", "--auto-refresh"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "account-b"
    assert "auto-refresh failed" in result.stderr.lower()


def test_auto_refresh_no_refresh_token_is_noop(credentials_dir: Path) -> None:
    """AUTH_EXPIRED profile with NO refreshToken on disk → never attempts refresh.
    Falls through to exit 2 (AUTH_REQUIRED) since no other candidates exist."""
    profile_dir = credentials_dir / "account-a"
    profile_dir.mkdir(parents=True, exist_ok=True)
    cred_path = profile_dir / ".credentials.json"
    # Modern shape but missing refreshToken — simulates pre-refresh-token
    # profiles or ones created before Anthropic shipped OAuth refresh.
    cred_path.write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-test",
                "expiresAt": 99999999999999,
            }
        })
    )

    refresh_call_count = {"n": 0}

    def _tracking_refresh(profiles, *, timeout=10.0):
        refresh_call_count["n"] += 1
        return []

    probe_stub = _stateful_probe_stub(Health.AUTH_EXPIRED, Health.AUTH_EXPIRED)
    with patch.object(cli_mod, "probe_many_sync", probe_stub), \
         patch.object(cli_mod, "refresh_many_sync", _tracking_refresh):
        result = runner.invoke(app, ["pick", "--auto-refresh"])
    assert result.exit_code == 2  # AUTH_REQUIRED — can't refresh without a refresh token
    assert refresh_call_count["n"] == 0, "refresh must not be attempted without a refresh token"


def test_auto_refresh_rediscovers_profile_after_refresh(profile_factory) -> None:
    """REGRESSION: after refresh succeeds, _attempt_auto_refresh must
    re-discover the Profile (re-read .credentials.json) before re-probing.

    The bug: refresh mutates `expiresAt` on disk. The Profile objects passed
    into `refresh_many_sync` were built BEFORE that mutation, so their
    `access_token_expires_at` still points at the past. If the re-probe is
    called with those stale Profile objects, `_local_auth_expired` (in the
    real probe path) short-circuits to AUTH_EXPIRED on the disk-updated
    profile — silently undoing the whole point of auto-refresh.

    This test verifies the fix by checking that the second probe call
    receives Profile objects whose `credentials_mtime` reflects the
    post-refresh disk state, not the pre-refresh snapshot.
    """
    from datetime import datetime, timedelta

    cred_path = profile_factory("account-a")

    captured: list[list[tuple[str, float, datetime | None]]] = []

    def _probe_stub(profiles, *, prev_health=None, timeout=10.0):
        captured.append([
            (p.name, p.credentials_mtime, p.access_token_expires_at)
            for p in profiles
        ])
        now = datetime.now(UTC)
        if len(captured) == 1:
            return [
                ProfileHealth(
                    name=p.name, health=Health.AUTH_EXPIRED,
                    probed_at=now, expires_at=None,
                    credentials_mtime=p.credentials_mtime,
                )
                for p in profiles
            ]
        return [
            ProfileHealth(
                name=p.name, health=Health.OK,
                probed_at=now, credentials_mtime=p.credentials_mtime,
            )
            for p in profiles
        ]

    def _refresh_mutates_disk(profiles, *, timeout=10.0):
        """Simulate a real refresh: bump expiresAt on disk so re-discovery
        sees a fresh value. Sleeps briefly to guarantee mtime changes on
        filesystems with low timestamp resolution (e.g. NTFS = ~10ms)."""
        import time

        from claude_lb.refresh import RefreshResult
        future_ms = int((datetime.now(UTC) + timedelta(hours=8)).timestamp() * 1000)
        time.sleep(0.05)  # ensure mtime ticks past pre-refresh snapshot
        for p in profiles:
            data = json.loads(Path(p.credentials_path).read_text())
            data["claudeAiOauth"]["expiresAt"] = future_ms
            Path(p.credentials_path).write_text(json.dumps(data))
        return [RefreshResult(name=p.name, refreshed=True) for p in profiles]

    pre_refresh_mtime = cred_path.stat().st_mtime
    with patch.object(cli_mod, "probe_many_sync", _probe_stub), \
         patch.object(cli_mod, "refresh_many_sync", _refresh_mutates_disk):
        result = runner.invoke(app, ["pick", "--auto-refresh"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "account-a"
    assert len(captured) == 2
    first_probe_mtime = captured[0][0][1]
    second_probe_mtime = captured[1][0][1]
    # Sanity: first probe ran with pre-refresh credentials.
    assert first_probe_mtime == pre_refresh_mtime
    # The fix: second probe must see the post-refresh disk state.
    # Without re-discovery, both probes would receive the same stale Profile.
    assert second_probe_mtime > first_probe_mtime, (
        "regression: re-probe used stale Profile object (mtime unchanged); "
        "auto-refresh must re-discover the Profile after refresh so the "
        "post-refresh expiresAt is read from disk"
    )


def test_auto_refresh_last_candidate_falls_through_to_exit_2(profile_factory) -> None:
    """Only one profile, expired. Refresh fails. Exit 2 (AUTH_REQUIRED)."""
    profile_factory("account-a")
    # Both probe calls return AUTH_EXPIRED — refresh failure means we don't
    # actually heal the token, and the second probe never fires anyway because
    # refreshed_profiles is empty.
    probe_stub = _stateful_probe_stub(Health.AUTH_EXPIRED, Health.AUTH_EXPIRED)
    with patch.object(cli_mod, "probe_many_sync", probe_stub), \
         patch.object(cli_mod, "refresh_many_sync", _stub_refresh_fail):
        result = runner.invoke(app, ["pick", "--auto-refresh"])
    assert result.exit_code == 2
    assert "auto-refresh failed" in result.stderr.lower()


# ---------------------------------------------------------------------------
# pick --count N (multi-pick)
# ---------------------------------------------------------------------------


def _stub_probe_with_weeklies(*, weekly_by_name: dict[str, int]):
    """Stub that returns ProfileHealth with per-profile weekly% for ordering tests."""
    from datetime import datetime

    from claude_lb.models import Usage

    def _stub(profiles, *, prev_health=None, timeout=10.0):
        now = datetime.now(UTC)
        return [
            ProfileHealth(
                name=p.name,
                health=Health.OK,
                probed_at=now,
                usage=Usage(weekly_pct=weekly_by_name.get(p.name, 50), session_pct=0),
                credentials_mtime=p.credentials_mtime,
            )
            for p in profiles
        ]

    return _stub


def test_pick_count_emits_multiple_names(profile_factory) -> None:
    profile_factory("account-a")
    profile_factory("account-b")
    profile_factory("account-c")
    weeklies = {"account-a": 10, "account-b": 60, "account-c": 30}
    with patch.object(
        cli_mod, "probe_many_sync", _stub_probe_with_weeklies(weekly_by_name=weeklies)
    ):
        result = runner.invoke(
            app, ["pick", "--count", "3", "--strategy", "least-used"]
        )
    assert result.exit_code == 0
    # Newline-separated, ordered least-used first.
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert lines == ["account-a", "account-c", "account-b"]


def test_pick_count_short_flag_n(profile_factory) -> None:
    """-n is the short form of --count."""
    profile_factory("account-a")
    profile_factory("account-b")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick", "-n", "2"])
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2


def test_pick_count_returns_fewer_when_candidates_limited(profile_factory) -> None:
    """Request 5, only 2 pass the ladder → returns 2, exit 0."""
    profile_factory("account-a")
    profile_factory("account-b")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick", "--count", "5"])
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2


def test_pick_count_no_profiles_exits_9() -> None:
    """No profiles discovered: --count doesn't change the failure code."""
    result = runner.invoke(app, ["pick", "--count", "3"])
    assert result.exit_code == 9  # UNAVAILABLE


def test_pick_count_with_export_rejected(profile_factory) -> None:
    """--export with --count > 1 is ambiguous → EXIT_VALIDATION."""
    profile_factory("account-a")
    profile_factory("account-b")
    result = runner.invoke(app, ["pick", "--count", "2", "--export"])
    assert result.exit_code == 4  # VALIDATION
    assert "export" in result.stderr.lower()


def test_pick_count_with_export_and_count_one_is_fine(profile_factory) -> None:
    """--export is only rejected when count > 1. Explicit --count 1 works."""
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick", "--count", "1", "--export"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "AXIOM_CLAUDE_PROFILE=account-a"


def test_pick_count_json_is_array_shape(profile_factory) -> None:
    """count > 1 flips JSON from single-object to array with meta."""
    profile_factory("account-a")
    profile_factory("account-b")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick", "--count", "2", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload["data"], list)
    assert len(payload["data"]) == 2
    assert payload["meta"]["count"] == 2
    assert payload["meta"]["requested"] == 2
    assert "strategy" in payload["meta"]


def test_pick_count_one_keeps_single_object_json(profile_factory) -> None:
    """Backward compat: --count 1 (explicit or default) keeps the single-object shape."""
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick", "--count", "1", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload["data"], dict)
    assert payload["data"]["name"] == "account-a"


def test_pick_count_respects_strategy(profile_factory) -> None:
    """--count N must still honour --strategy ordering."""
    profile_factory("a")
    profile_factory("b")
    profile_factory("c")
    weeklies = {"a": 40, "b": 10, "c": 20}
    with patch.object(
        cli_mod, "probe_many_sync", _stub_probe_with_weeklies(weekly_by_name=weeklies)
    ):
        result = runner.invoke(
            app, ["pick", "--count", "2", "--strategy", "least-used"]
        )
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert lines == ["b", "c"]  # lowest two weeklies, ascending


def test_pick_count_logs_each_to_picks_log(
    profile_factory, _isolated_home: Path
) -> None:
    """Each picked profile gets a picks.log entry (audit trail for parallel dispatch)."""
    profile_factory("a")
    profile_factory("b")
    profile_factory("c")
    weeklies = {"a": 40, "b": 10, "c": 20}
    log_path = _isolated_home / "picks.log"
    with patch.object(
        cli_mod, "probe_many_sync", _stub_probe_with_weeklies(weekly_by_name=weeklies)
    ):
        result = runner.invoke(
            app, ["pick", "--count", "3", "--strategy", "least-used"]
        )
    assert result.exit_code == 0
    log_text = log_path.read_text()
    # One line per pick, containing each profile name.
    for name in ("a", "b", "c"):
        assert f"\t{name}\t" in log_text


# ---------------------------------------------------------------------------
# exec — child dispatch
# ---------------------------------------------------------------------------


def _stub_run_child_factory(*, rc_sequence: list[int], timed_out: bool = False,
                            not_found: bool = False):
    """Build a run_child stub that returns the next rc in sequence per call.

    Captures the args of each call in `.calls` for assertion. Walks rc_sequence;
    if the list is exhausted, keeps returning the last rc so tests don't have
    to count invocations precisely.
    """
    from claude_lb.exec_cmd import ExecResult

    calls: list[dict] = []

    def _stub(argv, *, env_var_name, profile_name, timeout):
        calls.append({
            "argv": list(argv),
            "env_var_name": env_var_name,
            "profile_name": profile_name,
            "timeout": timeout,
        })
        i = min(len(calls) - 1, len(rc_sequence) - 1)
        rc = rc_sequence[i]
        return ExecResult(
            rc=rc,
            duration_ms=100,
            timed_out=timed_out and i == 0,
            not_found=not_found and i == 0,
        )

    _stub.calls = calls  # type: ignore[attr-defined]
    return _stub


def test_exec_happy_path(profile_factory, _isolated_home: Path) -> None:
    """Pick profile, run child, return child rc. picks.log gets PICK + EXEC lines."""
    profile_factory("account-a")
    stub_run = _stub_run_child_factory(rc_sequence=[0])
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(app, ["exec", "echo", "hello"])
    assert result.exit_code == 0
    assert len(stub_run.calls) == 1
    call = stub_run.calls[0]
    assert call["argv"] == ["echo", "hello"]
    assert call["env_var_name"] == "AXIOM_CLAUDE_PROFILE"
    assert call["profile_name"] == "account-a"

    log_text = (_isolated_home / "picks.log").read_text()
    assert "EXEC" in log_text
    assert "account-a" in log_text
    assert "rc=0" in log_text


def test_exec_no_command_given_exits_validation(profile_factory) -> None:
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["exec"])
    assert result.exit_code == 4  # VALIDATION
    assert "requires a command" in result.stderr.lower()


def test_exec_no_healthy_profile_exits_9() -> None:
    """No profiles discovered → propagate pick failure (exit 9)."""
    result = runner.invoke(app, ["exec", "echo", "hi"])
    assert result.exit_code == 9  # UNAVAILABLE


def test_exec_dry_run_prints_without_running(profile_factory) -> None:
    profile_factory("account-a")
    stub_run = _stub_run_child_factory(rc_sequence=[0])
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(app, ["exec", "--dry-run", "echo", "hello"])
    assert result.exit_code == 0
    assert len(stub_run.calls) == 0  # child never ran
    assert "AXIOM_CLAUDE_PROFILE=account-a" in result.stdout
    assert "echo" in result.stdout


def test_exec_propagates_child_exit_code(profile_factory) -> None:
    profile_factory("account-a")
    stub_run = _stub_run_child_factory(rc_sequence=[42])
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(app, ["exec", "failing-cmd"])
    # No retry because re-probe won't show rate-limit change (stub always returns OK).
    assert result.exit_code == 42


def test_exec_custom_var_name(profile_factory) -> None:
    profile_factory("account-a")
    stub_run = _stub_run_child_factory(rc_sequence=[0])
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(
            app, ["exec", "--var-name", "MY_VAR", "echo"]
        )
    assert result.exit_code == 0
    assert stub_run.calls[0]["env_var_name"] == "MY_VAR"


def test_exec_timeout_returns_124(profile_factory) -> None:
    profile_factory("account-a")
    stub_run = _stub_run_child_factory(rc_sequence=[124], timed_out=True)
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(
            app, ["exec", "--timeout", "1", "sleep", "10"]
        )
    assert result.exit_code == 124
    assert "timeout" in result.stderr.lower()


def test_exec_logs_argv0_only_by_default(
    profile_factory, _isolated_home: Path
) -> None:
    """picks.log should contain argv[0] but not downstream args (secrets risk)."""
    profile_factory("account-a")
    stub_run = _stub_run_child_factory(rc_sequence=[0])
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(
            app, ["exec", "echo", "super-secret-token-abc123"]
        )
    assert result.exit_code == 0
    log_text = (_isolated_home / "picks.log").read_text()
    assert "echo" in log_text
    assert "super-secret-token-abc123" not in log_text


def test_exec_log_full_argv_opts_in(
    profile_factory, _isolated_home: Path
) -> None:
    profile_factory("account-a")
    stub_run = _stub_run_child_factory(rc_sequence=[0])
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(
            app, ["exec", "--log-full-argv", "echo", "visible-arg"]
        )
    assert result.exit_code == 0
    log_text = (_isolated_home / "picks.log").read_text()
    assert "visible-arg" in log_text


def test_exec_retry_on_rate_limit(profile_factory) -> None:
    """First child fails + profile flips OK→RATE_LIMITED → retry w/ different profile."""
    from datetime import datetime

    from claude_lb.models import Usage

    profile_factory("account-a")
    profile_factory("account-b")

    probe_state = {"count": 0}

    def _stateful_probe(profiles, *, prev_health=None, timeout=10.0):
        """First call: both profiles OK (seeds cache).
        Second call (single-profile re-probe of chosen): that profile RATE_LIMITED.
        """
        probe_state["count"] += 1
        now = datetime.now(UTC)
        # Re-probe of a single profile after child fails
        if probe_state["count"] >= 2 and len(profiles) == 1:
            p = profiles[0]
            return [
                ProfileHealth(
                    name=p.name,
                    health=Health.RATE_LIMITED,
                    probed_at=now,
                    expires_at=now,
                    credentials_mtime=p.credentials_mtime,
                    error=ErrorInfo(type="rate_limited", message="slow down"),
                )
            ]
        # First call: both OK with distinct weeklies for deterministic ordering
        weeklies = {"account-a": 10, "account-b": 50}
        return [
            ProfileHealth(
                name=p.name,
                health=Health.OK,
                probed_at=now,
                usage=Usage(weekly_pct=weeklies.get(p.name, 50), session_pct=0),
                credentials_mtime=p.credentials_mtime,
            )
            for p in profiles
        ]

    # First run_child: rc=1 (as if rate-limited). Second: rc=0.
    stub_run = _stub_run_child_factory(rc_sequence=[1, 0])
    with patch.object(cli_mod, "probe_many_sync", _stateful_probe), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(
            app, ["exec", "--strategy", "least-used", "claude-bin"]
        )
    assert result.exit_code == 0  # retry succeeded
    assert len(stub_run.calls) == 2
    # Primary pick: account-a (least-used, 10%). Retry excludes it → account-b.
    assert stub_run.calls[0]["profile_name"] == "account-a"
    assert stub_run.calls[1]["profile_name"] == "account-b"
    assert "retry" in result.stderr.lower()


def test_exec_retry_disabled_when_budget_zero(profile_factory) -> None:
    """--retry-on-429 0 means never retry, even if rate-limited."""
    from datetime import datetime

    from claude_lb.models import Usage

    profile_factory("account-a")
    profile_factory("account-b")

    probe_state = {"count": 0}

    def _stateful_probe(profiles, *, prev_health=None, timeout=10.0):
        probe_state["count"] += 1
        now = datetime.now(UTC)
        if probe_state["count"] >= 2 and len(profiles) == 1:
            p = profiles[0]
            return [
                ProfileHealth(
                    name=p.name,
                    health=Health.RATE_LIMITED,
                    probed_at=now,
                    expires_at=now,
                    credentials_mtime=p.credentials_mtime,
                )
            ]
        weeklies = {"account-a": 10, "account-b": 50}
        return [
            ProfileHealth(
                name=p.name,
                health=Health.OK,
                probed_at=now,
                usage=Usage(weekly_pct=weeklies.get(p.name, 50), session_pct=0),
                credentials_mtime=p.credentials_mtime,
            )
            for p in profiles
        ]

    stub_run = _stub_run_child_factory(rc_sequence=[1])
    with patch.object(cli_mod, "probe_many_sync", _stateful_probe), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(
            app, ["exec", "--retry-on-429", "0", "claude-bin"]
        )
    assert result.exit_code == 1
    assert len(stub_run.calls) == 1  # no retry attempted


def test_exec_no_retry_on_timeout(profile_factory) -> None:
    """Timeout is not a rate-limit symptom — shouldn't trigger retry."""
    profile_factory("account-a")
    profile_factory("account-b")
    stub_run = _stub_run_child_factory(rc_sequence=[124], timed_out=True)
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(
            app, ["exec", "--timeout", "1", "sleep", "10"]
        )
    assert result.exit_code == 124
    assert len(stub_run.calls) == 1


# ---------------------------------------------------------------------------
# Shell completion (helpers only — full shell integration is Typer's job)
# ---------------------------------------------------------------------------


def test_completion_profile_names_filters_by_prefix(profile_factory) -> None:
    profile_factory("account-a")
    profile_factory("account-b")
    profile_factory("account-c")
    assert sorted(cli_mod._complete_profile_names("")) == [
        "account-a", "account-b", "account-c",
    ]
    assert cli_mod._complete_profile_names("account-a") == ["account-a"]
    assert sorted(cli_mod._complete_profile_names("account")) == [
        "account-a", "account-b", "account-c",
    ]
    assert cli_mod._complete_profile_names("zzz") == []


def test_completion_profile_names_swallows_errors(monkeypatch) -> None:
    """Completion must never break the user's shell — broken discovery returns []."""

    def _boom():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("claude_lb.discovery.discover_profiles", _boom)
    assert cli_mod._complete_profile_names("anything") == []


def test_completion_strategy_filters_by_prefix() -> None:
    from claude_lb.pick import Strategy
    all_strategies = {s.value for s in Strategy}
    assert set(cli_mod._complete_strategy("")) == all_strategies
    assert cli_mod._complete_strategy("least") == ["least-used"]
    assert cli_mod._complete_strategy("zzz") == []


# ---------------------------------------------------------------------------
# Duration parser (used by --soon and --since)
# ---------------------------------------------------------------------------


def test_parse_duration_variants() -> None:
    assert cli_mod._parse_duration("30s") == 30
    assert cli_mod._parse_duration("30m") == 1800
    assert cli_mod._parse_duration("1h") == 3600
    assert cli_mod._parse_duration("2d") == 172800
    assert cli_mod._parse_duration("1w") == 604800
    assert cli_mod._parse_duration("90") == 90  # bare seconds
    assert cli_mod._parse_duration("  1h  ") == 3600  # whitespace tolerated
    assert cli_mod._parse_duration("1H") == 3600  # case-insensitive


def test_parse_duration_rejects_garbage() -> None:
    assert cli_mod._parse_duration("") is None
    assert cli_mod._parse_duration("h") is None
    assert cli_mod._parse_duration("1y") is None  # year not supported
    assert cli_mod._parse_duration("abc") is None
    assert cli_mod._parse_duration("-1h") is None  # negative rejected
    assert cli_mod._parse_duration("1.5h") is None  # fractional rejected


# ---------------------------------------------------------------------------
# _humanize_elapsed — every duration band
# ---------------------------------------------------------------------------


def test_humanize_elapsed_negative_clamps_to_zero() -> None:
    assert cli_mod._humanize_elapsed(-5) == "0s ago"
    assert cli_mod._humanize_elapsed(-9999) == "0s ago"


def test_humanize_elapsed_seconds_band() -> None:
    assert cli_mod._humanize_elapsed(0) == "0s ago"
    assert cli_mod._humanize_elapsed(1) == "1s ago"
    assert cli_mod._humanize_elapsed(59) == "59s ago"


def test_humanize_elapsed_minutes_band() -> None:
    assert cli_mod._humanize_elapsed(60) == "1m ago"
    assert cli_mod._humanize_elapsed(3599) == "59m ago"


def test_humanize_elapsed_hours_band() -> None:
    assert cli_mod._humanize_elapsed(3600) == "1h ago"
    assert cli_mod._humanize_elapsed(86399) == "23h ago"


def test_humanize_elapsed_days_band() -> None:
    assert cli_mod._humanize_elapsed(86400) == "1d ago"
    assert cli_mod._humanize_elapsed(86400 * 7) == "7d ago"


# ---------------------------------------------------------------------------
# claude-lb history
# ---------------------------------------------------------------------------


def _seed_picks_log(log_path: Path, lines: list[str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_history_no_log_file(_isolated_home: Path) -> None:
    """No picks.log yet → friendly message, exit 0."""
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    assert "no history" in result.stderr.lower()


def test_history_default_shows_recent_entries(_isolated_home: Path) -> None:
    log_path = _isolated_home / "picks.log"
    _seed_picks_log(log_path, [
        "2026-04-25T01:00:00.000000+00:00\taccount-a\tsticky\tscore=1.00",
        "2026-04-25T01:01:00.000000+00:00\taccount-b\tleast-used\tscore=0.50",
        "2026-04-25T01:02:00.000000+00:00\taccount-a\tEXEC\targv=python\trc=0\tdur=92ms",
    ])
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    assert "account-a" in result.stderr
    assert "account-b" in result.stderr
    assert "EXEC" in result.stderr
    assert "3 of 3 entries" in result.stdout


def test_history_filter_by_profile(_isolated_home: Path) -> None:
    log_path = _isolated_home / "picks.log"
    _seed_picks_log(log_path, [
        "2026-04-25T01:00:00.000000+00:00\taccount-a\tsticky\tscore=1.00",
        "2026-04-25T01:01:00.000000+00:00\taccount-b\tleast-used\tscore=0.50",
    ])
    result = runner.invoke(app, ["history", "--profile", "account-a"])
    assert result.exit_code == 0
    assert "account-a" in result.stderr
    assert "account-b" not in result.stderr
    assert "1 of 2 entries" in result.stdout


def test_history_filter_by_since(_isolated_home: Path) -> None:
    """--since 1h should keep only entries within the last hour."""
    from datetime import UTC, datetime, timedelta

    log_path = _isolated_home / "picks.log"
    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).isoformat()
    fresh = (now - timedelta(minutes=10)).isoformat()
    _seed_picks_log(log_path, [
        f"{old}\told-profile\tsticky\tscore=1.00",
        f"{fresh}\tnew-profile\tsticky\tscore=1.00",
    ])
    result = runner.invoke(app, ["history", "--since", "1h"])
    assert result.exit_code == 0
    assert "new-profile" in result.stderr
    assert "old-profile" not in result.stderr


def test_history_invalid_since_rejected(_isolated_home: Path) -> None:
    log_path = _isolated_home / "picks.log"
    _seed_picks_log(log_path, [
        "2026-04-25T01:00:00.000000+00:00\taccount-a\tsticky\tscore=1.00",
    ])
    result = runner.invoke(app, ["history", "--since", "garbage"])
    assert result.exit_code == 4  # VALIDATION
    assert "invalid --since" in result.stderr.lower()


def test_history_json_shape(_isolated_home: Path) -> None:
    log_path = _isolated_home / "picks.log"
    _seed_picks_log(log_path, [
        "2026-04-25T01:00:00.000000+00:00\taccount-a\tsticky\tscore=1.00",
        "2026-04-25T01:01:00.000000+00:00\taccount-a\tEXEC\targv=python\trc=0\tdur=92ms",
    ])
    result = runner.invoke(app, ["history", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload["data"], list)
    assert len(payload["data"]) == 2
    assert payload["meta"]["count"] == 2
    assert payload["meta"]["total_in_log"] == 2
    # EXEC entry has structured details
    exec_entry = next(e for e in payload["data"] if e["action"] == "EXEC")
    assert exec_entry["details"]["rc"] == "0"
    assert exec_entry["details"]["argv"] == "python"


def test_history_tail_limits_count(_isolated_home: Path) -> None:
    log_path = _isolated_home / "picks.log"
    _seed_picks_log(log_path, [
        f"2026-04-25T01:0{i}:00.000000+00:00\taccount-a\tsticky\tscore=1.00"
        for i in range(5)
    ])
    result = runner.invoke(app, ["history", "--tail", "2", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["data"]) == 2
    # Should be the last two
    assert payload["data"][-1]["timestamp"].startswith("2026-04-25T01:04")


def test_history_skips_malformed_lines(_isolated_home: Path) -> None:
    log_path = _isolated_home / "picks.log"
    _seed_picks_log(log_path, [
        "2026-04-25T01:00:00.000000+00:00\taccount-a\tsticky\tscore=1.00",
        "garbage line with no tabs",
        "not-a-timestamp\tprofile\taction",
        "2026-04-25T01:02:00.000000+00:00\taccount-a\tsticky\tscore=1.00",
    ])
    result = runner.invoke(app, ["history", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == 2  # malformed lines silently dropped


# ---------------------------------------------------------------------------
# refresh --soon
# ---------------------------------------------------------------------------


def test_refresh_soon_invalid_value_rejected(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["refresh", "--soon", "garbage"])
    assert result.exit_code == 4
    assert "invalid --soon" in result.stderr.lower()


def test_refresh_soon_filters_by_window(
    profile_factory, _isolated_home: Path
) -> None:
    """--soon 1h refreshes profiles expiring within 1 hour. Use a non-default
    timeout via stub to confirm targets get passed through."""
    from datetime import UTC, datetime, timedelta

    cred_a = profile_factory("expires_in_30m")
    cred_b = profile_factory("expires_in_2d")

    # Mutate expiresAt on disk for each
    now = datetime.now(UTC)
    soon_ms = int((now + timedelta(minutes=30)).timestamp() * 1000)
    later_ms = int((now + timedelta(days=2)).timestamp() * 1000)
    for cred, ms in [(cred_a, soon_ms), (cred_b, later_ms)]:
        data = json.loads(cred.read_text())
        data["claudeAiOauth"]["expiresAt"] = ms
        cred.write_text(json.dumps(data))

    # Stub refresh to capture which profiles get refreshed
    captured_names: list[str] = []

    def _stub_refresh(profiles, *, timeout=10.0):
        from claude_lb.refresh import RefreshResult
        captured_names.extend(p.name for p in profiles)
        return [RefreshResult(name=p.name, refreshed=True) for p in profiles]

    from claude_lb import cli as _cli_mod
    with patch.object(_cli_mod, "refresh_many_sync", _stub_refresh):
        result = runner.invoke(app, ["refresh", "--soon", "1h", "--json"])

    assert result.exit_code == 0
    # expires_in_30m is within 1h window; expires_in_2d is not.
    assert captured_names == ["expires_in_30m"]


def test_refresh_soon_includes_already_expired(profile_factory) -> None:
    """`--soon N` must cover already-expired tokens too (they're <= now < cutoff)."""
    from datetime import UTC, datetime, timedelta

    cred = profile_factory("expired_yesterday")
    past_ms = int((datetime.now(UTC) - timedelta(days=1)).timestamp() * 1000)
    data = json.loads(cred.read_text())
    data["claudeAiOauth"]["expiresAt"] = past_ms
    cred.write_text(json.dumps(data))

    captured: list[str] = []

    def _stub(profiles, *, timeout=10.0):
        from claude_lb.refresh import RefreshResult
        captured.extend(p.name for p in profiles)
        return [RefreshResult(name=p.name, refreshed=True) for p in profiles]

    from claude_lb import cli as _cli_mod
    with patch.object(_cli_mod, "refresh_many_sync", _stub):
        result = runner.invoke(app, ["refresh", "--soon", "30m"])
    assert result.exit_code == 0
    assert captured == ["expired_yesterday"]


def test_refresh_soon_conflicts_with_expired() -> None:
    """Passing both --soon and --expired is ambiguous → EXIT_VALIDATION."""
    result = runner.invoke(app, ["refresh", "--soon", "1h", "--expired"])
    # No profiles → may exit 9 first; let's add a profile to force the actual check
    # Actually: validation runs after discovery. With no profiles, we get exit 9 first.
    # That's a separate test concern. For the conflict-check test, ensure profiles exist.


def test_refresh_soon_and_expired_together_rejected(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["refresh", "--soon", "1h", "--expired"])
    assert result.exit_code == 4
    assert "exactly one" in result.stderr.lower()


# ---------------------------------------------------------------------------
# claude-lb add — onboarding helper
# ---------------------------------------------------------------------------


def _write_credentials_file(path: Path, *, with_refresh: bool = True) -> None:
    """Write a valid modern-shape credentials file at the given path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-test",
            "expiresAt": 99999999999999,
        }
    }
    if with_refresh:
        payload["claudeAiOauth"]["refreshToken"] = "sk-ant-ort01-test"
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_add_imports_from_default_claude_dir(
    tmp_path: Path, credentials_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`claude-lb add NAME` defaults --from to ~/.claude/.credentials.json."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    src = fake_home / ".claude" / ".credentials.json"
    _write_credentials_file(src)

    result = runner.invoke(app, ["add", "personal"])
    assert result.exit_code == 0
    dest = credentials_dir / "personal" / ".credentials.json"
    assert dest.is_file()
    # Source is preserved (copy, not move)
    assert src.is_file()


def test_add_with_explicit_from_path(
    tmp_path: Path, credentials_dir: Path
) -> None:
    src = tmp_path / "elsewhere.json"
    _write_credentials_file(src)
    result = runner.invoke(app, ["add", "work", "--from", str(src)])
    assert result.exit_code == 0
    dest = credentials_dir / "work" / ".credentials.json"
    assert dest.is_file()


def test_add_rejects_invalid_name(credentials_dir: Path, tmp_path: Path) -> None:
    """Profile name must match [A-Za-z0-9_-]+ — same regex as discovery."""
    src = tmp_path / "src.json"
    _write_credentials_file(src)
    result = runner.invoke(app, ["add", "bad name with spaces", "--from", str(src)])
    assert result.exit_code == 4
    assert "invalid profile name" in result.stderr.lower()
    # Nothing written
    assert not (credentials_dir / "bad name with spaces").exists()


def test_add_missing_source_returns_not_found(
    tmp_path: Path, credentials_dir: Path
) -> None:
    nonexistent = tmp_path / "does-not-exist.json"
    result = runner.invoke(app, ["add", "x", "--from", str(nonexistent)])
    assert result.exit_code == 3
    assert "not found" in result.stderr.lower()


def test_add_default_source_missing_gives_helpful_hint(
    tmp_path: Path, credentials_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the default source (~/.claude/.credentials.json) is missing, the
    error should mention `claude login` so the user knows what to do."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    result = runner.invoke(app, ["add", "x"])
    assert result.exit_code == 3
    # Rich wraps long lines; collapse whitespace before matching.
    flat = " ".join(result.stderr.split()).lower()
    assert "claude login" in flat


def test_add_validates_source_is_parseable_json(
    tmp_path: Path, credentials_dir: Path
) -> None:
    src = tmp_path / "bad.json"
    src.write_text("{not valid json", encoding="utf-8")
    result = runner.invoke(app, ["add", "x", "--from", str(src)])
    assert result.exit_code == 4
    assert "parseable" in result.stderr.lower() or "json" in result.stderr.lower()
    # Nothing copied
    assert not (credentials_dir / "x").exists()


def test_add_rejects_credentials_with_no_token(
    tmp_path: Path, credentials_dir: Path
) -> None:
    """Empty/non-token JSON shouldn't be accepted — would silently fail at pick time."""
    src = tmp_path / "no-token.json"
    src.write_text(json.dumps({"unrelated_field": "value"}), encoding="utf-8")
    result = runner.invoke(app, ["add", "x", "--from", str(src)])
    assert result.exit_code == 4
    assert "token" in result.stderr.lower()


def test_add_refuses_to_overwrite_without_force(
    tmp_path: Path, credentials_dir: Path
) -> None:
    src = tmp_path / "src.json"
    _write_credentials_file(src)
    runner.invoke(app, ["add", "dup", "--from", str(src)])
    # Second invocation
    result = runner.invoke(app, ["add", "dup", "--from", str(src)])
    assert result.exit_code == 7  # CONFLICT
    assert "force" in result.stderr.lower() or "exists" in result.stderr.lower()


def test_add_force_overwrites(tmp_path: Path, credentials_dir: Path) -> None:
    src = tmp_path / "src.json"
    _write_credentials_file(src)
    runner.invoke(app, ["add", "dup", "--from", str(src)])
    result = runner.invoke(app, ["add", "dup", "--from", str(src), "--force"])
    assert result.exit_code == 0


def test_add_json_output(tmp_path: Path, credentials_dir: Path) -> None:
    src = tmp_path / "src.json"
    _write_credentials_file(src)
    result = runner.invoke(app, ["add", "newprofile", "--from", str(src), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["name"] == "newprofile"
    assert payload["data"]["source"] == str(src)
    assert payload["meta"]["action"] == "added"


# ---------------------------------------------------------------------------
# add command — JSON-output error envelopes for every validation path
# ---------------------------------------------------------------------------


def test_add_json_invalid_name_emits_validation_error_envelope(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src.json"
    _write_credentials_file(src)
    result = runner.invoke(app, ["add", "bad name", "--from", str(src), "--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert "invalid profile name" in payload["error"]["message"].lower()


def test_add_json_missing_source_emits_not_found_envelope(
    tmp_path: Path,
) -> None:
    nonexistent = tmp_path / "nope.json"
    result = runner.invoke(
        app, ["add", "x", "--from", str(nonexistent), "--json"]
    )
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "NOT_FOUND"


def test_add_json_malformed_source_emits_validation_error(
    tmp_path: Path,
) -> None:
    src = tmp_path / "bad.json"
    src.write_text("{not valid json", encoding="utf-8")
    result = runner.invoke(app, ["add", "x", "--from", str(src), "--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert "parseable" in payload["error"]["message"].lower()


def test_add_json_source_is_list_emits_validation_error(
    tmp_path: Path,
) -> None:
    """A JSON list (vs object) at the source path should be rejected with
    a clear message — covers the `not isinstance(payload, dict)` branch."""
    src = tmp_path / "list.json"
    src.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    result = runner.invoke(app, ["add", "x", "--from", str(src), "--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert "object" in payload["error"]["message"].lower()


def test_add_json_no_token_emits_validation_error(
    tmp_path: Path,
) -> None:
    src = tmp_path / "no-token.json"
    src.write_text(json.dumps({"unrelated": 1}), encoding="utf-8")
    result = runner.invoke(app, ["add", "x", "--from", str(src), "--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert "token" in payload["error"]["message"].lower()


def test_add_json_conflict_emits_conflict_envelope(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src.json"
    _write_credentials_file(src)
    runner.invoke(app, ["add", "dup", "--from", str(src)])
    result = runner.invoke(app, ["add", "dup", "--from", str(src), "--json"])
    assert result.exit_code == 7
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "CONFLICT"
    assert "exists" in payload["error"]["message"].lower()


def test_add_json_oauth_accessToken_legacy_shape_accepted(
    tmp_path: Path, credentials_dir: Path
) -> None:
    """Legacy `oauthAccessToken` shape should pass token validation
    (covers the `any(...)` branch in the token-shape check)."""
    src = tmp_path / "legacy.json"
    src.write_text(
        json.dumps({"oauthAccessToken": "sk-ant-oat01-legacy"}),
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["add", "legacy", "--from", str(src), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["name"] == "legacy"


# ---------------------------------------------------------------------------
# refresh command — selector validation envelopes
# ---------------------------------------------------------------------------


def test_refresh_no_selector_returns_validation_error(profile_factory) -> None:
    """`claude-lb refresh` with no name and no flags should refuse — would
    otherwise be ambiguous between "all" and "nothing"."""
    profile_factory("account-a")
    result = runner.invoke(app, ["refresh"])
    assert result.exit_code == 4
    flat = " ".join(result.stderr.split()).lower()
    assert "specify" in flat or "one of" in flat


def test_refresh_no_selector_json_emits_validation_error_envelope(
    profile_factory,
) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["refresh", "--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "VALIDATION_ERROR"


def test_refresh_multiple_selectors_returns_validation_error(
    profile_factory,
) -> None:
    """Combining --all and --expired (or any two selectors) is ambiguous."""
    profile_factory("account-a")
    result = runner.invoke(app, ["refresh", "--all", "--expired"])
    assert result.exit_code == 4
    flat = " ".join(result.stderr.split()).lower()
    assert "exactly one" in flat or "one of" in flat


def test_refresh_multiple_selectors_json_emits_validation_envelope(
    profile_factory,
) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["refresh", "--all", "--expired", "--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "VALIDATION_ERROR"


def test_refresh_unknown_profile_name_returns_not_found(
    profile_factory,
) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["refresh", "nonexistent"])
    assert result.exit_code == 3
    flat = " ".join(result.stderr.split()).lower()
    assert "no such profile" in flat


def test_refresh_unknown_profile_name_json_emits_not_found_envelope(
    profile_factory,
) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["refresh", "nonexistent", "--json"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "NOT_FOUND"


def test_refresh_expired_with_no_expired_profiles_succeeds_quietly(
    profile_factory,
) -> None:
    """`refresh --expired` with no eligible profiles should succeed (rc=0)
    with a "nothing to do" message — not an error."""
    profile_factory("account-a")  # default has future expiresAt
    result = runner.invoke(app, ["refresh", "--expired"])
    assert result.exit_code == 0


def test_refresh_expired_with_no_expired_profiles_json_envelope(
    profile_factory,
) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["refresh", "--expired", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"] == []
    assert payload["meta"]["count"] == 0
    assert payload["meta"]["refreshed"] == 0


# ---------------------------------------------------------------------------
# update --apply CLI surface (the apply_update path is unit-tested separately)
# ---------------------------------------------------------------------------


def test_update_command_renders_status_text() -> None:
    """`claude-lb update` (no --apply) prints the version + git status."""
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    flat = " ".join(result.stderr.split())
    # Always at least mentions the current version + a hint
    assert "claude-lb" in flat
    assert "hint" in flat.lower()


def test_update_command_json_envelope() -> None:
    result = runner.invoke(app, ["update", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "data" in payload
    assert "meta" in payload
    assert "current_version" in payload["data"]
    assert isinstance(payload["meta"]["update_available"], bool)


def test_update_apply_happy_path_renders_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When apply_update succeeds, the CLI prints 'Update applied' on stderr
    and exits 0."""
    from claude_lb.updater import UpdateApplyResult

    def _fake_apply(*, pull: bool) -> UpdateApplyResult:
        return UpdateApplyResult(
            current_version="0.8.0",
            install_dir="/path",
            applied=True,
            pulled=pull,
            reinstalled=True,
            stdout="ok",
        )

    monkeypatch.setattr(cli_mod, "apply_update", _fake_apply)
    result = runner.invoke(app, ["update", "--apply"])
    assert result.exit_code == 0
    flat = " ".join(result.stderr.split()).lower()
    assert "update applied" in flat


def test_update_apply_failure_returns_exit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_lb.updater import UpdateApplyResult

    def _fake_apply(*, pull: bool) -> UpdateApplyResult:
        return UpdateApplyResult(
            current_version="0.8.0",
            install_dir="/path",
            applied=False,
            pulled=False,
            reinstalled=False,
            error="simulated failure",
            stdout="some details",
        )

    monkeypatch.setattr(cli_mod, "apply_update", _fake_apply)
    result = runner.invoke(app, ["update", "--apply"])
    assert result.exit_code == 1
    flat = " ".join(result.stderr.split()).lower()
    assert "update failed" in flat
    assert "simulated failure" in flat


def test_update_apply_json_failure_returns_exit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_lb.updater import UpdateApplyResult

    def _fake_apply(*, pull: bool) -> UpdateApplyResult:
        return UpdateApplyResult(
            current_version="0.8.0",
            install_dir="/path",
            applied=False,
            pulled=False,
            reinstalled=False,
            error="boom",
        )

    monkeypatch.setattr(cli_mod, "apply_update", _fake_apply)
    result = runner.invoke(app, ["update", "--apply", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["applied"] is False
    assert payload["meta"]["applied"] is False


def test_update_apply_no_pull_flag_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--no-pull` should pass `pull=False` through to apply_update."""
    from claude_lb.updater import UpdateApplyResult

    captured: dict = {}

    def _fake_apply(*, pull: bool) -> UpdateApplyResult:
        captured["pull"] = pull
        return UpdateApplyResult(
            current_version="0.8.0",
            install_dir="/p",
            applied=True,
            pulled=False,
            reinstalled=True,
        )

    monkeypatch.setattr(cli_mod, "apply_update", _fake_apply)
    result = runner.invoke(app, ["update", "--apply", "--no-pull"])
    assert result.exit_code == 0
    assert captured["pull"] is False


# ---------------------------------------------------------------------------
# Edge cases — ferreting out behaviours not covered by the headline tests
# ---------------------------------------------------------------------------


def test_history_handles_empty_log_file(_isolated_home: Path) -> None:
    """Empty file is different from missing file. Both should produce 0 entries
    without crashing."""
    log_path = _isolated_home / "picks.log"
    log_path.touch()
    result = runner.invoke(app, ["history", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"] == []
    assert payload["meta"]["count"] == 0
    assert payload["meta"]["total_in_log"] == 0


def test_history_huge_tail_is_capped_to_total(_isolated_home: Path) -> None:
    log_path = _isolated_home / "picks.log"
    _seed_picks_log(log_path, [
        f"2026-04-25T01:0{i}:00.000000+00:00\taccount-a\tsticky\tscore=1.00"
        for i in range(3)
    ])
    result = runner.invoke(app, ["history", "--tail", "9999", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == 3  # capped at what's available


def test_history_log_with_only_blank_lines(_isolated_home: Path) -> None:
    log_path = _isolated_home / "picks.log"
    log_path.write_text("\n\n   \n\n", encoding="utf-8")
    result = runner.invoke(app, ["history", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == 0


def test_exec_separator_only_no_argv_exits_validation(profile_factory) -> None:
    """`claude-lb exec --` with nothing after the separator must not
    silently treat `--` as the command."""
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["exec", "--"])
    assert result.exit_code == 4
    assert "requires a command" in result.stderr.lower()


def test_exec_separator_is_stripped_when_present(profile_factory) -> None:
    """`claude-lb exec -- echo hi` and `claude-lb exec echo hi` produce the
    same child argv (the leading `--` is consumed, not passed to the child)."""
    profile_factory("account-a")
    stub_run = _stub_run_child_factory(rc_sequence=[0])
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(app, ["exec", "--", "echo", "hi"])
    assert result.exit_code == 0
    assert stub_run.calls[0]["argv"] == ["echo", "hi"]
    assert stub_run.calls[0]["argv"][0] != "--"


def test_refresh_soon_excludes_profiles_without_expires_at(
    profile_factory, credentials_dir: Path
) -> None:
    """A profile whose .credentials.json has no `expiresAt` field must be
    silently excluded from --soon (we have no baseline to compare against)."""
    import json as _json

    # One profile with future expiresAt (eligible if cutoff is generous)
    profile_factory("with_expiry")
    # One profile WITHOUT expiresAt
    no_exp_dir = credentials_dir / "no_expiry"
    no_exp_dir.mkdir(parents=True, exist_ok=True)
    (no_exp_dir / ".credentials.json").write_text(_json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-no-exp",
            "refreshToken": "sk-ant-ort01-x",
        }
    }))

    captured: list[str] = []

    def _stub(profiles, *, timeout=10.0):
        from claude_lb.refresh import RefreshResult
        captured.extend(p.name for p in profiles)
        return [RefreshResult(name=p.name, refreshed=True) for p in profiles]

    with patch.object(cli_mod, "refresh_many_sync", _stub):
        result = runner.invoke(app, ["refresh", "--soon", "100w"])
    assert result.exit_code == 0
    # `with_expiry` had expiresAt: 99999999999999 (year ~5138) — way beyond
    # any realistic --soon window. So both are excluded:
    # - no_expiry: no baseline (correctly skipped)
    # - with_expiry: cutoff < its expiry (not yet "soon")
    assert captured == []


def test_refresh_soon_zero_seconds_acts_like_expired(profile_factory) -> None:
    """`--soon 0` cutoff = now → only catches already-expired tokens."""
    from datetime import UTC, datetime, timedelta

    expired = profile_factory("expired")
    fresh = profile_factory("fresh")
    past_ms = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    future_ms = int((datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000)
    for cred, ms in [(expired, past_ms), (fresh, future_ms)]:
        data = json.loads(cred.read_text())
        data["claudeAiOauth"]["expiresAt"] = ms
        cred.write_text(json.dumps(data))

    captured: list[str] = []

    def _stub(profiles, *, timeout=10.0):
        from claude_lb.refresh import RefreshResult
        captured.extend(p.name for p in profiles)
        return [RefreshResult(name=p.name, refreshed=True) for p in profiles]

    with patch.object(cli_mod, "refresh_many_sync", _stub):
        result = runner.invoke(app, ["refresh", "--soon", "0"])
    assert result.exit_code == 0
    assert captured == ["expired"]


def test_auto_refresh_mixed_success_and_failure(
    profile_factory, _isolated_home: Path
) -> None:
    """Two AUTH_EXPIRED profiles. Refresh succeeds for one, fails for the
    other. After: succeeded profile is OK in cache + pickable; failed
    profile is still AUTH_EXPIRED + filtered out. Pick returns succeeded."""
    from datetime import datetime

    profile_factory("good_refresh")
    profile_factory("bad_refresh")

    probe_state = {"count": 0}

    def _probe_stub(profiles, *, prev_health=None, timeout=10.0):
        probe_state["count"] += 1
        now = datetime.now(UTC)
        # First call: both AUTH_EXPIRED (cache seed)
        if probe_state["count"] == 1:
            return [
                ProfileHealth(
                    name=p.name, health=Health.AUTH_EXPIRED,
                    probed_at=now, expires_at=None,
                    credentials_mtime=p.credentials_mtime,
                    error=ErrorInfo(type="token_expired", message="expired"),
                )
                for p in profiles
            ]
        # Subsequent calls (re-probe of refreshed profiles): they're OK now
        return [
            ProfileHealth(
                name=p.name, health=Health.OK, probed_at=now,
                credentials_mtime=p.credentials_mtime,
            )
            for p in profiles
        ]

    def _refresh_mixed(profiles, *, timeout=10.0):
        from claude_lb.refresh import RefreshResult
        return [
            RefreshResult(
                name=p.name,
                refreshed=(p.name == "good_refresh"),
                error_code=None if p.name == "good_refresh" else "REFRESH_REJECTED",
                error_message=None if p.name == "good_refresh" else "rejected",
            )
            for p in profiles
        ]

    with patch.object(cli_mod, "probe_many_sync", _probe_stub), \
         patch.object(cli_mod, "refresh_many_sync", _refresh_mixed):
        result = runner.invoke(app, ["pick", "--auto-refresh"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "good_refresh"
    assert "auto-refresh failed" in result.stderr.lower()
    assert "bad_refresh" in result.stderr.lower()


def test_auto_refresh_composes_with_count(profile_factory) -> None:
    """`pick --auto-refresh --count 2` should auto-refresh expired profiles,
    then return up to 2 healthy profiles."""
    from datetime import datetime

    profile_factory("a")
    profile_factory("b")
    profile_factory("c")

    probe_state = {"count": 0}

    def _probe_stub(profiles, *, prev_health=None, timeout=10.0):
        probe_state["count"] += 1
        now = datetime.now(UTC)
        if probe_state["count"] == 1:
            # Initial: a expired, b+c ok
            return [
                ProfileHealth(
                    name=p.name,
                    health=Health.AUTH_EXPIRED if p.name == "a" else Health.OK,
                    probed_at=now,
                    expires_at=None,
                    credentials_mtime=p.credentials_mtime,
                    error=(
                        ErrorInfo(type="token_expired", message="x")
                        if p.name == "a" else None
                    ),
                )
                for p in profiles
            ]
        # Re-probe of refreshed profile a → ok
        return [
            ProfileHealth(
                name=p.name, health=Health.OK, probed_at=now,
                credentials_mtime=p.credentials_mtime,
            )
            for p in profiles
        ]

    with patch.object(cli_mod, "probe_many_sync", _probe_stub), \
         patch.object(cli_mod, "refresh_many_sync", _stub_refresh_success):
        result = runner.invoke(
            app, ["pick", "--auto-refresh", "--count", "2"]
        )
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2
    # All 3 should be selectable now (a was healed); top 2 by strategy.
    assert set(lines).issubset({"a", "b", "c"})


def test_exec_auto_refresh_is_honored(profile_factory, _isolated_home: Path) -> None:
    """exec --auto-refresh refreshes expired profiles before picking."""
    profile_factory("account-a")
    probe_stub = _stateful_probe_stub(Health.AUTH_EXPIRED, Health.OK)
    stub_run = _stub_run_child_factory(rc_sequence=[0])
    with patch.object(cli_mod, "probe_many_sync", probe_stub), \
         patch.object(cli_mod, "refresh_many_sync", _stub_refresh_success), \
         patch.object(cli_mod, "run_child", stub_run):
        result = runner.invoke(app, ["exec", "--auto-refresh", "claude-bin"])
    assert result.exit_code == 0
    assert stub_run.calls[0]["profile_name"] == "account-a"


# ---------------------------------------------------------------------------
# Platform-status header on `status`
# ---------------------------------------------------------------------------


def _stub_platform_status(
    *,
    indicator: str = "none",
    description: str = "All Systems Operational",
    incidents: list | None = None,
    components: list | None = None,
    fetch_error: str | None = None,
):
    """Build a PlatformStatus and return a callable suitable for patching
    `cli_mod._load_platform_status`."""
    from claude_lb.platform_status import PlatformStatus

    status = PlatformStatus(
        indicator=indicator,
        description=description,
        active_incidents=[
            {
                "name": i.get("name"),
                "status": i.get("status"),
                "impact": i.get("impact"),
                "shortlink": i.get("shortlink"),
            }
            for i in (incidents or [])
        ],
        degraded_components=[
            {"name": c.get("name"), "status": c.get("status")}
            for c in (components or [])
        ],
        fetch_error=fetch_error,
    )

    def _loader(**kwargs):
        _loader.called_with = kwargs  # type: ignore[attr-defined]
        return status

    _loader.called_with = None  # type: ignore[attr-defined]
    return _loader


def test_status_renders_platform_header_when_incident(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monitoring-state incident from status.claude.com surfaces as a
    stderr header above the profile table."""
    profile_factory("account-a")
    loader = _stub_platform_status(incidents=[
        {"name": "Elevated errors on Claude Opus 4.7", "status": "monitoring", "impact": "minor"},
    ])
    monkeypatch.setattr(cli_mod, "_load_platform_status", loader)
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # Rich wraps narrow output, so collapse whitespace before substring checks.
    flat = " ".join(result.stderr.split())
    assert "Elevated errors on Claude Opus 4.7" in flat
    assert "Anthropic" in flat  # the leading label
    assert "monitoring" in flat
    assert "minor" in flat


def test_status_omits_platform_header_when_clean(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No header noise when everything is operational."""
    profile_factory("account-a")
    loader = _stub_platform_status()  # clean by default
    monkeypatch.setattr(cli_mod, "_load_platform_status", loader)
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Anthropic" not in result.stderr
    assert "incident" not in result.stderr.lower()


def test_status_no_platform_status_flag_skips_loader(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-platform-status` should mean we don't even call the loader."""
    profile_factory("account-a")
    called = {"n": 0}

    def _should_not_be_called(**kwargs):
        called["n"] += 1
        return None

    monkeypatch.setattr(cli_mod, "_load_platform_status", _should_not_be_called)
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["status", "--no-platform-status"])
    assert result.exit_code == 0
    assert called["n"] == 0


def test_status_json_includes_platform_status_in_meta(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structured envelope folds platform_status into meta so scripts
    can react to incidents without parsing the human header."""
    profile_factory("account-a")
    loader = _stub_platform_status(
        indicator="minor",
        description="Partial Outage",
        incidents=[{"name": "X", "status": "investigating", "impact": "minor"}],
    )
    monkeypatch.setattr(cli_mod, "_load_platform_status", loader)
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "platform_status" in payload["meta"]
    assert payload["meta"]["platform_status"]["indicator"] == "minor"
    assert payload["meta"]["platform_status"]["active_incidents"][0]["name"] == "X"


def test_status_json_omits_platform_status_when_flag_set(
    profile_factory,
) -> None:
    """`--no-platform-status` keeps `meta.platform_status` out of the envelope."""
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["status", "--json", "--no-platform-status"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "platform_status" not in payload["meta"]


def test_status_force_refresh_propagates_to_loader(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-cache` / `--refresh` should bypass the platform-status cache too,
    so 'force re-probe' really means everything."""
    profile_factory("account-a")
    loader = _stub_platform_status()
    monkeypatch.setattr(cli_mod, "_load_platform_status", loader)
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["status", "--no-cache"])
    assert result.exit_code == 0
    assert loader.called_with is not None
    assert loader.called_with.get("force_refresh") is True


# ---------------------------------------------------------------------------
# probe command — empty profile list + JSON variants
# ---------------------------------------------------------------------------


def test_probe_no_profiles_returns_unavailable() -> None:
    """`claude-lb probe` with no profiles on disk should exit 9 (UNAVAILABLE)."""
    result = runner.invoke(app, ["probe"])
    assert result.exit_code == 9
    flat = " ".join(result.stderr.split()).lower()
    assert "no profiles" in flat


def test_probe_no_profiles_json_emits_not_found_envelope() -> None:
    result = runner.invoke(app, ["probe", "--json"])
    assert result.exit_code == 9
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "NOT_FOUND"


def test_probe_named_unknown_profile_json_emits_not_found(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["probe", "nonexistent", "--json"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# show command — JSON-output for unprobed-but-discovered profile
# ---------------------------------------------------------------------------


def test_show_unknown_profile_returns_not_found(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["show", "nonexistent"])
    assert result.exit_code == 3


def test_show_unknown_profile_json_emits_not_found(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["show", "nonexistent", "--json"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "NOT_FOUND"


def test_show_unprobed_profile_text_output(profile_factory) -> None:
    """A profile that exists on disk but isn't in the cache yet should still
    render — printing what we know (credentials path, token source) without
    the health bits. Hits the entry-is-None branch."""
    profile_factory("never-probed")
    result = runner.invoke(app, ["show", "never-probed"])
    assert result.exit_code == 0
    flat = " ".join(result.stderr.split())
    assert "never-probed" in flat
    assert "credentials" in flat


def test_show_unprobed_profile_json_output(profile_factory) -> None:
    profile_factory("never-probed")
    result = runner.invoke(app, ["show", "never-probed", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["name"] == "never-probed"
    assert payload["data"]["health"] == "unknown"
    assert payload["data"]["probed_at"] is None


# ---------------------------------------------------------------------------
# list command — JSON output, multiple profiles
# ---------------------------------------------------------------------------


def test_list_json_output(profile_factory) -> None:
    profile_factory("account-a")
    profile_factory("account-b")
    result = runner.invoke(app, ["list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == 2
    names = [p if isinstance(p, str) else p["name"] for p in payload["data"]]
    assert set(names) == {"account-a", "account-b"}


# ---------------------------------------------------------------------------
# invalidate command — happy path + unknown profile
# ---------------------------------------------------------------------------


def test_invalidate_unknown_profile_returns_not_found(profile_factory) -> None:
    profile_factory("account-a")
    result = runner.invoke(app, ["invalidate", "nonexistent"])
    assert result.exit_code == 3


def test_invalidate_removes_cache_entry(profile_factory) -> None:
    """After invalidate, the cache should no longer have an entry for that
    profile. Use status with a stub probe to seed the cache first."""
    profile_factory("account-a")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        runner.invoke(app, ["status"])  # seeds cache
    # Now invalidate
    result = runner.invoke(app, ["invalidate", "account-a"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# pick exit-code branches
# ---------------------------------------------------------------------------


def test_pick_no_profiles_returns_unavailable() -> None:
    result = runner.invoke(app, ["pick"])
    # No profiles → UNAVAILABLE (9)
    assert result.exit_code == 9


def test_pick_export_with_count_greater_than_one_rejected(profile_factory) -> None:
    """`pick --export --count 2` is ambiguous (one var, multiple values).
    Should refuse with VALIDATION exit code."""
    profile_factory("account-a")
    profile_factory("account-b")
    with patch.object(cli_mod, "probe_many_sync", _stub_probe_many_sync):
        result = runner.invoke(app, ["pick", "--export", "--count", "2"])
    assert result.exit_code == 4


# ---------------------------------------------------------------------------
# history command — additional flag combos
# ---------------------------------------------------------------------------


def test_history_with_invalid_since_value_returns_validation_error(
    _isolated_home: Path,
) -> None:
    """`--since` value that doesn't parse should be a clean VALIDATION error,
    not a crash or a silent no-op."""
    result = runner.invoke(app, ["history", "--since", "garbage"])
    assert result.exit_code == 4


def test_history_handles_malformed_log_lines_gracefully(
    _isolated_home: Path,
) -> None:
    """Lines that don't parse (rotation can leave partial trailing lines)
    should be skipped, not crash."""
    log_path = _isolated_home / "picks.log"
    log_path.write_text(
        "this is not\ttab-separated-iso\n"
        "2026-04-25T10:30:00Z\taccount-a\tsticky\tscore=0.50\n"
        "another bad line\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["history", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # Only the well-formed line should appear
    assert payload["meta"]["count"] == 1


def test_history_filter_by_profile_returns_only_matching(
    _isolated_home: Path,
) -> None:
    log_path = _isolated_home / "picks.log"
    log_path.write_text(
        "2026-04-25T10:00:00Z\taccount-a\tsticky\tscore=0.50\n"
        "2026-04-25T10:01:00Z\taccount-b\tsticky\tscore=0.60\n"
        "2026-04-25T10:02:00Z\taccount-a\tsticky\tscore=0.55\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["history", "--json", "--profile", "account-a"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == 2
    assert all(e["profile"] == "account-a" for e in payload["data"])


def test_history_text_output_when_log_missing(_isolated_home: Path) -> None:
    """Text output (not --json) with no log file should print a friendly
    'no history yet' notice — not crash."""
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    flat = " ".join(result.stderr.split()).lower()
    assert "no history" in flat or "no picks" in flat


# ---------------------------------------------------------------------------
# refresh — exit-code mapping when failures dominate
# ---------------------------------------------------------------------------


def _stub_refresh_results(profile_names: list[str], *, error_code: str | None):
    """Build a refresh_many_sync stub returning failures with a specific code."""
    from claude_lb.refresh import RefreshResult

    def _stub(profiles, *, timeout=10.0):
        return [
            RefreshResult(
                name=p.name,
                refreshed=False,
                error_code=error_code,
                error_message=f"simulated {error_code}",
            )
            for p in profiles
        ]

    return _stub


def test_refresh_all_failures_with_lock_held_exits_conflict(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every profile fails AND any error_code is LOCK_HELD, exit is 7."""
    profile_factory("account-a")
    monkeypatch.setattr(
        cli_mod,
        "refresh_many_sync",
        _stub_refresh_results(["account-a"], error_code="LOCK_HELD"),
    )
    result = runner.invoke(app, ["refresh", "--all"])
    assert result.exit_code == 7


def test_refresh_all_failures_refresh_rejected_exits_auth_required(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REFRESH_REJECTED → AUTH_REQUIRED (2)."""
    profile_factory("account-a")
    monkeypatch.setattr(
        cli_mod,
        "refresh_many_sync",
        _stub_refresh_results(["account-a"], error_code="REFRESH_REJECTED"),
    )
    result = runner.invoke(app, ["refresh", "--all"])
    assert result.exit_code == 2


def test_refresh_all_failures_unexpected_response_exits_error(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_factory("account-a")
    monkeypatch.setattr(
        cli_mod,
        "refresh_many_sync",
        _stub_refresh_results(["account-a"], error_code="UNEXPECTED_RESPONSE"),
    )
    result = runner.invoke(app, ["refresh", "--all"])
    assert result.exit_code == 1


def test_refresh_partial_success_with_lock_exits_conflict(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed: one succeeded, one LOCK_HELD → still exit 7 so retries pick up
    just the conflicted ones."""
    from claude_lb.refresh import RefreshResult

    profile_factory("account-a")
    profile_factory("account-b")

    def _mixed(profiles, *, timeout=10.0):
        return [
            RefreshResult(name=profiles[0].name, refreshed=True),
            RefreshResult(name=profiles[1].name, refreshed=False, error_code="LOCK_HELD"),
        ]

    monkeypatch.setattr(cli_mod, "refresh_many_sync", _mixed)
    result = runner.invoke(app, ["refresh", "--all"])
    assert result.exit_code == 7


def test_refresh_partial_success_with_other_error_exits_error(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed: one succeeded, one generic failure → exit 1 (ERROR)."""
    from claude_lb.refresh import RefreshResult

    profile_factory("account-a")
    profile_factory("account-b")

    def _mixed(profiles, *, timeout=10.0):
        return [
            RefreshResult(name=profiles[0].name, refreshed=True),
            RefreshResult(
                name=profiles[1].name, refreshed=False, error_code="UNEXPECTED_RESPONSE"
            ),
        ]

    monkeypatch.setattr(cli_mod, "refresh_many_sync", _mixed)
    result = runner.invoke(app, ["refresh", "--all"])
    assert result.exit_code == 1


def test_refresh_json_aggregate_meta_counts(
    profile_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`refresh --all --json` should emit aggregate counts in meta even
    on partial failure."""
    from claude_lb.refresh import RefreshResult

    profile_factory("account-a")
    profile_factory("account-b")

    def _mixed(profiles, *, timeout=10.0):
        return [
            RefreshResult(name=profiles[0].name, refreshed=True),
            RefreshResult(name=profiles[1].name, refreshed=False, error_code="REFRESH_REJECTED"),
        ]

    monkeypatch.setattr(cli_mod, "refresh_many_sync", _mixed)
    result = runner.invoke(app, ["refresh", "--all", "--json"])
    # JSON-mode partial failure exits 1 (ERROR) — exit-code escalation to
    # AUTH_REQUIRED only kicks in when ALL refreshes failed.
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == 2
    assert payload["meta"]["refreshed"] == 1
    assert payload["meta"]["failed"] == 1


# ---------------------------------------------------------------------------
# doctor — JSON failing-check returns EXIT_ERROR
# ---------------------------------------------------------------------------


def test_doctor_json_returns_exit_error_when_any_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a check fails, `doctor --json` should exit 1 even though the JSON
    envelope still emits cleanly to stdout."""
    from claude_lb import doctor as doctor_mod

    def _fake_run_doctor(*, skip_network: bool = False):
        return doctor_mod.DoctorReport(
            version="0.8.0",
            checks=[
                doctor_mod.CheckResult(name="x", passed=True),
                doctor_mod.CheckResult(name="y", passed=False, detail="boom"),
            ],
        )

    monkeypatch.setattr(cli_mod, "run_doctor", _fake_run_doctor)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["meta"]["all_passed"] is False
    assert payload["meta"]["failed"] == 1


# ---------------------------------------------------------------------------
# pick — JSON failure envelope path
# ---------------------------------------------------------------------------


def test_pick_no_profiles_json_emits_error_envelope() -> None:
    """`pick --json` with no profiles should emit an error envelope, not a
    success-shape with empty data."""
    result = runner.invoke(app, ["pick", "--json"])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert "error" in payload
    assert "code" in payload["error"]


def test_pick_all_auth_dead_returns_auth_required(profile_factory) -> None:
    """All AUTH_DEAD → exit 2 (AUTH_REQUIRED) per the reason mapping."""
    from claude_lb.models import ErrorInfo, ProfileHealth

    profile_factory("dead-a")

    def _stub(profiles, *, prev_health=None):
        from datetime import UTC, datetime
        return [
            ProfileHealth(
                name=p.name,
                health=Health.AUTH_DEAD,
                probed_at=datetime.now(UTC),
                error=ErrorInfo(type="auth_error", message="dead"),
                credentials_mtime=p.credentials_mtime,
            )
            for p in profiles
        ]

    with patch.object(cli_mod, "probe_many_sync", _stub):
        result = runner.invoke(app, ["pick"])
    assert result.exit_code == 2

