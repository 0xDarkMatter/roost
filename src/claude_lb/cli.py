"""Typer CLI entry point (SPEC §2, §4)."""

from __future__ import annotations

import logging
import sys
from datetime import UTC
from typing import Annotated

import typer

from . import __version__
from .cache import is_entry_fresh, load_cache, remove_profile, save_cache
from .discovery import discover_profiles, get_profile
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
EXIT_TIMEOUT = 8
EXIT_UNAVAILABLE = 9

REASON_TO_EXIT: dict[PickFailureReason, int] = {
    PickFailureReason.NO_PROFILES: EXIT_UNAVAILABLE,
    PickFailureReason.ALL_AUTH_DEAD: EXIT_AUTH_REQUIRED,
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
) -> None:
    """Alias for `profiles probe`."""
    profiles_probe(name=name, json_output=json_output)


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
        payload: dict = {
            "data": {
                "name": name,
                "credentials_path": profile.credentials_path,
                "token_source": profile.token_source,
            }
        }
        if entry is not None:
            payload["data"]["health"] = entry.health.value
            payload["data"]["probed_at"] = entry.probed_at.isoformat().replace(
                "+00:00", "Z"
            )
            payload["data"]["expires_at"] = (
                entry.expires_at.isoformat().replace("+00:00", "Z")
                if entry.expires_at
                else None
            )
            payload["data"]["retry_after_s"] = entry.retry_after_s
            payload["data"]["usage"] = entry.usage.model_dump() if entry.usage else None
            payload["data"]["error"] = entry.error.model_dump() if entry.error else None
            payload["data"]["probe_latency_ms"] = entry.probe_latency_ms
        else:
            payload["data"]["health"] = "unknown"
        emit_json(payload)
        return
    stderr.print(f"[bold]{name}[/bold]")
    stderr.print(f"  credentials: {profile.credentials_path}")
    stderr.print(f"  token_source: {profile.token_source}")
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
) -> None:
    """Pick the best healthy profile for scripting."""
    chosen_strategy = _validate_strategy(strategy)
    cache, names = _load_or_probe(refresh=no_cache, max_age=max_age)

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
    for k in ("ok", "rate_limited", "session_limit", "weekly_limit", "auth_dead", "network_error", "unknown"):
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
