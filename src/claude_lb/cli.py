"""Typer CLI entry point (SPEC §2, §4)."""

from __future__ import annotations

import logging
import sys
from datetime import UTC
from typing import Annotated, Any

import typer

from . import __version__
from .cache import is_entry_fresh, load_cache, remove_profile, save_cache
from .discovery import discover_profiles, get_profile
from .doctor import report_to_dict, run_doctor
from .models import Health, HealthCache, ProfileHealth
from .output import (
    build_status_payload,
    emit_error_json,
    emit_json,
    emit_text,
    render_status_table,
    stderr,
)
from .pick import (
    PickFailureReason,
    Strategy,
    append_pick_log,
    pick,
    write_last_pick,
)
from .exec_cmd import RC_NOT_FOUND, RC_TIMEOUT, ExecResult, run_child
from .platform_status import (
    format_status_line as _format_platform_status_line,
    load_or_fetch as _load_platform_status,
    to_json_meta as _platform_status_to_meta,
)
from .probe import probe_many_sync
from .refresh import refresh_many_sync
from .updater import apply_result_to_dict, apply_update, check_for_update, status_to_dict

app = typer.Typer(
    name="claude-lb",
    help="Pick the healthiest Claude Code Max profile — health taxonomy + load balancer.",
    no_args_is_help=True,
    # Typer adds `--install-completion` / `--show-completion` here for free.
    # Bash/Zsh/Fish/PowerShell all supported. Per-argument profile-name
    # completion is wired below via the `_complete_profile_names` callback.
    add_completion=True,
)


def _complete_profile_names(incomplete: str) -> list[str]:
    """Tab-complete profile names for `show`, `probe`, `refresh`, `invalidate`.

    Called by typer at completion time. Best-effort: a slow/broken discovery
    must not break the user's shell — swallow exceptions and return [].
    """
    try:
        from .discovery import discover_profiles
        return [p.name for p in discover_profiles() if p.name.startswith(incomplete)]
    except Exception:
        return []


def _complete_strategy(incomplete: str) -> list[str]:
    """Tab-complete --strategy values."""
    from .pick import Strategy
    return [s.value for s in Strategy if s.value.startswith(incomplete)]


_DURATION_UNITS: dict[str, int] = {
    "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
}


def _parse_duration(value: str) -> int | None:
    """Parse '30m' / '1h' / '2d' / '1w' / plain seconds → seconds, else None.

    Used by `--soon` (refresh window) and `--since` (history filter). Keep the
    grammar minimal: one integer + one unit suffix (or bare seconds).
    """
    s = value.strip().lower()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if len(s) < 2:
        return None
    try:
        n = int(s[:-1])
    except ValueError:
        return None
    unit = s[-1]
    mult = _DURATION_UNITS.get(unit)
    if mult is None or n < 0:
        return None
    return n * mult


def _humanize_elapsed(seconds: float) -> str:
    """'37s ago' / '4m ago' / '2h ago' / '3d ago'. Matches status table style."""
    s = int(seconds)
    if s < 0:
        return "0s ago"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"

profiles_app = typer.Typer(help="Profile operations")
app.add_typer(profiles_app, name="profiles")

# ---------------------------------------------------------------------------
# Exit codes (SPEC §4)
# ---------------------------------------------------------------------------

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_AUTH_REQUIRED = 2
EXIT_NOT_FOUND = 3
EXIT_VALIDATION = 4
EXIT_FORBIDDEN = 5
EXIT_RATE_LIMITED = 6
EXIT_CONFLICT = 7
EXIT_TIMEOUT = 8
EXIT_UNAVAILABLE = 9

REFRESH_ERROR_TO_EXIT: dict[str, int] = {
    "UNREADABLE": EXIT_NOT_FOUND,
    "NO_REFRESH_TOKEN": EXIT_AUTH_REQUIRED,
    "NETWORK_ERROR": EXIT_UNAVAILABLE,
    "REFRESH_REJECTED": EXIT_AUTH_REQUIRED,
    "UNEXPECTED_RESPONSE": EXIT_ERROR,
    "WRITE_FAILED": EXIT_ERROR,
    "LOCK_HELD": EXIT_CONFLICT,
    # Stale editable install: pyproject declares the dep but tool venv
    # never got re-synced. Reusing EXIT_ERROR so scripts just see
    # "something went wrong"; the error_message/error_code tells the user
    # exactly how to fix it.
    "MISSING_DEPENDENCY": EXIT_ERROR,
}

REASON_TO_EXIT: dict[PickFailureReason, int] = {
    PickFailureReason.NO_PROFILES: EXIT_UNAVAILABLE,
    PickFailureReason.ALL_AUTH_DEAD: EXIT_AUTH_REQUIRED,
    PickFailureReason.ALL_AUTH_EXPIRED: EXIT_AUTH_REQUIRED,
    PickFailureReason.ALL_WEEKLY: EXIT_UNAVAILABLE,
    PickFailureReason.ALL_THROTTLED: EXIT_RATE_LIMITED,
    PickFailureReason.ALL_TERMINAL: EXIT_UNAVAILABLE,
    PickFailureReason.REQUIRE_OK_NONE: EXIT_FORBIDDEN,
}

REASON_MESSAGES: dict[PickFailureReason, str] = {
    PickFailureReason.NO_PROFILES: (
        "No profiles discovered. Expected ~/.claude-profiles/<name>/.credentials.json."
    ),
    PickFailureReason.ALL_AUTH_DEAD: (
        "No authenticated profiles. Run: claude login --profile <name>"
    ),
    PickFailureReason.ALL_AUTH_EXPIRED: (
        "All access tokens expired. Run: claude-lb refresh --expired"
    ),
    PickFailureReason.ALL_WEEKLY: "All profiles weekly-exhausted.",
    PickFailureReason.ALL_THROTTLED: "All profiles throttled.",
    PickFailureReason.ALL_TERMINAL: "No profiles available. Run: claude-lb status",
    PickFailureReason.REQUIRE_OK_NONE: "No profiles currently ok. Run: claude-lb probe",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _validate_strategy(value: str) -> Strategy:
    try:
        return Strategy(value)
    except ValueError:
        allowed = [s.value for s in Strategy]
        stderr.print(f"[red]Unknown strategy:[/red] {value}")
        stderr.print(f"Allowed: {', '.join(allowed)}")
        raise typer.Exit(EXIT_VALIDATION) from None


def _load_or_probe(
    *, refresh: bool, max_age: int | None, profile_filter: str | None = None
) -> tuple[HealthCache, list[str]]:
    """Return (possibly-refreshed cache, discovered profile names).

    If `refresh` is True, all profiles are probed.
    If `max_age` is set, it overrides the per-state TTLs (stale entries are re-probed).
    If `profile_filter` is set, only that profile is (re-)probed.
    """
    profiles = discover_profiles()
    names = [p.name for p in profiles]
    if not profiles:
        return load_cache(), names

    cache = load_cache()
    now_profiles = {p.name: p for p in profiles}

    to_probe: list[str] = []
    for p in profiles:
        if profile_filter is not None and p.name != profile_filter:
            continue
        if refresh:
            to_probe.append(p.name)
            continue
        entry = cache.profiles.get(p.name)
        if entry is None:
            to_probe.append(p.name)
            continue
        fresh = is_entry_fresh(entry, credentials_mtime=p.credentials_mtime)
        if max_age is not None and entry.probed_at:
            from datetime import datetime

            age = (datetime.now(UTC) - entry.probed_at.replace(
                tzinfo=entry.probed_at.tzinfo or UTC
            )).total_seconds()
            if age > max_age:
                fresh = False
        if not fresh:
            to_probe.append(p.name)

    if to_probe:
        targets = [now_profiles[n] for n in to_probe]
        prev = {n: cache.profiles[n].health for n in to_probe if n in cache.profiles}
        results = probe_many_sync(targets, prev_health=prev)
        for h in results:
            cache.profiles[h.name] = h
        save_cache(cache)

    return cache, names


def _attempt_auto_refresh(cache: HealthCache, names: list[str]) -> HealthCache:
    """Heal AUTH_EXPIRED profiles before picking.

    For each discovered profile whose cached health is AUTH_EXPIRED and which
    has a refresh token on disk, call refresh_many_sync(); on success, re-probe
    and update the cache. Refresh failures are reported on stderr but do not
    raise — the filter ladder in pick() will skip them naturally.

    Rationale: `pick --auto-refresh` should behave like "run refresh --expired,
    then pick" without the extra hop. Profiles without refresh tokens are never
    attempted (they require `claude login`, not `refresh`).
    """
    expired_targets = []
    for name in names:
        entry = cache.profiles.get(name)
        if entry is None or entry.health is not Health.AUTH_EXPIRED:
            continue
        profile = get_profile(name)
        if profile is None or not profile.refresh_token_present:
            continue
        expired_targets.append(profile)

    if not expired_targets:
        return cache

    results = refresh_many_sync(expired_targets)
    refreshed_profiles = []
    for profile, result in zip(expired_targets, results):
        if result.refreshed:
            refreshed_profiles.append(profile)
        else:
            stderr.print(
                f"[yellow]warn:[/yellow] auto-refresh failed for {result.name}: "
                f"{result.error_code or 'ERROR'} — {result.error_message or ''}"
            )

    if not refreshed_profiles:
        return cache

    # CRITICAL: re-discover the refreshed profiles. The Profile objects in
    # `refreshed_profiles` were built BEFORE the refresh, so their
    # `access_token_expires_at` still points at the past timestamp. If we
    # probe with those stale objects, `_local_auth_expired` (probe.py)
    # short-circuits to AUTH_EXPIRED without a network call — ignoring the
    # newly-refreshed expiresAt on disk. Net effect: refresh succeeds, cache
    # stays AUTH_EXPIRED, pick fails as if refresh hadn't happened.
    # Re-discovery re-reads .credentials.json and picks up the new expiresAt.
    fresh_profiles = []
    for stale in refreshed_profiles:
        fresh = get_profile(stale.name)
        if fresh is not None:
            fresh_profiles.append(fresh)

    if not fresh_profiles:
        return cache

    probed = probe_many_sync(fresh_profiles)
    for h in probed:
        cache.profiles[h.name] = h
    save_cache(cache)
    return cache


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        emit_text(f"claude-lb {__version__}")
        raise typer.Exit(EXIT_SUCCESS)


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Print version and exit.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable debug logging to stderr."),
    ] = False,
) -> None:
    """claude-lb — pick the healthiest Claude Code Max profile."""
    _setup_logging(verbose)


# ---------------------------------------------------------------------------
# profiles list (alias: list)
# ---------------------------------------------------------------------------


@profiles_app.command("list")
def profiles_list(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List discovered profiles (no probe)."""
    profiles = discover_profiles()
    names = [p.name for p in profiles]
    if json_output:
        emit_json({
            "data": [
                {
                    "name": p.name,
                    "credentials_path": p.credentials_path,
                    "token_source": p.token_source,
                    "subscription_type": p.subscription_type,
                }
                for p in profiles
            ],
            "meta": {"count": len(profiles)},
        })
        return
    if not profiles:
        stderr.print("[yellow]No profiles discovered.[/yellow]")
        return
    for n in names:
        emit_text(n)


@app.command("list")
def top_list(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Alias for `profiles list`."""
    profiles_list(json_output=json_output)


# ---------------------------------------------------------------------------
# profiles probe (alias: probe)
# ---------------------------------------------------------------------------


@profiles_app.command("probe")
def profiles_probe(
    name: Annotated[
        str | None,
        typer.Argument(
            help="Probe only this profile (default: all).",
            autocompletion=_complete_profile_names,
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    raw: Annotated[
        bool,
        typer.Option(
            "--raw",
            help=(
                "Emit the untouched /api/oauth/usage response body for each "
                "probed profile to stdout. Diagnostic only — does not update cache."
            ),
        ),
    ] = False,
) -> None:
    """Live-probe profile(s) and update cache."""
    profiles = discover_profiles()
    if not profiles:
        if json_output:
            emit_error_json("NOT_FOUND", "No profiles discovered.")
        stderr.print("[yellow]No profiles discovered.[/yellow]")
        raise typer.Exit(EXIT_UNAVAILABLE)

    if name is not None:
        match = next((p for p in profiles if p.name == name), None)
        if match is None:
            if json_output:
                emit_error_json("NOT_FOUND", f"No such profile: {name}")
            stderr.print(f"[red]No such profile:[/red] {name}")
            raise typer.Exit(EXIT_NOT_FOUND)
        targets = [match]
    else:
        targets = profiles

    if raw:
        # Diagnostic mode: dump raw response bodies, skip classification + cache write.
        from .probe import probe_raw_many_sync

        raw_results = probe_raw_many_sync(targets)
        payload = {
            "data": [
                {
                    "name": name,
                    "status_code": status,
                    "body": body,
                    "headers": headers,
                }
                for name, status, body, headers in raw_results
            ],
            "meta": {"count": len(raw_results)},
        }
        emit_json(payload)
        return

    cache = load_cache()
    prev = {p.name: cache.profiles[p.name].health for p in targets if p.name in cache.profiles}
    results = probe_many_sync(targets, prev_health=prev)
    for h in results:
        cache.profiles[h.name] = h
    save_cache(cache)

    if json_output:
        emit_json(build_status_payload(cache, [p.name for p in profiles]))
        return
    render_status_table([cache.profiles[p.name] for p in profiles if p.name in cache.profiles])
    emit_text(_summary_line(cache, [p.name for p in profiles]))


@app.command("probe")
def top_probe(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=_complete_profile_names),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    raw: Annotated[bool, typer.Option("--raw")] = False,
) -> None:
    """Alias for `profiles probe`."""
    profiles_probe(name=name, json_output=json_output, raw=raw)


# ---------------------------------------------------------------------------
# profiles status (alias: status)
# ---------------------------------------------------------------------------


@profiles_app.command("status")
def profiles_status(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ignore cache; probe everything.")
    ] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Alias for --no-cache.")
    ] = False,
    max_age: Annotated[
        int | None, typer.Option("--max-age", help="Override TTL, seconds.")
    ] = None,
    no_platform_status: Annotated[
        bool,
        typer.Option(
            "--no-platform-status",
            help="Skip the status.claude.com check (no extra fetch on cache miss).",
        ),
    ] = False,
) -> None:
    """Show cached health per profile (probes if stale).

    Also fetches https://status.claude.com (60s cache) and surfaces any
    Anthropic-side incident as a header above the table — useful when a
    misbehaving profile is actually a platform-wide issue. Disable with
    `--no-platform-status`.
    """
    cache, names = _load_or_probe(refresh=(no_cache or refresh), max_age=max_age)

    # Platform status is best-effort enrichment — never fail `status` over it.
    platform = None if no_platform_status else _load_platform_status(
        force_refresh=(no_cache or refresh),
    )

    if json_output:
        meta = _platform_status_to_meta(platform) if platform is not None else None
        emit_json(build_status_payload(cache, names, platform_status_meta=meta))
        return
    if platform is not None:
        line = _format_platform_status_line(platform)
        if line:
            stderr.print(line)
    entries = [
        cache.profiles[n]
        for n in names
        if n in cache.profiles
    ]
    render_status_table(entries)
    emit_text(_summary_line(cache, names))


@app.command("status")
def top_status(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age")] = None,
    no_platform_status: Annotated[bool, typer.Option("--no-platform-status")] = False,
) -> None:
    """Alias for `profiles status`."""
    profiles_status(
        json_output=json_output,
        no_cache=no_cache,
        refresh=refresh,
        max_age=max_age,
        no_platform_status=no_platform_status,
    )


# ---------------------------------------------------------------------------
# profiles show (alias: show)
# ---------------------------------------------------------------------------


@profiles_app.command("show")
def profiles_show(
    name: Annotated[
        str,
        typer.Argument(help="Profile name.", autocompletion=_complete_profile_names),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one profile's full health detail."""
    profile = get_profile(name)
    if profile is None:
        if json_output:
            emit_error_json("NOT_FOUND", f"No such profile: {name}")
        stderr.print(f"[red]No such profile:[/red] {name}")
        raise typer.Exit(EXIT_NOT_FOUND)
    cache = load_cache()
    entry = cache.profiles.get(name)
    if json_output:
        # Emit a consistent shape regardless of whether the profile has been
        # probed yet — callers should not have to null-guard half the fields.
        from datetime import datetime as _dt

        def _iso_or_none(dt: _dt | None) -> str | None:
            if dt is None:
                return None
            return dt.isoformat().replace("+00:00", "Z")

        data: dict[str, Any] = {
            "name": name,
            "credentials_path": profile.credentials_path,
            "token_source": profile.token_source,
            "subscription_type": profile.subscription_type,
            "health": entry.health.value if entry is not None else "unknown",
            "probed_at": _iso_or_none(entry.probed_at) if entry is not None else None,
            "expires_at": _iso_or_none(entry.expires_at) if entry is not None else None,
            "retry_after_s": entry.retry_after_s if entry is not None else None,
            "session_reset_at": (
                _iso_or_none(entry.session_reset_at) if entry is not None else None
            ),
            "weekly_reset_at": (
                _iso_or_none(entry.weekly_reset_at) if entry is not None else None
            ),
            "usage": (
                entry.usage.model_dump() if entry is not None and entry.usage else None
            ),
            "error": (
                entry.error.model_dump() if entry is not None and entry.error else None
            ),
            "probe_latency_ms": entry.probe_latency_ms if entry is not None else None,
        }
        emit_json({"data": data})
        return
    stderr.print(f"[bold]{name}[/bold]")
    stderr.print(f"  credentials: {profile.credentials_path}")
    stderr.print(f"  token_source: {profile.token_source}")
    if profile.subscription_type:
        stderr.print(f"  plan: {profile.subscription_type}")
    if entry is not None:
        stderr.print(f"  health: {entry.health.value}")
        if entry.probe_latency_ms is not None:
            stderr.print(f"  probe_latency_ms: {entry.probe_latency_ms}")
        if entry.error:
            stderr.print(f"  error: {entry.error.type} — {entry.error.message}")
    else:
        stderr.print("  health: (not probed)")


@app.command("show")
def top_show(
    name: Annotated[
        str, typer.Argument(autocompletion=_complete_profile_names)
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Alias for `profiles show`."""
    profiles_show(name=name, json_output=json_output)


# ---------------------------------------------------------------------------
# profiles pick (alias: pick)
# ---------------------------------------------------------------------------


@profiles_app.command("pick")
def profiles_pick(
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            help="sticky | least-used | round-robin | weighted | first-healthy",
            autocompletion=_complete_strategy,
        ),
    ] = "sticky",
    stickiness: Annotated[
        int | None,
        typer.Option("--stickiness", help="Stickiness window in seconds (default 300)."),
    ] = None,
    require_ok: Annotated[
        bool,
        typer.Option("--require-ok", help="Fail unless at least one profile is ok."),
    ] = False,
    export: Annotated[
        bool,
        typer.Option("--export", help="Emit VAR=value for eval $(...)."),
    ] = False,
    var_name: Annotated[
        str,
        typer.Option("--var-name", help="Var name for --export (default AXIOM_CLAUDE_PROFILE)."),
    ] = "AXIOM_CLAUDE_PROFILE",
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age")] = None,
    warn_at: Annotated[
        int | None,
        typer.Option(
            "--warn-at",
            help=(
                "If the chosen profile's session OR weekly utilisation is >= N%%, "
                "print a warning to stderr. Exit code is still 0."
            ),
        ),
    ] = None,
    auto_refresh: Annotated[
        bool,
        typer.Option(
            "--auto-refresh",
            help=(
                "Before picking, inline-refresh any profile whose cached health "
                "is auth_expired and which has a stored refresh token. Failed "
                "refreshes fall through — the filter ladder excludes them."
            ),
        ),
    ] = False,
    count: Annotated[
        int,
        typer.Option(
            "--count",
            "-n",
            help=(
                "Return up to N profiles (newline-separated) instead of one. "
                "Stickiness is ignored when N > 1. If fewer than N candidates "
                "pass the filter ladder, returns what's available. Incompatible "
                "with --export (ambiguous: can't export N vars with one name)."
            ),
            min=1,
        ),
    ] = 1,
) -> None:
    """Pick the best healthy profile for scripting."""
    chosen_strategy = _validate_strategy(strategy)
    if warn_at is not None and not (0 <= warn_at <= 100):
        stderr.print(f"[red]--warn-at must be between 0 and 100, got:[/red] {warn_at}")
        raise typer.Exit(EXIT_VALIDATION)
    if count > 1 and export:
        stderr.print(
            "[red]--export is incompatible with --count > 1[/red] "
            "(can't export N vars with one name). Use --json instead."
        )
        raise typer.Exit(EXIT_VALIDATION)
    cache, names = _load_or_probe(refresh=no_cache, max_age=max_age)

    if auto_refresh:
        cache = _attempt_auto_refresh(cache, names)

    outcome = pick(
        cache,
        names,
        strategy=chosen_strategy,
        stickiness_s=stickiness,
        require_ok=require_ok,
        count=count,
    )

    if not outcome.ok:
        reason = outcome.reason or PickFailureReason.NO_PROFILES
        exit_code = REASON_TO_EXIT.get(reason, EXIT_ERROR)
        message = REASON_MESSAGES.get(reason, "Pick failed.")
        if outcome.earliest_recovery_at:
            message = (
                f"{message} Earliest recovery: "
                f"{outcome.earliest_recovery_at.isoformat().replace('+00:00', 'Z')}"
            )
        if json_output:
            emit_error_json(
                reason.value.upper(),
                message,
                {"earliest_recovery_at": (
                    outcome.earliest_recovery_at.isoformat().replace("+00:00", "Z")
                    if outcome.earliest_recovery_at else None
                )},
            )
        stderr.print(f"[red]{message}[/red]")
        raise typer.Exit(exit_code)

    assert outcome.chosen is not None
    chosen = outcome.chosen
    picks = outcome.chosen_many or [chosen]
    strategy_used = outcome.strategy_used or chosen_strategy

    # last-pick: only the primary (first) pick updates stickiness state, so
    # a subsequent single-pick call without --count still sees the primary.
    write_last_pick(chosen.name)
    # picks.log: every picked profile gets an audit-trail entry so parallel
    # dispatch is observable.
    for entry in picks:
        score = 1.0 if entry.health is Health.OK else 0.5
        append_pick_log(entry.name, strategy_used, score)

    # --warn-at: non-fatal stderr hint if utilisation exceeds threshold.
    # For multi-pick, warn on the primary pick (consistent with single-pick
    # semantics; callers driving parallel dispatch can eyeball all N via --json).
    if warn_at is not None and chosen.usage is not None:
        hot_pcts: list[tuple[str, int]] = []
        if chosen.usage.session_pct is not None and chosen.usage.session_pct >= warn_at:
            hot_pcts.append(("session", chosen.usage.session_pct))
        if chosen.usage.weekly_pct is not None and chosen.usage.weekly_pct >= warn_at:
            hot_pcts.append(("weekly", chosen.usage.weekly_pct))
        if hot_pcts:
            parts = ", ".join(f"{label} {pct}%" for label, pct in hot_pcts)
            stderr.print(
                f"[yellow]warn:[/yellow] {chosen.name} {parts} (>= {warn_at}% threshold)"
            )

    if json_output:
        # Shape: single-object {"data": {...}} for count == 1 (backward compat),
        # array {"data": [...], "meta": {...}} for count > 1.
        if count == 1:
            primary_score = 1.0 if chosen.health is Health.OK else 0.5
            emit_json({
                "data": {
                    "name": chosen.name,
                    "health": chosen.health.value,
                    "score": primary_score,
                    "rationale": outcome.rationale,
                    "strategy": strategy_used.value,
                }
            })
        else:
            emit_json({
                "data": [
                    {
                        "name": e.name,
                        "health": e.health.value,
                        "score": 1.0 if e.health is Health.OK else 0.5,
                    }
                    for e in picks
                ],
                "meta": {
                    "count": len(picks),
                    "requested": count,
                    "strategy": strategy_used.value,
                    "rationale": outcome.rationale,
                },
            })
        return
    if export:
        emit_text(f"{var_name}={chosen.name}")
        return
    for entry in picks:
        emit_text(entry.name)


@app.command("pick")
def top_pick(
    strategy: Annotated[
        str,
        typer.Option("--strategy", autocompletion=_complete_strategy),
    ] = "sticky",
    stickiness: Annotated[int | None, typer.Option("--stickiness")] = None,
    require_ok: Annotated[bool, typer.Option("--require-ok")] = False,
    export: Annotated[bool, typer.Option("--export")] = False,
    var_name: Annotated[str, typer.Option("--var-name")] = "AXIOM_CLAUDE_PROFILE",
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age")] = None,
    warn_at: Annotated[int | None, typer.Option("--warn-at")] = None,
    auto_refresh: Annotated[bool, typer.Option("--auto-refresh")] = False,
    count: Annotated[int, typer.Option("--count", "-n", min=1)] = 1,
) -> None:
    """Alias for `profiles pick`."""
    profiles_pick(
        strategy=strategy,
        stickiness=stickiness,
        require_ok=require_ok,
        export=export,
        var_name=var_name,
        json_output=json_output,
        no_cache=no_cache,
        max_age=max_age,
        warn_at=warn_at,
        auto_refresh=auto_refresh,
        count=count,
    )


# ---------------------------------------------------------------------------
# profiles invalidate (alias: invalidate)
# ---------------------------------------------------------------------------


@profiles_app.command("invalidate")
def profiles_invalidate(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile to invalidate.", autocompletion=_complete_profile_names
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Drop a profile's cache entry so the next status/pick re-probes."""
    if get_profile(name) is None:
        if json_output:
            emit_error_json("NOT_FOUND", f"No such profile: {name}")
        stderr.print(f"[red]No such profile:[/red] {name}")
        raise typer.Exit(EXIT_NOT_FOUND)
    removed = remove_profile(name)
    if json_output:
        emit_json({"data": {"name": name, "invalidated": removed}, "meta": {"action": "invalidated"}})
        return
    if removed:
        stderr.print(f"[green]Invalidated:[/green] {name}")
    else:
        stderr.print(f"[yellow]Already absent from cache:[/yellow] {name}")


@app.command("invalidate")
def top_invalidate(
    name: Annotated[
        str, typer.Argument(autocompletion=_complete_profile_names)
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Alias for `profiles invalidate`."""
    profiles_invalidate(name=name, json_output=json_output)


# ---------------------------------------------------------------------------
# profiles refresh (alias: refresh)
# ---------------------------------------------------------------------------


def _run_refresh(
    names: list[str],
    *,
    all_profiles: bool,
    expired_only: bool,
    soon_seconds: int | None,
    timeout: float,
    json_output: bool,
) -> None:
    from datetime import UTC, datetime, timedelta

    discovered = discover_profiles()
    if not discovered:
        if json_output:
            emit_error_json("NOT_FOUND", "No profiles discovered.")
        stderr.print("[yellow]No profiles discovered.[/yellow]")
        raise typer.Exit(EXIT_UNAVAILABLE)

    by_name = {p.name: p for p in discovered}

    selectors_used = sum(
        1 for v in (bool(names), all_profiles, expired_only, soon_seconds is not None) if v
    )
    if selectors_used > 1:
        if json_output:
            emit_error_json(
                "VALIDATION_ERROR",
                "Pass exactly one of: <name>..., --all, --expired, --soon.",
            )
        stderr.print(
            "[red]Pass exactly one of: <name>..., --all, --expired, --soon.[/red]"
        )
        raise typer.Exit(EXIT_VALIDATION)
    if selectors_used == 0:
        if json_output:
            emit_error_json(
                "VALIDATION_ERROR",
                "Specify one or more profile names, --all, --expired, or --soon.",
            )
        stderr.print(
            "[red]Specify one or more profile names, --all, --expired, or --soon.[/red]"
        )
        raise typer.Exit(EXIT_VALIDATION)

    if names:
        missing = [n for n in names if n not in by_name]
        if missing:
            if json_output:
                emit_error_json(
                    "NOT_FOUND",
                    f"No such profile(s): {', '.join(missing)}",
                )
            stderr.print(f"[red]No such profile(s):[/red] {', '.join(missing)}")
            raise typer.Exit(EXIT_NOT_FOUND)
        targets = [by_name[n] for n in names]
    elif expired_only:
        now = datetime.now(UTC)
        targets = [
            p
            for p in discovered
            if p.access_token_expires_at is not None and p.access_token_expires_at <= now
        ]
    elif soon_seconds is not None:
        # `--soon N` covers everything `--expired` does plus tokens that will
        # expire within the window. Anticipatory refresh: cron-friendly way to
        # keep the fleet warm without waiting for a token to actually expire
        # mid-spawn.
        cutoff = datetime.now(UTC) + timedelta(seconds=soon_seconds)
        targets = [
            p
            for p in discovered
            if p.access_token_expires_at is not None and p.access_token_expires_at <= cutoff
        ]
    else:
        targets = list(discovered)

    if not targets:
        if json_output:
            emit_json({"data": [], "meta": {"count": 0, "refreshed": 0, "failed": 0}})
            return
        stderr.print("[green]Nothing to refresh.[/green]")
        return

    results = refresh_many_sync(targets, timeout=timeout)
    refreshed_count = sum(1 for r in results if r.refreshed)
    failed_count = len(results) - refreshed_count

    # Invalidate cache entries for successfully refreshed profiles so the
    # next status/pick call probes with the new accessToken.
    for r in results:
        if r.refreshed:
            remove_profile(r.name)

    if json_output:
        emit_json(
            {
                "data": [
                    {
                        "name": r.name,
                        "refreshed": r.refreshed,
                        "previous_expires_at": (
                            r.previous_expires_at.isoformat().replace("+00:00", "Z")
                            if r.previous_expires_at
                            else None
                        ),
                        "new_expires_at": (
                            r.new_expires_at.isoformat().replace("+00:00", "Z")
                            if r.new_expires_at
                            else None
                        ),
                        "error": (
                            None
                            if r.refreshed
                            else {"code": r.error_code, "message": r.error_message}
                        ),
                    }
                    for r in results
                ],
                "meta": {
                    "count": len(results),
                    "refreshed": refreshed_count,
                    "failed": failed_count,
                },
            }
        )
        if failed_count and not refreshed_count:
            raise typer.Exit(EXIT_AUTH_REQUIRED)
        if failed_count:
            raise typer.Exit(EXIT_ERROR)
        return

    for r in results:
        if r.refreshed:
            when = (
                r.new_expires_at.isoformat().replace("+00:00", "Z")
                if r.new_expires_at
                else "(unknown)"
            )
            stderr.print(f"[green]Refreshed[/green] {r.name} — new expiry: {when}")
        else:
            stderr.print(
                f"[red]Failed[/red] {r.name}: "
                f"{r.error_code or 'ERROR'} — {r.error_message or ''}"
            )

    if failed_count and not refreshed_count:
        # Pick the most specific exit code across the failures. LOCK_HELD
        # beats anything else (operator may just need to retry), then
        # AUTH_REQUIRED for refresh/dead errors, then generic ERROR.
        failed_codes = [r.error_code for r in results if not r.refreshed]
        if any(c == "LOCK_HELD" for c in failed_codes):
            raise typer.Exit(EXIT_CONFLICT)
        mapped = [REFRESH_ERROR_TO_EXIT.get(c or "", EXIT_ERROR) for c in failed_codes]
        raise typer.Exit(max(mapped) if mapped else EXIT_ERROR)
    if failed_count:
        # Some succeeded, some failed. Still exit non-zero but make LOCK_HELD
        # observable so scripts can retry just the conflicted ones.
        failed_codes = [r.error_code for r in results if not r.refreshed]
        if any(c == "LOCK_HELD" for c in failed_codes):
            raise typer.Exit(EXIT_CONFLICT)
        raise typer.Exit(EXIT_ERROR)


@profiles_app.command("refresh")
def profiles_refresh(
    names: Annotated[
        list[str] | None,
        typer.Argument(
            help="Profile name(s) to refresh. Omit with --all / --expired / --soon.",
            autocompletion=_complete_profile_names,
        ),
    ] = None,
    all_profiles: Annotated[
        bool, typer.Option("--all", help="Refresh every discovered profile.")
    ] = False,
    expired_only: Annotated[
        bool,
        typer.Option(
            "--expired",
            help="Refresh only profiles whose access token is already expired.",
        ),
    ] = False,
    soon: Annotated[
        str | None,
        typer.Option(
            "--soon",
            help=(
                "Refresh profiles expiring within this window: '30m', '1h', "
                "'2d', '1w', or seconds. Includes already-expired tokens. "
                "Cron-friendly anticipatory refresh: */15 * * * * "
                "claude-lb refresh --soon 30m --json"
            ),
        ),
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="HTTP timeout per refresh, seconds.")
    ] = 10.0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Exchange stored refresh tokens for fresh access tokens (SPEC §10)."""
    soon_secs: int | None = None
    if soon is not None:
        soon_secs = _parse_duration(soon)
        if soon_secs is None:
            if json_output:
                emit_error_json(
                    "VALIDATION_ERROR",
                    f"invalid --soon value: {soon!r} "
                    "(use '30m', '1h', '2d', '1w', or seconds)",
                )
            stderr.print(
                f"[red]invalid --soon value:[/red] {soon!r} "
                "(use '30m', '1h', '2d', '1w', or seconds)"
            )
            raise typer.Exit(EXIT_VALIDATION)
    _run_refresh(
        list(names or []),
        all_profiles=all_profiles,
        expired_only=expired_only,
        soon_seconds=soon_secs,
        timeout=timeout,
        json_output=json_output,
    )


@app.command("refresh")
def top_refresh(
    names: Annotated[
        list[str] | None,
        typer.Argument(autocompletion=_complete_profile_names),
    ] = None,
    all_profiles: Annotated[bool, typer.Option("--all")] = False,
    expired_only: Annotated[bool, typer.Option("--expired")] = False,
    soon: Annotated[str | None, typer.Option("--soon")] = None,
    timeout: Annotated[float, typer.Option("--timeout")] = 10.0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Alias for `profiles refresh`."""
    profiles_refresh(
        names=names,
        all_profiles=all_profiles,
        expired_only=expired_only,
        soon=soon,
        timeout=timeout,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# exec — pick + run a child command
# ---------------------------------------------------------------------------


def _append_exec_log(
    profile: str,
    argv0: str,
    rc: int,
    duration_ms: int,
    *,
    full_argv: str | None = None,
) -> None:
    """Append an EXEC line to picks.log. Default logs argv[0] only; caller
    can opt into full argv (secrets risk — argv often contains tokens).
    """
    from datetime import UTC, datetime

    from .pick import pick_log_path

    target = pick_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    payload = full_argv if full_argv is not None else argv0
    line = f"{ts}\t{profile}\tEXEC\targv={payload}\trc={rc}\tdur={duration_ms}ms\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _pick_one(
    cache: HealthCache,
    names: list[str],
    *,
    strategy: Strategy,
    stickiness: int | None,
    require_ok: bool,
    auto_refresh: bool,
    exclude: set[str] | None = None,
) -> tuple[ProfileHealth, HealthCache]:
    """Run the full pick pipeline for `exec`, honoring auto-refresh + exclude.

    On failure, emits stderr + raises typer.Exit with the appropriate code
    (same mapping as `pick`). Returns (chosen, updated_cache) on success.
    """
    if auto_refresh:
        cache = _attempt_auto_refresh(cache, names)
    filtered = [n for n in names if n not in (exclude or set())]
    outcome = pick(
        cache,
        filtered,
        strategy=strategy,
        stickiness_s=stickiness,
        require_ok=require_ok,
    )
    if not outcome.ok:
        reason = outcome.reason or PickFailureReason.NO_PROFILES
        exit_code = REASON_TO_EXIT.get(reason, EXIT_ERROR)
        message = REASON_MESSAGES.get(reason, "Pick failed.")
        if outcome.earliest_recovery_at:
            message = (
                f"{message} Earliest recovery: "
                f"{outcome.earliest_recovery_at.isoformat().replace('+00:00', 'Z')}"
            )
        stderr.print(f"[red]{message}[/red]")
        raise typer.Exit(exit_code)
    assert outcome.chosen is not None
    return outcome.chosen, cache


def _reprobe_after_child(
    cache: HealthCache, name: str
) -> ProfileHealth | None:
    """Re-probe a single profile to see if its health shifted (post-child).

    Writes the fresh result to the cache. Used by exec's rate-limit retry
    heuristic — if a profile flips from OK to RATE_LIMITED/SESSION_LIMIT
    during the child's run, the child's non-zero exit was probably caused
    by hitting that limit.
    """
    profile = get_profile(name)
    if profile is None:
        return None
    prev = cache.profiles.get(name)
    prev_h = {name: prev.health} if prev is not None else None
    results = probe_many_sync([profile], prev_health=prev_h)
    if not results:
        return None
    fresh = results[0]
    cache.profiles[name] = fresh
    save_cache(cache)
    return fresh


def _child_hit_rate_limit(
    before: ProfileHealth | None, after: ProfileHealth | None
) -> bool:
    """True iff the profile was OK (or unknown) before and is now throttled.

    Conservative: requires a clear OK→throttled transition. Pre-existing
    throttled profiles don't trigger retry (pick would have filtered them).
    """
    if after is None:
        return False
    transient = {Health.RATE_LIMITED, Health.SESSION_LIMIT}
    if after.health not in transient:
        return False
    return before is None or before.health is Health.OK


@app.command(
    "exec",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    },
)
def exec_cmd(
    ctx: typer.Context,
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            help="Pick strategy.",
            autocompletion=_complete_strategy,
        ),
    ] = "sticky",
    stickiness: Annotated[
        int | None, typer.Option("--stickiness")
    ] = None,
    require_ok: Annotated[
        bool, typer.Option("--require-ok", help="Only pick an `ok` profile.")
    ] = False,
    auto_refresh: Annotated[
        bool,
        typer.Option(
            "--auto-refresh",
            help="Inline-refresh auth_expired profiles before picking.",
        ),
    ] = False,
    var_name: Annotated[
        str,
        typer.Option(
            "--var-name",
            help="Env var name set to the picked profile (default AXIOM_CLAUDE_PROFILE).",
        ),
    ] = "AXIOM_CLAUDE_PROFILE",
    retry_on_429: Annotated[
        int,
        typer.Option(
            "--retry-on-429",
            min=0,
            max=3,
            help=(
                "If the child exits non-zero AND the profile re-probes as "
                "rate_limited/session_limit, re-pick a different profile and "
                "rerun. Default 1; set 0 to disable."
            ),
        ),
    ] = 1,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help=(
                "Kill the child after N seconds. Exit 124 on timeout "
                "(POSIX convention). Default: no timeout."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Print the env assignment + command that would run, exit 0. "
                "Does not talk to Anthropic's API (uses cache only)."
            ),
        ),
    ] = False,
    log_full_argv: Annotated[
        bool,
        typer.Option(
            "--log-full-argv",
            help=(
                "Log the full child argv in picks.log (may contain secrets). "
                "Default logs only argv[0]."
            ),
        ),
    ] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age")] = None,
) -> None:
    """Pick a profile, run a child command with AXIOM_CLAUDE_PROFILE set.

    Everything after the last flag is passed to the child. Use `--` to
    disambiguate child flags from claude-lb's own flags:

        claude-lb exec --auto-refresh -- claude --dangerously-skip-permissions <args>

    claude-lb's exit code equals the child's exit code, so scripts can
    treat this as a transparent wrapper. Picks are audited to picks.log
    with rc + duration for post-hoc investigation.
    """
    import shlex

    chosen_strategy = _validate_strategy(strategy)
    argv = list(ctx.args or [])
    if not argv:
        stderr.print(
            "[red]exec requires a command.[/red] "
            "Example: claude-lb exec -- claude --help"
        )
        raise typer.Exit(EXIT_VALIDATION)

    cache, names = _load_or_probe(refresh=no_cache, max_age=max_age)
    chosen, cache = _pick_one(
        cache,
        names,
        strategy=chosen_strategy,
        stickiness=stickiness,
        require_ok=require_ok,
        auto_refresh=auto_refresh,
    )

    if dry_run:
        pretty = " ".join(shlex.quote(a) for a in argv)
        emit_text(f"{var_name}={chosen.name} {pretty}")
        return

    # Update last-pick + picks.log before handing off. If we crash during
    # the child's run, we still have an audit trail of what was dispatched.
    write_last_pick(chosen.name)
    score = 1.0 if chosen.health is Health.OK else 0.5
    append_pick_log(chosen.name, chosen_strategy, score)

    result: ExecResult = run_child(
        argv,
        env_var_name=var_name,
        profile_name=chosen.name,
        timeout=timeout,
    )

    full_argv_str = " ".join(shlex.quote(a) for a in argv) if log_full_argv else None
    _append_exec_log(
        chosen.name,
        argv[0],
        result.rc,
        result.duration_ms,
        full_argv=full_argv_str,
    )

    if result.timed_out:
        stderr.print(
            f"[yellow]timeout:[/yellow] child killed after {timeout}s "
            f"(rc={RC_TIMEOUT}, profile={chosen.name})"
        )
    elif result.not_found:
        stderr.print(
            f"[red]command not found:[/red] {argv[0]} "
            f"(rc={RC_NOT_FOUND})"
        )

    # Rate-limit retry: if the child failed AND re-probe shows the profile
    # flipped to throttled, retry once with a different profile. Timeout +
    # not_found are not rate-limit symptoms — skip the retry for those.
    if (
        result.rc != 0
        and retry_on_429 > 0
        and not result.timed_out
        and not result.not_found
    ):
        before = cache.profiles.get(chosen.name)
        after = _reprobe_after_child(cache, chosen.name)
        if _child_hit_rate_limit(before, after):
            stderr.print(
                f"[yellow]retry:[/yellow] {chosen.name} flipped to "
                f"{after.health.value if after else '?'} — retrying with another profile"
            )
            try:
                second, cache = _pick_one(
                    cache,
                    names,
                    strategy=chosen_strategy,
                    stickiness=stickiness,
                    require_ok=require_ok,
                    auto_refresh=auto_refresh,
                    exclude={chosen.name},
                )
            except typer.Exit:
                # No other candidates; propagate the child's original rc.
                raise typer.Exit(result.rc) from None

            write_last_pick(second.name)
            second_score = 1.0 if second.health is Health.OK else 0.5
            append_pick_log(second.name, chosen_strategy, second_score)

            result2 = run_child(
                argv,
                env_var_name=var_name,
                profile_name=second.name,
                timeout=timeout,
            )
            full_argv_str2 = (
                " ".join(shlex.quote(a) for a in argv) if log_full_argv else None
            )
            _append_exec_log(
                second.name,
                argv[0],
                result2.rc,
                result2.duration_ms,
                full_argv=full_argv_str2,
            )
            raise typer.Exit(result2.rc)

    raise typer.Exit(result.rc)


# ---------------------------------------------------------------------------
# add — onboard an existing .credentials.json into the multi-profile layout
# ---------------------------------------------------------------------------


@app.command("add")
def add(
    name: Annotated[
        str,
        typer.Argument(
            help=(
                "Name for the new profile. Becomes the directory under "
                "~/.claude-profiles/<NAME>/. Must match [A-Za-z0-9_-]+."
            ),
        ),
    ],
    from_path: Annotated[
        str | None,
        typer.Option(
            "--from",
            help=(
                "Source .credentials.json. Default: ~/.claude/.credentials.json "
                "(where `claude login` writes by default)."
            ),
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Overwrite an existing profile of the same name.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Import an existing .credentials.json into the multi-profile layout.

    Typical onboarding:

        claude login                # populates ~/.claude/.credentials.json
        claude-lb add personal      # imports it as 'personal'
        claude logout && claude login   # sign in to a different account
        claude-lb add work          # imports the new state as 'work'
        claude-lb status            # both visible, ready to balance

    Source file is copied (not moved) so the standard `claude` CLI keeps
    working unchanged. Profile name must match the discovery regex
    [A-Za-z0-9_-]+; anything else is rejected.
    """
    import json as _json
    import re as _re
    import shutil as _shutil
    from pathlib import Path as _Path

    from .paths import profiles_dir as _profiles_dir

    if not _re.match(r"^[A-Za-z0-9_-]+$", name):
        msg = (
            f"invalid profile name: {name!r} (must match [A-Za-z0-9_-]+)"
        )
        if json_output:
            emit_error_json("VALIDATION_ERROR", msg)
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(EXIT_VALIDATION)

    src = _Path(from_path).expanduser() if from_path else _Path.home() / ".claude" / ".credentials.json"
    if not src.is_file():
        hint = (
            "" if from_path else
            " (default location — run `claude login` first, or pass --from PATH)"
        )
        msg = f"source not found: {src}{hint}"
        if json_output:
            emit_error_json("NOT_FOUND", msg)
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(EXIT_NOT_FOUND)

    # Validate parseable JSON + recognisable token shape before committing
    # to the copy. Better to refuse early than to leave a broken profile
    # on disk that subsequent `pick` calls will silently skip.
    try:
        with src.open("rb") as fh:
            payload = _json.load(fh)
    except (OSError, ValueError) as exc:
        if json_output:
            emit_error_json("VALIDATION_ERROR", f"source not parseable: {exc}")
        stderr.print(f"[red]source not parseable JSON:[/red] {exc}")
        raise typer.Exit(EXIT_VALIDATION) from None
    if not isinstance(payload, dict):
        msg = "source must be a JSON object (got list/scalar)"
        if json_output:
            emit_error_json("VALIDATION_ERROR", msg)
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(EXIT_VALIDATION)
    # Light sanity check: at least one of the recognised token shapes is present.
    has_token = (
        isinstance(payload.get("claudeAiOauth"), dict)
        and isinstance(payload["claudeAiOauth"].get("accessToken"), str)
        and bool(payload["claudeAiOauth"]["accessToken"].strip())
    ) or any(
        isinstance(payload.get(k), str) and payload[k].strip()
        for k in ("oauthAccessToken", "accessToken")
    )
    if not has_token:
        msg = (
            "source has no recognised access token "
            "(expected claudeAiOauth.accessToken, oauthAccessToken, or accessToken)"
        )
        if json_output:
            emit_error_json("VALIDATION_ERROR", msg)
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(EXIT_VALIDATION)

    dest_dir = _profiles_dir() / name
    dest = dest_dir / ".credentials.json"
    if dest.exists() and not force:
        msg = (
            f"profile {name!r} already exists at {dest}. "
            "Pass --force to overwrite."
        )
        if json_output:
            emit_error_json("CONFLICT", msg)
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(EXIT_CONFLICT)

    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        _shutil.copy2(src, dest)
    except OSError as exc:
        if json_output:
            emit_error_json("ERROR", f"copy failed: {exc}")
        stderr.print(f"[red]copy failed:[/red] {exc}")
        raise typer.Exit(EXIT_ERROR) from None

    if json_output:
        emit_json({
            "data": {
                "name": name,
                "source": str(src),
                "destination": str(dest),
                "overwritten": force and dest.exists(),
            },
            "meta": {"action": "added"},
        })
        return
    stderr.print(
        f"[green]added[/green] profile {name!r} → {dest}"
    )
    stderr.print(
        "  next: claude-lb probe " + name + "  (or `claude-lb status` to see all)"
    )


# ---------------------------------------------------------------------------
# history — read picks.log
# ---------------------------------------------------------------------------


def _parse_pick_log(path: Any) -> list[dict[str, Any]]:
    """Parse picks.log into structured entries.

    Lines are tab-separated:
        {ts}\\t{profile}\\t{action}\\t{key=value}...
    Where action is a strategy name (`sticky`, `least-used`, ...) for picks
    or `EXEC` for child runs. Malformed lines are skipped silently — the log
    is rotated under load and a partial last line is plausible.
    """
    from datetime import UTC, datetime as _dt
    from pathlib import Path as _Path

    entries: list[dict[str, Any]] = []
    if not _Path(str(path)).is_file():
        return entries
    try:
        text = _Path(str(path)).read_text(encoding="utf-8")
    except OSError:
        return entries
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ts_raw, profile, action = parts[0], parts[1], parts[2]
        try:
            iso = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
            ts = _dt.fromisoformat(iso)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except ValueError:
            continue
        details: dict[str, str] = {}
        for chunk in parts[3:]:
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                details[k] = v
        entries.append({
            "timestamp": ts,
            "profile": profile,
            "action": action,
            "details": details,
        })
    return entries


@app.command("history")
def history(
    tail: Annotated[
        int,
        typer.Option(
            "--tail", "-n", min=1,
            help="Show the last N entries (default 20).",
        ),
    ] = 20,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Filter to one profile only.",
            autocompletion=_complete_profile_names,
        ),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help=(
                "Filter to entries within this window: '30m', '1h', '2d', "
                "'1w', or seconds as an integer."
            ),
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show recent picks + exec invocations from picks.log.

    Filters compose: `--profile account-a --since 1h --tail 5` shows the last
    five entries for account-a within the past hour.
    """
    from datetime import UTC, datetime as _dt, timedelta

    from .pick import pick_log_path

    target = pick_log_path()
    cutoff: _dt | None = None
    if since is not None:
        secs = _parse_duration(since)
        if secs is None:
            stderr.print(
                f"[red]invalid --since value:[/red] {since!r} "
                "(use '30m', '1h', '2d', '1w', or seconds)"
            )
            raise typer.Exit(EXIT_VALIDATION)
        cutoff = _dt.now(UTC) - timedelta(seconds=secs)

    all_entries = _parse_pick_log(target)
    filtered = all_entries
    if profile:
        filtered = [e for e in filtered if e["profile"] == profile]
    if cutoff is not None:
        filtered = [e for e in filtered if e["timestamp"] >= cutoff]
    final = filtered[-tail:] if tail > 0 else filtered

    if json_output:
        emit_json({
            "data": [
                {
                    "timestamp": e["timestamp"].isoformat().replace("+00:00", "Z"),
                    "profile": e["profile"],
                    "action": e["action"],
                    "details": e["details"],
                }
                for e in final
            ],
            "meta": {
                "count": len(final),
                "total_in_log": len(all_entries),
                "log_path": str(target),
                "filters": {
                    "tail": tail,
                    "profile": profile,
                    "since": since,
                },
            },
        })
        return

    if not final:
        if not target.is_file():
            stderr.print("[yellow]No history yet.[/yellow] (no picks.log)")
        else:
            stderr.print("[yellow]No matching entries.[/yellow]")
        return

    from rich.table import Table

    table = Table(show_lines=False, expand=False)
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Profile")
    table.add_column("Action")
    table.add_column("Detail", overflow="fold")

    now = _dt.now(UTC)
    for e in final:
        elapsed = (now - e["timestamp"]).total_seconds()
        when = _humanize_elapsed(elapsed)
        if e["action"] == "EXEC":
            argv = e["details"].get("argv", "")
            rc = e["details"].get("rc", "?")
            dur = e["details"].get("dur", "")
            detail = f"argv={argv} rc={rc} dur={dur}".strip()
            action_style = (
                f"[green]EXEC[/green]" if rc == "0" else f"[red]EXEC[/red]"
            )
        else:
            detail = " ".join(f"{k}={v}" for k, v in e["details"].items())
            action_style = e["action"]
        table.add_row(when, e["profile"], action_style, detail)

    stderr.print(table)
    emit_text(f"{len(final)} of {len(all_entries)} entries")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command("doctor")
def doctor(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    skip_network: Annotated[
        bool,
        typer.Option("--skip-network", help="Skip the api.anthropic.com reachability probe."),
    ] = False,
) -> None:
    """Diagnose the local setup — config dir, profiles, credentials, cache, network."""
    report = run_doctor(skip_network=skip_network)
    if json_output:
        emit_json(report_to_dict(report))
        if not report.all_passed:
            raise typer.Exit(EXIT_ERROR)
        return
    stderr.print(f"[bold]claude-lb doctor[/bold] (v{report.version})")
    for check in report.checks:
        # WARN: passed but flagged a non-fatal concern (e.g. profiles without
        # refresh tokens — valid setup, just won't auto-heal).
        if check.passed and check.extra.get("warning"):
            mark = "[yellow]WARN[/yellow]"
        elif check.passed:
            mark = "[green]OK[/green]"
        else:
            mark = "[red]FAIL[/red]"
        stderr.print(f"  {mark}  {check.name}: {check.detail}")
    if report.all_passed:
        stderr.print("[green]All checks passed.[/green]")
    else:
        failed = [c.name for c in report.checks if not c.passed]
        stderr.print(f"[red]{len(failed)} check(s) failed:[/red] {', '.join(failed)}")
        raise typer.Exit(EXIT_ERROR)


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@app.command("update")
def update(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help=(
                "Actually perform the upgrade in-place: `git pull --ff-only` "
                "(if the install is a git working copy) then "
                "`uv tool install --reinstall --editable <install-dir>`. "
                "Without this flag, `update` only reports status."
            ),
        ),
    ] = False,
    no_pull: Annotated[
        bool,
        typer.Option(
            "--no-pull",
            help="With --apply, skip `git pull` and just re-sync deps.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check for or apply an in-place upgrade.

    Default is status-only. Pass `--apply` to run `git pull --ff-only` and
    `uv tool install --reinstall --editable`. Useful after a dep has been
    added to `pyproject.toml` but the tool venv hasn't been re-synced —
    editable-install drift is the single most common support issue.
    """
    if apply:
        result = apply_update(pull=not no_pull)
        if json_output:
            emit_json(apply_result_to_dict(result))
            if not result.applied:
                raise typer.Exit(EXIT_ERROR)
            return
        if result.error:
            stderr.print(f"[red]Update failed:[/red] {result.error}")
            if result.stdout:
                stderr.print(result.stdout)
            raise typer.Exit(EXIT_ERROR)
        stderr.print(
            f"[green]Update applied[/green] — "
            f"pulled={result.pulled}, reinstalled={result.reinstalled}"
        )
        if result.stdout:
            stderr.print(result.stdout)
        return

    status = check_for_update()
    if json_output:
        emit_json(status_to_dict(status))
        return
    stderr.print(f"[bold]claude-lb[/bold] {status.current_version}")
    if status.install_dir:
        stderr.print(f"  install: {status.install_dir}")
    if status.is_git_repo and status.local_commit:
        stderr.print(f"  commit:  {status.local_commit[:12]}")
        if status.behind is not None:
            if status.behind == 0 and (status.ahead or 0) == 0:
                stderr.print("  status:  [green]up-to-date[/green]")
            else:
                stderr.print(
                    f"  status:  [yellow]{status.ahead or 0} ahead, "
                    f"{status.behind} behind[/yellow]"
                )
    stderr.print(f"  hint:    {status.upgrade_hint}")
    stderr.print("  [dim](run with --apply to reinstall in-place)[/dim]")


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def _summary_line(cache: HealthCache, names: list[str]) -> str:
    counts: dict[str, int] = {}
    for n in names:
        entry = cache.profiles.get(n)
        key = entry.health.value if entry else "unknown"
        counts[key] = counts.get(key, 0) + 1
    total = len(names)
    parts = [f"{total} profile{'s' if total != 1 else ''}"]
    for k in ("ok", "rate_limited", "session_limit", "weekly_limit", "auth_expired", "auth_dead", "network_error", "unknown"):
        if counts.get(k, 0):
            parts.append(f"{counts[k]} {k}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Silence a lint warning; ProfileHealth import is used in type-only contexts
# elsewhere but we want to keep it top-level for IDE navigation.
# ---------------------------------------------------------------------------
_ = ProfileHealth


if __name__ == "__main__":
    app()
