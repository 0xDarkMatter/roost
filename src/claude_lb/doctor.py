"""`claude-lb doctor` — diagnose the local setup (Forma §9 introspection).

Runs a series of checks and reports pass/fail for each. Useful for answering
"why isn't pick returning what I expect?" without having to trace through
the classifier by hand.

Checks:
    - config dir writable
    - profiles discoverable (and at least one exists)
    - each profile's credentials parseable + has a token
    - cache file readable (or absent — still a pass)
    - outbound HTTPS to api.anthropic.com reachable
"""

from __future__ import annotations

import os
import socket
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any

from . import __version__
from .cache import load_cache
from .discovery import discover_profiles
from .paths import cache_path, config_dir


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


def _check_cache_readable() -> CheckResult:
    target = cache_path()
    if not target.is_file():
        return CheckResult(
            name="cache_readable",
            passed=True,
            detail="(no cache yet — first probe will create it)",
        )
    try:
        cache = load_cache()
    except Exception as exc:  # pragma: no cover - defensive
        return CheckResult(
            name="cache_readable",
            passed=False,
            detail=f"Cache exists but cannot be read: {exc}",
        )
    return CheckResult(
        name="cache_readable",
        passed=True,
        detail=f"{len(cache.profiles)} cached entries",
        extra={"profile_count": len(cache.profiles)},
    )


def _check_anthropic_reachable(timeout_s: float = 3.0) -> CheckResult:
    """DNS + TCP handshake to api.anthropic.com:443.

    Doesn't authenticate or probe — just confirms the endpoint is reachable.
    This is a best-effort check; network hiccups shouldn't fail doctor hard.
    """
    host = "api.anthropic.com"
    port = 443
    try:
        # getaddrinfo → resolves DNS without opening a socket
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
    family, socktype, proto, _, sockaddr = infos[0]
    s = socket.socket(family, socktype, proto)
    s.settimeout(timeout_s)
    try:
        s.connect(sockaddr)
    except OSError as exc:
        return CheckResult(
            name="anthropic_reachable",
            passed=False,
            detail=f"TCP connect to {host}:{port} failed: {exc}",
        )
    finally:
        s.close()
    return CheckResult(
        name="anthropic_reachable",
        passed=True,
        detail=f"{host}:{port} reachable",
    )


def run_doctor(*, skip_network: bool = False) -> DoctorReport:
    """Run all diagnostic checks and return a structured report."""
    checks: list[CheckResult] = [
        _check_config_dir_writable(),
        _check_profiles_discoverable(),
        _check_credentials_parseable(),
        _check_cache_readable(),
    ]
    if not skip_network:
        checks.append(_check_anthropic_reachable())
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
