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
from .probe import probe_many_sync
from .refresh import refresh_many_sync
from .updater import apply_result_to_dict, apply_update, check_for_update, status_to_dict

app = typer.Typer(
    name="claude-lb",
    help="Pick the healthiest Claude Code Max profile — health taxonomy + load balancer.",
    no_args_is_help=True,
    add_completion=False,
)

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

    # Refresh bumped credentials mtime (implicit invalidation). Re-probe the
    # refreshed profiles so pick() sees the new health state.
    probed = probe_many_sync(refreshed_profiles)
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
        typer.Argument(help="Probe only this profile (default: all)."),
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
    name: Annotated[str | None, typer.Argument()] = None,
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
) -> None:
    """Show cached health per profile (probes if stale)."""
    cache, names = _load_or_probe(refresh=(no_cache or refresh), max_age=max_age)
    if json_output:
        emit_json(build_status_payload(cache, names))
        return
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
) -> None:
    """Alias for `profiles status`."""
    profiles_status(
        json_output=json_output,
        no_cache=no_cache,
        refresh=refresh,
        max_age=max_age,
    )


# ---------------------------------------------------------------------------
# profiles show (alias: show)
# ---------------------------------------------------------------------------


@profiles_app.command("show")
def profiles_show(
    name: Annotated[str, typer.Argument(help="Profile name.")],
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
    name: Annotated[str, typer.Argument()],
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
        typer.Option("--strategy", help="sticky | least-used | round-robin | weighted | first-healthy"),
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
) -> None:
    """Pick the best healthy profile for scripting."""
    chosen_strategy = _validate_strategy(strategy)
    if warn_at is not None and not (0 <= warn_at <= 100):
        stderr.print(f"[red]--warn-at must be between 0 and 100, got:[/red] {warn_at}")
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
    score = 1.0 if chosen.health is Health.OK else 0.5
    write_last_pick(chosen.name)
    append_pick_log(chosen.name, outcome.strategy_used or chosen_strategy, score)

    # --warn-at: non-fatal stderr hint if utilisation exceeds threshold.
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
        emit_json({
            "data": {
                "name": chosen.name,
                "health": chosen.health.value,
                "score": score,
                "rationale": outcome.rationale,
                "strategy": (outcome.strategy_used or chosen_strategy).value,
            }
        })
        return
    if export:
        emit_text(f"{var_name}={chosen.name}")
        return
    emit_text(chosen.name)


@app.command("pick")
def top_pick(
    strategy: Annotated[str, typer.Option("--strategy")] = "sticky",
    stickiness: Annotated[int | None, typer.Option("--stickiness")] = None,
    require_ok: Annotated[bool, typer.Option("--require-ok")] = False,
    export: Annotated[bool, typer.Option("--export")] = False,
    var_name: Annotated[str, typer.Option("--var-name")] = "AXIOM_CLAUDE_PROFILE",
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age")] = None,
    warn_at: Annotated[int | None, typer.Option("--warn-at")] = None,
    auto_refresh: Annotated[bool, typer.Option("--auto-refresh")] = False,
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
    )


# ---------------------------------------------------------------------------
# profiles invalidate (alias: invalidate)
# ---------------------------------------------------------------------------


@profiles_app.command("invalidate")
def profiles_invalidate(
    name: Annotated[str, typer.Argument(help="Profile to invalidate.")],
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
    name: Annotated[str, typer.Argument()],
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
    timeout: float,
    json_output: bool,
) -> None:
    from datetime import UTC, datetime

    discovered = discover_profiles()
    if not discovered:
        if json_output:
            emit_error_json("NOT_FOUND", "No profiles discovered.")
        stderr.print("[yellow]No profiles discovered.[/yellow]")
        raise typer.Exit(EXIT_UNAVAILABLE)

    by_name = {p.name: p for p in discovered}

    if names and (all_profiles or expired_only):
        if json_output:
            emit_error_json(
                "VALIDATION_ERROR",
                "Pass either <name>... or --all/--expired, not both.",
            )
        stderr.print("[red]Pass either <name>... or --all/--expired, not both.[/red]")
        raise typer.Exit(EXIT_VALIDATION)
    if not names and not all_profiles and not expired_only:
        if json_output:
            emit_error_json(
                "VALIDATION_ERROR",
                "Specify one or more profile names, --all, or --expired.",
            )
        stderr.print(
            "[red]Specify one or more profile names, --all, or --expired.[/red]"
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
        typer.Argument(help="Profile name(s) to refresh. Omit with --all or --expired."),
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
    timeout: Annotated[
        float, typer.Option("--timeout", help="HTTP timeout per refresh, seconds.")
    ] = 10.0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Exchange stored refresh tokens for fresh access tokens (SPEC §10)."""
    _run_refresh(
        list(names or []),
        all_profiles=all_profiles,
        expired_only=expired_only,
        timeout=timeout,
        json_output=json_output,
    )


@app.command("refresh")
def top_refresh(
    names: Annotated[list[str] | None, typer.Argument()] = None,
    all_profiles: Annotated[bool, typer.Option("--all")] = False,
    expired_only: Annotated[bool, typer.Option("--expired")] = False,
    timeout: Annotated[float, typer.Option("--timeout")] = 10.0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Alias for `profiles refresh`."""
    _run_refresh(
        list(names or []),
        all_profiles=all_profiles,
        expired_only=expired_only,
        timeout=timeout,
        json_output=json_output,
    )


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
        mark = "[green]OK[/green]" if check.passed else "[red]FAIL[/red]"
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
