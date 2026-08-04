"""Run a child command against a picked profile (SPEC §2).

Collapses the spawn-worker wrapper from:

    profile=$(roost pick --auto-refresh 2>/dev/null) || exit $?
    ROOST_PROFILE=$profile claude --args...

into a single call:

    roost exec --auto-refresh -- claude --args...

roost's own exit code equals the child's exit code, so scripts can
treat `roost exec` as a transparent wrapper.

Named `exec_cmd.py` (not `exec.py`) because `exec` shadows the Python
builtin in some static-analysis setups.
"""

from __future__ import annotations

import contextlib
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
    lease_profile: bool = False,
    lease_duration_s: int = 30 * 60,
) -> ExecResult:
    """Exec argv with env[env_var_name]=profile_name; return rc + duration.

    stdin/stdout/stderr are inherited so interactive children (like `claude`
    itself) work. Ctrl+C is forwarded to the child by subprocess.run on both
    POSIX and Windows.

    lease_profile=True (the default from the CLI) pins the profile against
    `roost refresh` for lease_duration_s seconds — long enough to cover the
    child's expected lifetime. The lease is released in a finally block so
    Ctrl+C and exceptions both clean up correctly.
    """
    env = dict(os.environ)
    env[env_var_name] = profile_name

    with _maybe_lease(profile_name, lease_profile, lease_duration_s):
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


@contextlib.contextmanager
def _maybe_lease(profile: str, do_lease: bool, duration_s: int):
    """Context manager: acquire lease on entry, release on exit (if do_lease)."""
    if not do_lease:
        yield
        return

    from .lease import acquire as _acquire, release as _release

    lease = _acquire(profile, duration_s, creator="roost-exec")
    log.debug("exec: acquired lease %s on %s for %ds", lease.lease_id, profile, duration_s)
    try:
        yield
    finally:
        _release(lease.lease_id)
        log.debug("exec: released lease %s", lease.lease_id)
