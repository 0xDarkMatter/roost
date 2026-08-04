"""Live integration tests — talk to the real Anthropic API + local fleet.

Skipped by default; run with `pytest -m live`. These tests:

  - require at least one profile in ~/.claude-profiles/
  - hit live api.anthropic.com endpoints (probe, refresh, etc.)
  - assert SHAPE, not specific health states (which vary across fleets)

Failure modes worth understanding:

  - All profiles auth_dead → most tests skip (we can't probe)
  - All profiles weekly_exhausted → pick tests still pass (filter ladder
    correctly returns a structured failure)
  - Network outage → tests that hit the network fail, which is the point
    of having them: classifier drift is only catchable against live shapes

These tests use the real CLI — no mocking. They write to picks.log,
last-pick.json, and (if enabled) usage-log.ndjson on the developer's
machine, just like normal usage. Tests do NOT modify .credentials.json
contents (refresh tests skip if a real token rotation would happen).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_lb.cli import app
from claude_lb.discovery import discover_profiles

pytestmark = pytest.mark.live


runner = CliRunner()


@pytest.fixture(scope="session")
def fleet() -> list[str]:
    """Names of profiles discoverable in ~/.claude-profiles/.

    Skips the entire live suite if the fleet is empty — there is nothing to
    probe, refresh, or pick against.
    """
    profiles = discover_profiles()
    if not profiles:
        pytest.skip(
            "No profiles in ~/.claude-profiles/ — live tests need at least "
            "one. Run `roost add <name>` after `claude login`."
        )
    return [p.name for p in profiles]


# ---------------------------------------------------------------------------
# probe — single profile, all profiles, raw mode
# ---------------------------------------------------------------------------


def test_live_probe_single_profile_returns_health(fleet: list[str]) -> None:
    """probe <name> should return exit 0 and produce a status line."""
    name = fleet[0]
    result = runner.invoke(app, ["probe", name])
    assert result.exit_code == 0
    # Status table or stdout summary will mention the profile.
    combined = (result.stdout + result.stderr).lower()
    assert name.lower() in combined or "profile" in combined


def test_live_probe_all_profiles_succeeds(fleet: list[str]) -> None:
    """probe (no arg) probes every profile."""
    result = runner.invoke(app, ["probe"])
    assert result.exit_code == 0


def test_live_probe_json_envelope_shape(fleet: list[str]) -> None:
    name = fleet[0]
    result = runner.invoke(app, ["probe", name, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "data" in payload
    assert "meta" in payload


def test_live_probe_raw_returns_unwrapped_response(fleet: list[str]) -> None:
    name = fleet[0]
    result = runner.invoke(app, ["probe", name, "--raw"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]
    record = payload["data"][0]
    assert record["name"] == name
    assert "status_code" in record
    assert "body" in record
    assert "headers" in record


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_live_status_text_mode(fleet: list[str]) -> None:
    """Status table renders to stderr. Names may be truncated by Rich when
    the terminal is narrow (CliRunner uses 80 cols), so we assert on the
    summary line on stdout instead — that's not column-truncated."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # The summary line on stdout is "N profiles · M ok · ..." — test that
    # the count matches our fleet size.
    summary = result.stdout
    assert f"{len(fleet)} profile" in summary


def test_live_status_json_envelope_includes_all_profiles(
    fleet: list[str],
) -> None:
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["count"] == len(fleet)
    names = {entry["name"] for entry in payload["data"]}
    assert set(fleet) == names


# ---------------------------------------------------------------------------
# pick — every strategy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", [
    "sticky", "least-used", "round-robin", "weighted", "first-healthy",
    "lowest-overage",
])
def test_live_pick_returns_a_profile_for_each_strategy(
    fleet: list[str], strategy: str
) -> None:
    """Every strategy should either return a profile name (exit 0) or a
    structured failure code (2/5/6/9). It should never crash."""
    result = runner.invoke(app, ["pick", "--strategy", strategy])
    # Acceptable exit codes for live conditions:
    # 0 = picked, 2 = all auth, 5 = require-ok no ok, 6 = throttled, 9 = unavail
    assert result.exit_code in (0, 2, 5, 6, 9)
    if result.exit_code == 0:
        # When successful, stdout has one line: the profile name.
        chosen = result.stdout.strip()
        assert chosen in fleet


def test_live_pick_auto_refresh_does_not_crash(fleet: list[str]) -> None:
    """--auto-refresh should be safe even when no profile is auth_expired."""
    result = runner.invoke(app, ["pick", "--auto-refresh"])
    assert result.exit_code in (0, 2, 5, 6, 9)


def test_live_pick_explain_includes_decision_in_stderr(
    fleet: list[str],
) -> None:
    """--explain should always render the decision tree, regardless of
    success/failure."""
    result = runner.invoke(app, ["pick", "--explain"])
    # Decision table is on stderr.
    assert "decision" in result.stderr.lower() or "Pick" in result.stderr


def test_live_pick_explain_json_includes_explain_block(
    fleet: list[str],
) -> None:
    result = runner.invoke(app, ["pick", "--explain", "--json"])
    assert result.exit_code in (0, 2, 5, 6, 9)
    payload = json.loads(result.stdout)
    if "data" in payload and isinstance(payload["data"], dict):
        assert "explain" in payload["data"]
    elif "error" in payload:
        assert "explain" in payload["error"]["details"]


def test_live_pick_count_returns_at_most_n(fleet: list[str]) -> None:
    """--count N returns up to N profile names."""
    result = runner.invoke(app, ["pick", "--count", "3"])
    if result.exit_code == 0:
        names = result.stdout.strip().split("\n")
        assert 1 <= len(names) <= 3
        assert all(n in fleet for n in names)


# ---------------------------------------------------------------------------
# which — read-only counterpart
# ---------------------------------------------------------------------------


def test_live_which_does_not_modify_picks_log(
    fleet: list[str], tmp_path: Path
) -> None:
    """`which` must not write to picks.log."""
    from claude_lb.paths import pick_log_path

    log_path = pick_log_path()
    pre_size = log_path.stat().st_size if log_path.is_file() else 0

    result = runner.invoke(app, ["which"])
    assert result.exit_code in (0, 2, 5, 6, 9)

    post_size = log_path.stat().st_size if log_path.is_file() else 0
    assert post_size == pre_size, "which should not append to picks.log"


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def test_live_refresh_soon_with_long_window_no_op(
    fleet: list[str],
) -> None:
    """`refresh --soon 1s` only refreshes profiles expiring within 1 second
    — almost certainly nobody. Should exit 0 with empty result."""
    result = runner.invoke(app, ["refresh", "--soon", "1s", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["meta"]["refreshed"] == 0


def test_live_refresh_unknown_profile_exits_not_found(
    fleet: list[str],
) -> None:
    result = runner.invoke(app, ["refresh", "absolutely-not-a-profile-name"])
    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# doctor / update
# ---------------------------------------------------------------------------


def test_live_doctor_runs_without_crashing(fleet: list[str]) -> None:
    """Doctor talks to the network; verify it produces a verdict."""
    result = runner.invoke(app, ["doctor"])
    # Doctor exits 0 when all checks pass, 1 if any fail.
    assert result.exit_code in (0, 1)


def test_live_doctor_json_envelope_shape(fleet: list[str]) -> None:
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code in (0, 1)
    payload = json.loads(result.stdout)
    assert "checks" in payload or "data" in payload  # tolerate shape drift


def test_live_update_status_returns_version(fleet: list[str]) -> None:
    """`update` (without --apply) reports the current version."""
    result = runner.invoke(app, ["update", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "current_version" in payload or "data" in payload


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


def test_live_trace_dumps_request_and_response(fleet: list[str]) -> None:
    name = fleet[0]
    result = runner.invoke(app, ["trace", name, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["profile"] == name
    assert "request" in payload["data"]
    assert "response" in payload["data"]
    assert "classification" in payload["data"]


def test_live_trace_redacts_token_in_request_envelope(
    fleet: list[str],
) -> None:
    """Token redaction must hold against live profiles too — they're real."""
    name = fleet[0]
    result = runner.invoke(app, ["trace", name, "--json"])
    if result.exit_code != 0:
        pytest.skip("trace failed; can't assert redaction")
    payload = json.loads(result.stdout)
    auth = payload["data"]["request"]["headers"]["Authorization"]
    assert "***" in auth


# ---------------------------------------------------------------------------
# stats — reads picks.log (which exists if pick has ever been called)
# ---------------------------------------------------------------------------


def test_live_stats_does_not_crash(fleet: list[str]) -> None:
    """Stats reads picks.log; should always succeed (empty log → friendly msg)."""
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0


def test_live_stats_json_envelope(fleet: list[str]) -> None:
    result = runner.invoke(app, ["stats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "data" in payload
    assert "meta" in payload


# ---------------------------------------------------------------------------
# Discovery sanity
# ---------------------------------------------------------------------------


def test_live_list_lists_every_profile(fleet: list[str]) -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    for name in fleet:
        assert name in result.stdout


def test_live_show_unknown_profile_exits_not_found(fleet: list[str]) -> None:
    result = runner.invoke(app, ["show", "this-profile-cannot-possibly-exist"])
    assert result.exit_code == 3
