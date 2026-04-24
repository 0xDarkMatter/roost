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
    from claude_lb import cache, doctor, pick

    monkeypatch.setattr(cache, "cache_path", lambda: config / "health.json")
    monkeypatch.setattr(cache, "ensure_config_dir", lambda: config)
    monkeypatch.setattr(pick, "last_pick_path", lambda: config / "last-pick.json")
    monkeypatch.setattr(pick, "pick_log_path", lambda: config / "picks.log")
    monkeypatch.setattr(doctor, "config_dir", lambda: config)
    monkeypatch.setattr(doctor, "cache_path", lambda: config / "health.json")

    yield config


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "claude-lb" in result.stdout
    assert "0.4.1" in result.stdout


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
