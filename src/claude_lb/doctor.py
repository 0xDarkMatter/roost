"""`roost doctor` — diagnose the local setup (Forma §9 introspection).

Runs a series of checks and reports pass/fail for each. Useful for answering
"why isn't pick returning what I expect?" without having to trace through
the classifier by hand.

Checks:
    - config dir writable
    - profiles discoverable (and at least one exists)
    - each profile's credentials parseable + has a token
    - cache file readable (or absent — still a pass)
    - outbound HTTPS to api.anthropic.com reachable
    - status.claude.com platform incidents (warn-only)
"""

from __future__ import annotations

import os
import socket
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from . import __version__
from .cache import load_cache
from .discovery import discover_profiles
from .paths import cache_path, config_dir
from .platform_status import STATUS_PAGE_URL, fetch_platform_status


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    version: str
    checks: list[CheckResult]

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)


def _check_subcommand_imports() -> CheckResult:
    """Verify every subcommand module loads without ImportError.

    Specifically catches the "stale editable install" failure mode: the
    pyproject declares a dep that landed in a newer version, but the tool
    venv hasn't been re-synced, so one subcommand crashes at load time
    while every other command keeps working. Doctor surfaces this as a
    fleet-wide problem instead of waiting for the user to trip over it.
    """
    checked = ["cache", "cli", "discovery", "exec_cmd", "models", "output",
               "paths", "pick", "platform_status", "probe", "refresh",
               "taxonomy", "updater"]
    failures: list[str] = []
    for name in checked:
        try:
            __import__(f"claude_lb.{name}")
        except Exception as exc:  # pragma: no cover - exercised via stale-install test
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    if failures:
        return CheckResult(
            name="subcommand_imports",
            passed=False,
            detail=(
                "Stale install? Some subcommand modules failed to import. "
                "Reinstall with: uv tool install --reinstall --editable <repo>. "
                + " | ".join(failures)
            ),
            extra={"failures": failures},
        )
    return CheckResult(
        name="subcommand_imports",
        passed=True,
        detail=f"{len(checked)} modules load cleanly",
        extra={"checked": checked},
    )


def _check_config_dir_writable() -> CheckResult:
    d = config_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            name="config_dir_writable",
            passed=False,
            detail=f"Cannot create config dir {d}: {exc}",
        )
    # Actual write test — directory exists isn't enough on Windows with
    # restrictive ACLs.
    try:
        fd, tmp = tempfile.mkstemp(prefix=".doctor-", dir=str(d))
        os.close(fd)
        os.unlink(tmp)
    except OSError as exc:
        return CheckResult(
            name="config_dir_writable",
            passed=False,
            detail=f"{d} exists but is not writable: {exc}",
        )
    return CheckResult(name="config_dir_writable", passed=True, detail=str(d))


def _check_profiles_discoverable() -> CheckResult:
    profiles = discover_profiles()
    if not profiles:
        return CheckResult(
            name="profiles_discoverable",
            passed=False,
            detail=(
                "No profiles found. Expected "
                "~/.claude-profiles/<name>/.credentials.json. "
                "Run `claude login --profile <name>` to create one."
            ),
        )
    return CheckResult(
        name="profiles_discoverable",
        passed=True,
        detail=f"{len(profiles)} profile(s): " + ", ".join(p.name for p in profiles),
        extra={"count": len(profiles), "names": [p.name for p in profiles]},
    )


def _check_credentials_parseable() -> CheckResult:
    profiles = discover_profiles()
    if not profiles:
        return CheckResult(
            name="credentials_parseable",
            passed=True,
            detail="(skipped — no profiles)",
        )
    # discover_profiles() already rejects profiles without extractable tokens,
    # so every returned profile has a token. Report token-source distribution
    # to help diagnose drift.
    sources: dict[str, int] = {}
    for p in profiles:
        sources[p.token_source] = sources.get(p.token_source, 0) + 1
    modern = sources.get("claudeAiOauth.accessToken", 0)
    legacy_total = sum(v for k, v in sources.items() if k != "claudeAiOauth.accessToken")
    detail = f"modern shape: {modern}, legacy shape: {legacy_total}"
    return CheckResult(
        name="credentials_parseable",
        passed=True,
        detail=detail,
        extra={"sources": sources},
    )


def _check_refresh_tokens() -> CheckResult:
    """Verify each profile has a stored refresh token.

    Profiles without one fall through `pick --auto-refresh` silently (correctly,
    since there's nothing to refresh) but only manifest at the moment a token
    expires and the operator finds out the profile needs `claude login`. Doctor
    surfaces this proactively. Warning, not failure: a deliberate API-key-only
    or legacy-shape profile is a valid setup, just won't auto-heal.
    """
    profiles = discover_profiles()
    if not profiles:
        return CheckResult(
            name="refresh_tokens_present",
            passed=True,
            detail="(skipped — no profiles)",
        )
    with_token = [p.name for p in profiles if p.refresh_token_present]
    without_token = [p.name for p in profiles if not p.refresh_token_present]
    if not without_token:
        return CheckResult(
            name="refresh_tokens_present",
            passed=True,
            detail=f"all {len(profiles)} profile(s) have refresh tokens",
            extra={"with_token": with_token, "without_token": without_token},
        )
    # Don't fail the doctor run — this is a warning, not a hard error.
    # Distinguish in `extra` so JSON consumers can react.
    return CheckResult(
        name="refresh_tokens_present",
        passed=True,  # warn-level; failure would block CI for legitimate setups
        detail=(
            f"{len(without_token)} of {len(profiles)} profile(s) have NO refresh "
            f"token: {', '.join(without_token)}. These can't be healed by "
            f"`refresh` or `pick --auto-refresh` — they need `claude login "
            f"--profile <name>` when their access token expires."
        ),
        extra={
            "with_token": with_token,
            "without_token": without_token,
            "warning": True,
        },
    )


def _check_cache_readable() -> CheckResult:
    target = cache_path()
    if not target.is_file():
        return CheckResult(
            name="cache_readable",
            passed=True,
            detail="(no cache yet — first probe will create it)",
        )
    # `load_cache` is intentionally resilient: it swallows JSON errors and
    # returns an empty cache so the rest of the tool keeps working on a
    # corrupt cache (the next probe will rewrite it). For doctor, we want
    # to surface the corruption explicitly — silently passing this check
    # while load_cache logs a warning to stderr is the kind of thing that
    # makes "doctor passes but nothing works" debugging frustrating.
    import json as _json
    try:
        with target.open("rb") as fh:
            _json.load(fh)
    except OSError as exc:
        return CheckResult(
            name="cache_readable",
            passed=False,
            detail=f"Cache exists but cannot be read: {exc}",
        )
    except _json.JSONDecodeError as exc:
        return CheckResult(
            name="cache_readable",
            passed=False,
            detail=(
                f"Cache JSON is malformed at line {exc.lineno} col {exc.colno}: "
                f"{exc.msg}. Delete {target} or run `roost invalidate "
                f"<name>` for any profile to reset."
            ),
        )
    cache = load_cache()  # safe — JSON parse already validated above
    return CheckResult(
        name="cache_readable",
        passed=True,
        detail=f"{len(cache.profiles)} cached entries",
        extra={"profile_count": len(cache.profiles)},
    )


def _check_anthropic_reachable(timeout_s: float = 3.0) -> CheckResult:
    """DNS + TCP handshake to api.anthropic.com:443.

    Doesn't authenticate or probe — just confirms the endpoint is reachable.
    Tries each getaddrinfo result in turn so IPv6-only or IPv4-only hosts
    both work.
    """
    host = "api.anthropic.com"
    port = 443
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return CheckResult(
            name="anthropic_reachable",
            passed=False,
            detail=f"DNS lookup failed for {host}: {exc}",
        )
    if not infos:
        return CheckResult(
            name="anthropic_reachable",
            passed=False,
            detail=f"No A/AAAA records for {host}",
        )

    last_exc: OSError | None = None
    for family, socktype, proto, _canon, sockaddr in infos:
        s = socket.socket(family, socktype, proto)
        s.settimeout(timeout_s)
        try:
            s.connect(sockaddr)
            return CheckResult(
                name="anthropic_reachable",
                passed=True,
                detail=f"{host}:{port} reachable",
            )
        except OSError as exc:
            last_exc = exc
            continue
        finally:
            s.close()

    return CheckResult(
        name="anthropic_reachable",
        passed=False,
        detail=f"TCP connect to {host}:{port} failed: {last_exc}",
    )


def _check_claude_status_page(timeout_s: float = 3.0) -> CheckResult:
    """Fetch status.claude.com summary; surface platform incidents as a warning.

    This is informational — roost can't *fix* an Anthropic-side incident,
    but operators consulting `doctor` because pick is misbehaving deserve to
    know whether the platform is degraded vs whether their setup is wrong.

    Always WARN-level (passed=True). An Anthropic-side incident or an
    unreachable Statuspage endpoint should never fail the doctor run — that
    would mean every operator's CI breaks during every Anthropic blip.

    The summary endpoint returns both an aggregate `status.indicator`
    (none/minor/major/critical) AND a list of `incidents` whose lifecycle is
    investigating -> identified -> monitoring -> resolved. A monitoring-state
    incident often doesn't update the global indicator (e.g. "Elevated errors
    on Claude Opus 4.7" while everything else is operational), so we report
    on both signals independently.
    """
    try:
        status = fetch_platform_status(timeout_s=timeout_s)
    except (httpx.HTTPError, ValueError) as exc:
        return CheckResult(
            name="claude_status_page",
            passed=True,  # warn-level — don't block on Statuspage being flaky
            detail=f"Couldn't reach {STATUS_PAGE_URL}: {type(exc).__name__}: {exc}",
            extra={"warning": True, "fetch_error": str(exc)},
        )

    extra: dict[str, Any] = {
        "indicator": status.indicator,
        "description": status.description,
        "active_incidents": status.active_incidents,
        "degraded_components": status.degraded_components,
    }

    if status.is_clean:
        return CheckResult(
            name="claude_status_page",
            passed=True,
            detail=f"{status.description}, no active incidents",
            extra=extra,
        )

    parts: list[str] = [status.description]
    if status.active_incidents:  # pragma: no branch  -- degraded-only branch covered via format_status_line tests
        first = status.active_incidents[0]
        impact = first.get("impact") or "unknown"
        i_status = first.get("status") or "unknown"
        suffix = (
            f" (+{len(status.active_incidents) - 1} more)"
            if len(status.active_incidents) > 1
            else ""
        )
        parts.append(
            f"{len(status.active_incidents)} active incident(s): "
            f"'{first.get('name')}' [{i_status}, {impact}]{suffix}"
        )
    if status.degraded_components:
        names = ", ".join(str(c.get("name")) for c in status.degraded_components[:3])
        more = (
            ""
            if len(status.degraded_components) <= 3
            else f" (+{len(status.degraded_components) - 3} more)"
        )
        parts.append(f"degraded components: {names}{more}")

    extra["warning"] = True
    return CheckResult(
        name="claude_status_page",
        passed=True,  # WARN, not failure — informational
        detail="; ".join(parts),
        extra=extra,
    )


def run_doctor(*, skip_network: bool = False) -> DoctorReport:
    """Run all diagnostic checks and return a structured report."""
    checks: list[CheckResult] = [
        _check_subcommand_imports(),
        _check_config_dir_writable(),
        _check_profiles_discoverable(),
        _check_credentials_parseable(),
        _check_refresh_tokens(),
        _check_cache_readable(),
    ]
    if not skip_network:
        checks.append(_check_anthropic_reachable())
        checks.append(_check_claude_status_page())
    return DoctorReport(version=__version__, checks=checks)


def report_to_dict(report: DoctorReport) -> dict[str, Any]:
    """Serialise a DoctorReport for --json output."""
    return {
        "data": {
            "version": report.version,
            "checks": [asdict(c) for c in report.checks],
        },
        "meta": {
            "passed": sum(1 for c in report.checks if c.passed),
            "failed": sum(1 for c in report.checks if not c.passed),
            "total": len(report.checks),
            "all_passed": report.all_passed,
        },
    }
