"""Run a child command against a picked profile (SPEC §2).

Collapses the spawn-worker wrapper from:

    profile=$(claude-lb pick --auto-refresh 2>/dev/null) || exit $?
    AXIOM_CLAUDE_PROFILE=$profile claude --args...

into a single call:

    claude-lb exec --auto-refresh -- claude --args...

claude-lb's own exit code equals the child's exit code, so scripts can
treat `claude-lb exec` as a transparent wrapper.

Named `exec_cmd.py` (not `exec.py`) because `exec` shadows the Python
builtin in some static-analysis setups.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


# POSIX-ish exit-code conventions for distinguishing our failures from
# the child's. The child's own codes pass through unchanged; these only
# apply when we couldn't run it.
RC_TIMEOUT = 124        # GNU `timeout` convention
RC_NOT_FOUND = 127      # shell "command not found" convention


@dataclass
class ExecResult:
    """Wall-clock outcome of one subprocess.run() call."""

    rc: int
    duration_ms: int
    timed_out: bool = False
    not_found: bool = False


def run_child(
    argv: list[str],
    *,
    env_var_name: str,
    profile_name: str,
    timeout: float | None,
) -> ExecResult:
    """Exec argv with env[env_var_name]=profile_name; return rc + duration.

    stdin/stdout/stderr are inherited so interactive children (like `claude`
    itself) work. Ctrl+C is forwarded to the child by subprocess.run on both
    POSIX and Windows.
    """
    env = dict(os.environ)
    env[env_var_name] = profile_name

    start = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 — argv is operator-supplied
            argv,
            env=env,
            timeout=timeout,
            check=False,
            # stdin/stdout/stderr default to the parent's — interactive safe.
        )
        rc = completed.returncode
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecResult(rc=rc, duration_ms=duration_ms)
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecResult(rc=RC_TIMEOUT, duration_ms=duration_ms, timed_out=True)
    except FileNotFoundError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        log.debug("exec: command not found: %s", exc)
        return ExecResult(rc=RC_NOT_FOUND, duration_ms=duration_ms, not_found=True)
