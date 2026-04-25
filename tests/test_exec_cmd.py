"""Real-subprocess tests for `claude_lb.exec_cmd.run_child`.

These tests actually exec a child process (Python interpreter — guaranteed
available across platforms) and verify rc propagation, env-var injection,
and the timeout / not-found error paths. Distinct from `test_cli.py` which
stubs `run_child` to test the CLI wiring around it.
"""

from __future__ import annotations

import sys
import time

import pytest

from claude_lb.exec_cmd import RC_NOT_FOUND, RC_TIMEOUT, ExecResult, run_child


def test_run_child_propagates_rc_zero() -> None:
    result = run_child(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        env_var_name="AXIOM_CLAUDE_PROFILE",
        profile_name="account-a",
        timeout=10.0,
    )
    assert isinstance(result, ExecResult)
    assert result.rc == 0
    assert result.timed_out is False
    assert result.not_found is False
    assert result.duration_ms >= 0


def test_run_child_propagates_nonzero_rc() -> None:
    result = run_child(
        [sys.executable, "-c", "import sys; sys.exit(42)"],
        env_var_name="AXIOM_CLAUDE_PROFILE",
        profile_name="account-a",
        timeout=10.0,
    )
    assert result.rc == 42
    assert result.timed_out is False
    assert result.not_found is False


def test_run_child_injects_env_var() -> None:
    """The child should see env_var_name=profile_name in its environment.
    We verify by having Python read it and exit with rc=1 if absent / wrong."""
    code = (
        "import os, sys; "
        "sys.exit(0 if os.environ.get('MY_TEST_VAR') == 'profile-x' else 1)"
    )
    result = run_child(
        [sys.executable, "-c", code],
        env_var_name="MY_TEST_VAR",
        profile_name="profile-x",
        timeout=10.0,
    )
    assert result.rc == 0


def test_run_child_records_duration() -> None:
    """A child that sleeps ~100ms should report duration_ms >= 90 (slack for jitter)."""
    result = run_child(
        [sys.executable, "-c", "import time; time.sleep(0.1)"],
        env_var_name="AXIOM_CLAUDE_PROFILE",
        profile_name="account-a",
        timeout=10.0,
    )
    assert result.rc == 0
    assert result.duration_ms >= 90  # 100ms ± jitter


def test_run_child_timeout_returns_rc_124() -> None:
    """Child that runs past the timeout should be killed and report
    rc=124 (GNU `timeout` convention) with timed_out=True."""
    start = time.monotonic()
    result = run_child(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        env_var_name="AXIOM_CLAUDE_PROFILE",
        profile_name="account-a",
        timeout=0.3,
    )
    elapsed = time.monotonic() - start
    assert result.rc == RC_TIMEOUT
    assert result.timed_out is True
    assert result.not_found is False
    # Should have killed the child reasonably close to the timeout, not waited
    # the full 5s. Allow generous slack for slow CI.
    assert elapsed < 4.0
