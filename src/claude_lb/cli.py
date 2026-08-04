"""Typer CLI entry point (SPEC §2, §4)."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC
from typing import Annotated, Any

import typer

from . import __version__
from .cache import is_entry_fresh, load_cache, remove_profile, save_cache
from .discovery import discover_profiles, get_profile
from .doctor import report_to_dict, run_doctor
from .exec_cmd import RC_NOT_FOUND, RC_TIMEOUT, ExecResult, run_child
from .models import Health, HealthCache, ProfileHealth
from .output import (
    build_status_payload,
    emit_error_json,
    emit_json,
    emit_text,
    render_capacity_cards,
    render_pick_explanation,
    render_status_table,
    stderr,
)
from .term import Term, emit_panel
from .widget import render_widget
from .pick import (
    PickFailureReason,
    Strategy,
    append_pick_log,
    pick,
    write_last_pick,
)
from .platform_status import (
    format_status_line as _format_platform_status_line,
)
from .platform_status import (
    load_or_fetch as _load_platform_status,
)
from .platform_status import (
    to_json_meta as _platform_status_to_meta,
)
from .probe import probe_many_sync
from .refresh import refresh_many_sync
from .updater import apply_result_to_dict, apply_update, check_for_update, status_to_dict

app = typer.Typer(
    name="roost",
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
    "LEASE_HELD": EXIT_CONFLICT,
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
    # Mirrors ALL_WEEKLY: the accounts are alive but none can serve the
    # scoped model until its window resets, which is an availability problem,
    # not an auth or throttling one. Without this entry the reason fell
    # through to the generic exit 1, so a script could not tell "every
    # profile is model-limited" from an unexpected crash.
    PickFailureReason.ALL_MODEL_LIMIT: EXIT_UNAVAILABLE,
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
        "All access tokens expired. Run: roost refresh --expired"
    ),
    PickFailureReason.ALL_WEEKLY: "All profiles weekly-exhausted.",
    PickFailureReason.ALL_MODEL_LIMIT: (
        "All profiles model-limited. The accounts are healthy but none can "
        "serve the scoped model until its window resets. Run: roost status"
    ),
    PickFailureReason.ALL_THROTTLED: "All profiles throttled.",
    PickFailureReason.ALL_TERMINAL: "No profiles available. Run: roost status",
    PickFailureReason.REQUIRE_OK_NONE: "No profiles currently ok. Run: roost probe",
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
    *, refresh: bool, max_age: int | None
) -> tuple[HealthCache, list[str]]:
    """Return (possibly-refreshed cache, discovered profile names).

    If `refresh` is True, all profiles are probed.
    If `max_age` is set, it overrides the per-state TTLs (stale entries are re-probed).
    """
    profiles = discover_profiles()
    names = [p.name for p in profiles]
    if not profiles:
        return load_cache(), names

    cache = load_cache()
    now_profiles = {p.name: p for p in profiles}

    to_probe: list[str] = []
    for p in profiles:
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
            if age > max_age:  # pragma: no branch  -- always-fresh test path covered separately
                fresh = False
        if not fresh:
            to_probe.append(p.name)

    if to_probe:
        targets = [now_profiles[n] for n in to_probe]
        prev = {n: cache.profiles[n].health for n in to_probe if n in cache.profiles}
        # Carry consecutive_failures across the probe so backoff can advance.
        prev_failures = {
            n: cache.profiles[n].consecutive_failures
            for n in to_probe if n in cache.profiles
        }
        results = probe_many_sync(targets, prev_health=prev, prev_failures=prev_failures)
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
    for stale in refreshed_profiles:  # pragma: no branch  -- iteration always yields when refreshed list non-empty
        fresh = get_profile(stale.name)
        if fresh is not None:  # pragma: no branch  -- discovery just succeeded; profile won't vanish here
            fresh_profiles.append(fresh)

    if not fresh_profiles:  # pragma: no cover  -- get_profile re-discovery should never lose a refreshed profile
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
        emit_text(f"roost {__version__}")
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
    """roost — pick the healthiest Claude Code Max profile."""
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
    cards: Annotated[
        bool,
        typer.Option(
            "--cards",
            help="Render capacity cards instead of the table.",
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
    if cards:
        render_capacity_cards(entries)
    else:
        render_status_table(entries)
    emit_text(_summary_line(cache, names))


@app.command("status")
def top_status(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age")] = None,
    no_platform_status: Annotated[bool, typer.Option("--no-platform-status")] = False,
    cards: Annotated[bool, typer.Option("--cards")] = False,
) -> None:
    """Alias for `profiles status`."""
    profiles_status(
        json_output=json_output,
        no_cache=no_cache,
        refresh=refresh,
        max_age=max_age,
        no_platform_status=no_platform_status,
        cards=cards,
    )


@app.command("widget")
def status_widget(
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ignore cache; probe everything.")
    ] = False,
    max_age: Annotated[
        int | None, typer.Option("--max-age", help="Override TTL, seconds.")
    ] = None,
    no_platform_status: Annotated[
        bool, typer.Option("--no-platform-status", help="Skip the status.claude.com check.")
    ] = False,
    max_kb: Annotated[
        int,
        typer.Option("--max-kb", help="Byte budget for the emitted HTML."),
    ] = 28,
) -> None:
    """Emit capacity cards as self-contained HTML for Claude Code's show_widget.

    The whole document goes to stdout so it can be piped straight into the
    tool. Nothing else may print there (rule 8) — the byte-budget warning and
    any platform-status chatter stay on stderr.
    """
    cache, names = _load_or_probe(refresh=no_cache, max_age=max_age)
    platform = None if no_platform_status else _load_platform_status(
        force_refresh=no_cache,
    )
    meta_extra = _platform_status_to_meta(platform) if platform is not None else None
    payload = build_status_payload(cache, names, platform_status_meta=meta_extra)
    emit_text(
        render_widget(
            payload["data"],
            payload["meta"],
            max_bytes=max_kb * 1024,
            recommended=_recommended_profile(cache, names),
            stats=_widget_stats(names),
        )
    )


def _widget_stats(names: list[str]) -> dict[str, dict[str, Any]]:
    """Per-profile dispatch history and capacity trend for the cards.

    Sourced from roost's own logs, not the usage API: pick/exec counts from
    picks.log, the trend series and burn-rate ETA from the opt-in usage log.
    The API reports no tokens and no turns, so there is deliberately nothing
    resembling a token count here.

    Entirely best-effort. The usage log is opt-in and off by default, picks.log
    may not exist on a fresh install, and neither is worth failing a render
    over — a missing series renders as no chip rather than a zero.
    """
    from datetime import datetime, timedelta

    from .paths import pick_log_path
    from .output import humanize_until
    from .stats import (
        aggregate_stats,
        parse_pick_log,
        project_exhaustion,
        summarise_metric,
    )
    from .usage_log import iter_records

    out: dict[str, dict[str, Any]] = {name: {} for name in names}
    try:
        records = parse_pick_log(pick_log_path())
        report = aggregate_stats(records)
        # Per-profile exec latency and failure rate. aggregate_stats reports
        # both fleet-wide only, but a card is about one profile — a global
        # p50 on four cards is the same number four times.
        durations: dict[str, list[float]] = {name: [] for name in names}
        failures: dict[str, int] = dict.fromkeys(names, 0)
        for record in records:
            if record.get("action") != "EXEC":
                continue
            profile = str(record.get("profile") or "")
            if profile not in durations:
                continue
            details = record.get("details") or {}
            # The field is written as `dur=10030ms` — a number with a unit
            # glued on, not a bare int. Reading it as one silently yielded no
            # samples at all, so the median chip never appeared.
            raw = str(details.get("dur", "")).removesuffix("ms")
            try:
                durations[profile].append(float(raw))
            except (TypeError, ValueError):
                pass
            if str(details.get("rc", "0")) not in ("0", ""):
                failures[profile] += 1
        for name in names:
            picks = report.pick_by_profile.get(name)
            if picks:
                out[name]["picks"] = picks
            execs = report.exec_by_profile.get(name)
            if execs:
                out[name]["execs"] = execs
                out[name]["fail_pct"] = round(100 * failures[name] / execs)
            samples = sorted(durations[name])
            if samples:
                out[name]["p50_ms"] = samples[len(samples) // 2]
    except (OSError, ValueError):
        pass
    try:
        records = list(iter_records())
        now = datetime.now(UTC)
        for summary in summarise_metric(records, metric="weekly_pct"):
            if summary.profile not in out:
                continue
            out[summary.profile]["trend"] = [value for _ts, value in summary.series]
            # project_exhaustion returns SECONDS until the metric hits 100,
            # not a timestamp — humanize_until wants the latter.
            seconds = project_exhaustion(summary)
            if seconds is not None:
                out[summary.profile]["eta"] = humanize_until(
                    now + timedelta(seconds=seconds), now
                ).removeprefix("in ")
    except (OSError, ValueError):
        pass
    return out


def _recommended_profile(cache: HealthCache, names: list[str]) -> dict[str, Any]:
    """What `pick` WOULD return, with `which`'s read-only guarantee.

    `pick()` itself is pure — the side effects (`append_pick_log`,
    `write_last_pick`) live here in the CLI layer, so calling it directly is
    safe. That is the whole point: rendering a dashboard must never move the
    stickiness pointer or write an audit entry, or merely *looking* at the
    fleet would change which profile the next dispatch gets (AGENTS.md rule
    21). Do not "simplify" this into a call to the pick command.

    Stickiness is disabled deliberately. A sticky answer reports whatever was
    picked last rather than what is healthiest now, which is the opposite of
    what a fleet overview is for.
    """
    outcome = pick(cache, names, strategy=Strategy.LEAST_USED, stickiness_s=0)
    if outcome.chosen is not None:
        return {"name": outcome.chosen.name, "rationale": outcome.rationale}
    return {
        "reason": REASON_MESSAGES.get(
            outcome.reason, "no profile currently selectable"
        )
        if outcome.reason is not None
        else "no profile currently selectable"
    }


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
            if dt is None:  # pragma: no cover  -- helper guards a None defensively; entry-not-None path already returns
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
    if profile.subscription_type:  # pragma: no branch  -- factory always sets this; no-subscription profiles are rare
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
        typer.Option("--var-name", help="Var name for --export (default ROOST_PROFILE)."),
    ] = "ROOST_PROFILE",
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
    avoid: Annotated[
        list[str] | None,
        typer.Option(
            "--avoid",
            help=(
                "Exclude this profile from selection. Repeatable: "
                "`--avoid a --avoid b`. Composes with all strategies and with "
                "--count. Stickiness is bypassed if the sticky pick is avoided."
            ),
            autocompletion=_complete_profile_names,
        ),
    ] = None,
    fallback: Annotated[
        str | None,
        typer.Option(
            "--fallback",
            help=(
                "If the primary pick fails for any reason, return this profile "
                "name with a stderr warning. Useful for scripts that prefer "
                "'any profile' over 'no profile'. The fallback is returned only "
                "when it exists in discovery — non-existent fallbacks propagate "
                "the original failure."
            ),
            autocompletion=_complete_profile_names,
        ),
    ] = None,
    max_cost: Annotated[
        int | None,
        typer.Option(
            "--max-cost",
            help=(
                "Skip profiles whose monthly overage utilization is >= N%%. "
                "Profiles without overage data (Pro/Team plans, or Max plans "
                "with overage disabled) are NEVER excluded — they have no "
                "cost signal to gate against."
            ),
            min=0,
            max=100,
        ),
    ] = None,
    explain: Annotated[
        bool,
        typer.Option(
            "--explain",
            help=(
                "Render the pick decision tree to stderr: discovered names, "
                "ladder exclusions with reasons, surviving candidates with "
                "strategy scores. In --json mode the same data is folded into "
                "data.explain instead."
            ),
        ),
    ] = False,
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
    avoid_set = set(avoid or [])
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
        avoid=avoid_set,
        max_cost=max_cost,
    )

    # --explain rendering kicks in regardless of success/failure. In text mode
    # it prints a Rich table to stderr; in JSON mode it's folded into the
    # data.explain block (success) or error.details.explain (failure).
    explain_payload: dict[str, Any] | None = None
    if explain:
        explain_payload = {
            "discovered": names,
            "chosen": outcome.chosen.name if outcome.chosen else None,
            "strategy_used": (
                outcome.strategy_used.value
                if outcome.strategy_used else chosen_strategy.value
            ),
            "rationale": outcome.rationale,
            "excluded_reasons": outcome.excluded_reasons,
            "filter_scores": outcome.filter_scores,
        }
        if not json_output:
            render_pick_explanation(
                discovered_names=names,
                chosen=outcome.chosen.name if outcome.chosen else None,
                strategy=(
                    outcome.strategy_used.value
                    if outcome.strategy_used else chosen_strategy.value
                ),
                rationale=outcome.rationale,
                excluded_reasons=outcome.excluded_reasons,
                filter_scores=outcome.filter_scores,
            )

    if not outcome.ok:
        # --fallback intercepts failure before any error rendering: if the
        # fallback name is in discovery (and not itself avoided), use it with
        # a stderr warning. Cache health is not consulted — fallback's whole
        # job is "give me SOMETHING" when the ladder + strategy say no.
        if fallback is not None and fallback in names and fallback not in avoid_set:
            stderr.print(
                f"[yellow]fallback:[/yellow] primary pick failed "
                f"({(outcome.reason or PickFailureReason.NO_PROFILES).value}); "
                f"using {fallback}"
            )
            from datetime import datetime as _dt_fallback

            entry = cache.profiles.get(fallback) or ProfileHealth(
                name=fallback,
                health=Health.UNKNOWN,
                probed_at=_dt_fallback.now(UTC),
            )
            write_last_pick(fallback)
            score = 1.0 if entry.health is Health.OK else 0.5
            append_pick_log(fallback, chosen_strategy, score)
            if json_output:
                payload: dict[str, Any] = {
                    "data": {
                        "name": fallback,
                        "health": entry.health.value,
                        "score": score,
                        "rationale": "fallback: primary pick failed",
                        "strategy": chosen_strategy.value,
                    }
                }
                if explain_payload is not None:
                    payload["data"]["explain"] = explain_payload
                emit_json(payload)
            elif export:
                emit_text(f"{var_name}={fallback}")
            else:
                emit_text(fallback)
            return
        reason = outcome.reason or PickFailureReason.NO_PROFILES
        exit_code = REASON_TO_EXIT.get(reason, EXIT_ERROR)
        message = REASON_MESSAGES.get(reason, "Pick failed.")
        if outcome.earliest_recovery_at:
            message = (
                f"{message} Earliest recovery: "
                f"{outcome.earliest_recovery_at.isoformat().replace('+00:00', 'Z')}"
            )
        if json_output:
            details: dict[str, Any] = {
                "earliest_recovery_at": (
                    outcome.earliest_recovery_at.isoformat().replace("+00:00", "Z")
                    if outcome.earliest_recovery_at else None
                ),
            }
            if explain_payload is not None:
                details["explain"] = explain_payload
            emit_error_json(reason.value.upper(), message, details)
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
            data: dict[str, Any] = {
                "name": chosen.name,
                "health": chosen.health.value,
                "score": primary_score,
                "rationale": outcome.rationale,
                "strategy": strategy_used.value,
            }
            if explain_payload is not None:
                data["explain"] = explain_payload
            emit_json({"data": data})
        else:
            meta: dict[str, Any] = {
                "count": len(picks),
                "requested": count,
                "strategy": strategy_used.value,
                "rationale": outcome.rationale,
            }
            if explain_payload is not None:
                meta["explain"] = explain_payload
            emit_json({
                "data": [
                    {
                        "name": e.name,
                        "health": e.health.value,
                        "score": 1.0 if e.health is Health.OK else 0.5,
                    }
                    for e in picks
                ],
                "meta": meta,
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
    var_name: Annotated[str, typer.Option("--var-name")] = "ROOST_PROFILE",
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age")] = None,
    warn_at: Annotated[int | None, typer.Option("--warn-at")] = None,
    auto_refresh: Annotated[bool, typer.Option("--auto-refresh")] = False,
    count: Annotated[int, typer.Option("--count", "-n", min=1)] = 1,
    avoid: Annotated[
        list[str] | None,
        typer.Option("--avoid", autocompletion=_complete_profile_names),
    ] = None,
    fallback: Annotated[
        str | None,
        typer.Option("--fallback", autocompletion=_complete_profile_names),
    ] = None,
    max_cost: Annotated[
        int | None, typer.Option("--max-cost", min=0, max=100)
    ] = None,
    explain: Annotated[bool, typer.Option("--explain")] = False,
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
        avoid=avoid,
        fallback=fallback,
        max_cost=max_cost,
        explain=explain,
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
    jitter_s: float,
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

    results = refresh_many_sync(targets, timeout=timeout, jitter_s=jitter_s)
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
        if failed_count and not refreshed_count:  # pragma: no cover  -- duplicate of text-mode branch below; covered via non-JSON tests
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
        if any(c in ("LOCK_HELD", "LEASE_HELD") for c in failed_codes):
            raise typer.Exit(EXIT_CONFLICT)
        mapped = [REFRESH_ERROR_TO_EXIT.get(c or "", EXIT_ERROR) for c in failed_codes]
        raise typer.Exit(max(mapped) if mapped else EXIT_ERROR)
    if failed_count:
        # Some succeeded, some failed. Still exit non-zero but make LOCK_HELD
        # / LEASE_HELD observable so scripts can retry just the conflicted ones.
        failed_codes = [r.error_code for r in results if not r.refreshed]
        if any(c in ("LOCK_HELD", "LEASE_HELD") for c in failed_codes):
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
                "roost refresh --soon 30m --json"
            ),
        ),
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="HTTP timeout per refresh, seconds.")
    ] = 10.0,
    jitter: Annotated[
        float,
        typer.Option(
            "--jitter",
            help=(
                "Random delay 0..N seconds before each refresh (per profile). "
                "Cron-friendly: spreads concurrent invocations across the "
                "window so N machines don't all hit Anthropic's OAuth endpoint "
                "at the top of the minute. Default 0 (no jitter)."
            ),
            min=0.0,
        ),
    ] = 0.0,
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
        jitter_s=jitter,
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
    jitter: Annotated[float, typer.Option("--jitter", min=0.0)] = 0.0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Alias for `profiles refresh`."""
    profiles_refresh(
        names=names,
        all_profiles=all_profiles,
        expired_only=expired_only,
        soon=soon,
        timeout=timeout,
        jitter=jitter,
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
        reason = outcome.reason or PickFailureReason.NO_PROFILES  # pragma: no cover  -- defensive: failing pick always sets reason
        exit_code = REASON_TO_EXIT.get(reason, EXIT_ERROR)
        message = REASON_MESSAGES.get(reason, "Pick failed.")
        if outcome.earliest_recovery_at:  # pragma: no cover  -- _pick_one is exec-only; recovery-message path covered via standalone pick tests
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
    if after is None:  # pragma: no cover  -- defensive: _reprobe_after_child only returns None for vanished profile
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
            help="Env var name set to the picked profile (default ROOST_PROFILE).",
        ),
    ] = "ROOST_PROFILE",
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
    no_lease: Annotated[
        bool,
        typer.Option(
            "--no-lease",
            help=(
                "Skip auto-leasing the picked profile. By default exec pins the "
                "profile against rotation for the child's lifetime so that a "
                "background probe cannot rotate the refresh_token under a "
                "long-running child. Use --no-lease for short-lived children "
                "where the overhead is unwanted."
            ),
        ),
    ] = False,
    lease_for: Annotated[
        str,
        typer.Option(
            "--lease-for",
            help=(
                "Lease TTL when --no-lease is not set. Accepts '30m', '2h', '90s'. "
                "Defaults to the --timeout value (if set) plus 20%%, else 30m."
            ),
        ),
    ] = "",
) -> None:
    """Pick a profile, run a child command with ROOST_PROFILE set.

    Everything after the last flag is passed to the child. Use `--` to
    disambiguate child flags from roost's own flags:

        roost exec --auto-refresh -- claude --dangerously-skip-permissions <args>

    roost's exit code equals the child's exit code, so scripts can
    treat this as a transparent wrapper. Picks are audited to picks.log
    with rc + duration for post-hoc investigation.
    """
    import shlex

    from .lease import parse_duration as _parse_lease_duration

    chosen_strategy = _validate_strategy(strategy)
    argv = list(ctx.args or [])
    if not argv:
        stderr.print(
            "[red]exec requires a command.[/red] "
            "Example: roost exec -- claude --help"
        )
        raise typer.Exit(EXIT_VALIDATION)

    # Resolve lease duration: explicit --lease-for > timeout+20% > 30m default.
    do_lease = not no_lease
    if lease_for:
        try:
            lease_duration_s = _parse_lease_duration(lease_for)
        except ValueError as exc:
            stderr.print(f"[red]--lease-for: {exc}[/red]")
            raise typer.Exit(EXIT_VALIDATION) from None
    elif timeout is not None:
        lease_duration_s = max(int(timeout * 1.2), 60)
    else:
        lease_duration_s = 30 * 60

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
        lease_profile=do_lease,
        lease_duration_s=lease_duration_s,
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
                lease_profile=do_lease,
                lease_duration_s=lease_duration_s,
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
        roost add personal      # imports it as 'personal'
        claude logout && claude login   # sign in to a different account
        roost add work          # imports the new state as 'work'
        roost status            # both visible, ready to balance

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
        if json_output:  # pragma: no branch  -- both modes covered, branch tracking reports the no-flag case oddly
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
        "  next: roost probe " + name + "  (or `roost status` to see all)"
    )


# ---------------------------------------------------------------------------
# remove — symmetric counterpart to `add`; deletes a profile dir + cache entry
# ---------------------------------------------------------------------------


def _clear_last_pick_if_matches(name: str) -> bool:
    """Drop last-pick.json when it points at `name`. Returns True if cleared.

    Used by `remove` and `rename` so stickiness can't reference a profile that
    no longer exists. Best-effort — a write failure on cleanup must not block
    the primary mutation, since the next pick will discard the stale entry
    naturally.
    """
    from .paths import last_pick_path
    from .pick import read_last_pick

    last = read_last_pick()
    if last is None or last[0] != name:
        return False
    target = last_pick_path()
    try:
        target.unlink()
    except OSError:  # pragma: no cover  -- cleanup is best-effort; missing file fine
        pass
    return True


def _do_remove(
    name: str,
    *,
    json_output: bool,
) -> None:
    """Shared implementation for `remove` (top-level + profiles namespace)."""
    from .discovery import remove_profile_dir

    result = remove_profile_dir(name)
    if not result.ok:
        code = result.error_code or "ERROR"
        msg = result.error_message or f"failed to remove {name}"
        exit_map = {
            "VALIDATION_ERROR": EXIT_VALIDATION,
            "NOT_FOUND": EXIT_NOT_FOUND,
        }
        exit_code = exit_map.get(code, EXIT_ERROR)
        if json_output:
            emit_error_json(code, msg)
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(exit_code)

    cache_dropped = remove_profile(name)
    last_pick_cleared = _clear_last_pick_if_matches(name)

    if json_output:
        emit_json({
            "data": {
                "name": name,
                "path": str(result.path) if result.path else None,
                "cache_dropped": cache_dropped,
                "last_pick_cleared": last_pick_cleared,
            },
            "meta": {"action": "removed"},
        })
        return
    stderr.print(f"[green]removed[/green] profile {name!r} → {result.path}")
    if cache_dropped:
        stderr.print("  cache entry dropped")
    if last_pick_cleared:
        stderr.print("  last-pick reset (was pointing at the removed profile)")


@app.command("remove")
def remove(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile to remove (matches `add <name>`).",
            autocompletion=_complete_profile_names,
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Delete a profile directory + its cache entry.

    Symmetric counterpart to `roost add`. The profile directory under
    ~/.claude-profiles/<name>/ is recursively removed; the health cache entry
    is invalidated; and last-pick.json is cleared if it pointed at this
    profile. The original credentials at ~/.claude/.credentials.json (the
    source `add` copies from) are NOT touched.
    """
    _do_remove(name, json_output=json_output)


@profiles_app.command("remove")
def profiles_remove(
    name: Annotated[
        str, typer.Argument(autocompletion=_complete_profile_names)
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Alias for `remove`."""
    _do_remove(name, json_output=json_output)


# ---------------------------------------------------------------------------
# rename — move a profile dir to a new name; refresh last-pick + cache
# ---------------------------------------------------------------------------


def _do_rename(
    old: str,
    new: str,
    *,
    force: bool,
    json_output: bool,
) -> None:
    """Shared implementation for `rename` (top-level + profiles namespace)."""
    from .discovery import rename_profile_dir

    result = rename_profile_dir(old, new, force=force)
    if not result.ok:
        code = result.error_code or "ERROR"
        msg = result.error_message or f"failed to rename {old} -> {new}"
        exit_map = {
            "VALIDATION_ERROR": EXIT_VALIDATION,
            "NOT_FOUND": EXIT_NOT_FOUND,
            "CONFLICT": EXIT_CONFLICT,
        }
        exit_code = exit_map.get(code, EXIT_ERROR)
        if json_output:
            emit_error_json(code, msg)
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(exit_code)

    # Cache: drop the old entry; the new name will be re-probed naturally on
    # the next status/pick. We don't try to migrate the entry under the new
    # key — the credentials_mtime invariant in is_entry_fresh would correctly
    # detect a "new" profile, but a fresh probe is cleaner than guessing.
    cache_dropped = remove_profile(old)
    last_pick_cleared = _clear_last_pick_if_matches(old)

    if json_output:
        emit_json({
            "data": {
                "old": old,
                "new": new,
                "path": str(result.path) if result.path else None,
                "cache_dropped": cache_dropped,
                "last_pick_cleared": last_pick_cleared,
            },
            "meta": {"action": "renamed"},
        })
        return
    stderr.print(
        f"[green]renamed[/green] {old!r} → {new!r} ({result.path})"
    )
    stderr.print(
        f"  next: roost probe {new}  (re-classify health under the new name)"
    )


@app.command("rename")
def rename(
    old: Annotated[
        str,
        typer.Argument(
            help="Existing profile name.",
            autocompletion=_complete_profile_names,
        ),
    ],
    new: Annotated[
        str,
        typer.Argument(help="New profile name (must match [A-Za-z0-9_-]+)."),
    ],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="If a profile with the new name already exists, replace it.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Rename a profile directory and reset its cached health.

    The credentials file is moved (preserving its mtime so already-cached
    health for OTHER profiles is unaffected). The OLD profile's cache entry
    is dropped; the NEW name will discover fresh on the next probe cycle.
    """
    _do_rename(old, new, force=force, json_output=json_output)


@profiles_app.command("rename")
def profiles_rename(
    old: Annotated[
        str, typer.Argument(autocompletion=_complete_profile_names)
    ],
    new: Annotated[str, typer.Argument()],
    force: Annotated[bool, typer.Option("--force", "-f")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Alias for `rename`."""
    _do_rename(old, new, force=force, json_output=json_output)


# ---------------------------------------------------------------------------
# which — read-only counterpart to `pick`: returns the would-be pick
# ---------------------------------------------------------------------------


def _do_which(
    *,
    strategy: Strategy,
    stickiness: int | None,
    require_ok: bool,
    no_cache: bool,
    max_age: int | None,
    avoid: list[str] | None,
    max_cost: int | None,
    explain: bool,
    json_output: bool,
) -> None:
    """Shared implementation for `which` (top-level + profiles namespace).

    Mirrors `pick`'s decision logic exactly but skips both side effects:
    last-pick.json is NOT updated, picks.log is NOT appended. Useful for
    debugging ("what would pick return right now?") without contaminating
    stickiness state. Probing IS allowed — that's freshness, not state.
    """
    avoid_set = set(avoid or [])
    cache, names = _load_or_probe(refresh=no_cache, max_age=max_age)
    outcome = pick(
        cache,
        names,
        strategy=strategy,
        stickiness_s=stickiness,
        require_ok=require_ok,
        count=1,
        avoid=avoid_set,
        max_cost=max_cost,
    )

    # --explain rendering (mirrors profiles_pick): always populates the
    # explain block on the outcome (cheap), then renders to stderr in text
    # mode or folds into data.explain / error.details.explain in JSON mode.
    explain_payload: dict[str, Any] | None = None
    if explain:
        explain_payload = {
            "discovered": names,
            "chosen": outcome.chosen.name if outcome.chosen else None,
            "strategy_used": (
                outcome.strategy_used.value
                if outcome.strategy_used else strategy.value
            ),
            "rationale": outcome.rationale,
            "excluded_reasons": outcome.excluded_reasons,
            "filter_scores": outcome.filter_scores,
        }
        if not json_output:
            render_pick_explanation(
                discovered_names=names,
                chosen=outcome.chosen.name if outcome.chosen else None,
                strategy=(
                    outcome.strategy_used.value
                    if outcome.strategy_used else strategy.value
                ),
                rationale=outcome.rationale,
                excluded_reasons=outcome.excluded_reasons,
                filter_scores=outcome.filter_scores,
            )

    if not outcome.ok:
        reason = outcome.reason or PickFailureReason.NO_PROFILES
        exit_code = REASON_TO_EXIT.get(reason, EXIT_ERROR)
        message = REASON_MESSAGES.get(reason, "Pick failed.")
        if json_output:
            details: dict[str, Any] = {}
            if explain_payload is not None:
                details["explain"] = explain_payload
            emit_error_json(
                reason.value.upper(), message, details if details else None,
            )
        stderr.print(f"[red]{message}[/red]")
        raise typer.Exit(exit_code)
    assert outcome.chosen is not None
    chosen = outcome.chosen
    if json_output:
        data: dict[str, Any] = {
            "name": chosen.name,
            "health": chosen.health.value,
            "rationale": outcome.rationale,
            "strategy": (outcome.strategy_used or strategy).value,
        }
        if explain_payload is not None:
            data["explain"] = explain_payload
        emit_json({
            "data": data,
            "meta": {"side_effects": False},
        })
        return
    emit_text(chosen.name)


@app.command("which")
def which(
    strategy: Annotated[
        str,
        typer.Option("--strategy", autocompletion=_complete_strategy),
    ] = "sticky",
    stickiness: Annotated[int | None, typer.Option("--stickiness")] = None,
    require_ok: Annotated[bool, typer.Option("--require-ok")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age")] = None,
    avoid: Annotated[
        list[str] | None,
        typer.Option("--avoid", autocompletion=_complete_profile_names),
    ] = None,
    max_cost: Annotated[
        int | None,
        typer.Option(
            "--max-cost",
            help="Skip profiles with monthly overage utilization >= N%%.",
            min=0,
            max=100,
        ),
    ] = None,
    explain: Annotated[
        bool,
        typer.Option(
            "--explain",
            help=(
                "Render the would-be pick decision tree to stderr (or fold "
                "into data.explain in JSON mode). Pairs naturally with the "
                "read-only nature of `which` for debugging."
            ),
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Return the profile that `pick` WOULD choose right now — without side effects.

    Identical decision logic to `pick`, but skips the side effects that `pick`
    writes:
      - last-pick.json is NOT updated (stickiness state preserved)
      - picks.log is NOT appended (no audit-trail noise)

    Useful for debugging ("why is it choosing X?"), pre-flight checks, or
    pairing with `--explain` to understand the decision tree without
    committing to it. Cache may still be probed if stale (that's freshness,
    not state).
    """
    chosen_strategy = _validate_strategy(strategy)
    _do_which(
        strategy=chosen_strategy,
        stickiness=stickiness,
        require_ok=require_ok,
        no_cache=no_cache,
        max_age=max_age,
        avoid=avoid,
        max_cost=max_cost,
        explain=explain,
        json_output=json_output,
    )


@profiles_app.command("which")
def profiles_which(
    strategy: Annotated[
        str, typer.Option("--strategy", autocompletion=_complete_strategy)
    ] = "sticky",
    stickiness: Annotated[int | None, typer.Option("--stickiness")] = None,
    require_ok: Annotated[bool, typer.Option("--require-ok")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age")] = None,
    avoid: Annotated[
        list[str] | None,
        typer.Option("--avoid", autocompletion=_complete_profile_names),
    ] = None,
    max_cost: Annotated[
        int | None, typer.Option("--max-cost", min=0, max=100)
    ] = None,
    explain: Annotated[bool, typer.Option("--explain")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Alias for `which`."""
    chosen_strategy = _validate_strategy(strategy)
    _do_which(
        strategy=chosen_strategy,
        stickiness=stickiness,
        require_ok=require_ok,
        no_cache=no_cache,
        max_age=max_age,
        avoid=avoid,
        max_cost=max_cost,
        explain=explain,
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# snapshot — credential rotation safety
# ---------------------------------------------------------------------------


@app.command("snapshot")
def snapshot_cmd(
    profile: Annotated[
        str,
        typer.Argument(
            help="Profile to snapshot.",
            autocompletion=_complete_profile_names,
        ),
    ],
    out_path: Annotated[
        str,
        typer.Argument(help="Destination path for the snapshot file."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Copy a profile's credentials.json to a stable out-path.

    Unlike reading the live file directly, this makes the intent explicit:
    the snapshot is a point-in-time copy that roost will never touch.
    For full rotation protection during a long workload, combine with
    `roost lease` (or use `roost exec` which auto-leases by default).

    Example (Axiom trial pattern):
        roost snapshot mknv74 /tmp/trial-creds.json
        AXIOM_HOST_CLAUDE_CREDENTIALS=/tmp/trial-creds.json axiom solve ...
    """
    import shutil
    from pathlib import Path

    names_map = {p.name: p for p in discover_profiles()}
    if profile not in names_map:
        if json_output:
            emit_error_json("NOT_FOUND", f"Profile {profile!r} not found")
        else:
            stderr.print(f"[red]profile not found:[/red] {profile!r}")
        raise typer.Exit(EXIT_NOT_FOUND)

    src = Path(names_map[profile].credentials_path)
    dst = Path(out_path)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as exc:
        if json_output:
            emit_error_json("IO_ERROR", f"copy failed: {exc}")
        else:
            stderr.print(f"[red]snapshot failed:[/red] {exc}")
        raise typer.Exit(1) from None

    if json_output:
        emit_json({
            "data": {"profile": profile, "path": str(dst), "source": str(src)},
            "meta": {"profile": profile},
        })
    else:
        stderr.print(
            f"[green]snapshot written:[/green] {dst}\n"
            "[dim]Note: roost will not update this file — it is a point-in-time copy.[/dim]"
        )


# ---------------------------------------------------------------------------
# history — read picks.log
# ---------------------------------------------------------------------------


# _parse_pick_log lives in stats.py for re-use by the `stats` command. Kept as
# a thin alias here so the original symbol stays importable for backwards-compat
# with any external callers.
def _parse_pick_log(path: Any) -> list[dict[str, Any]]:
    """Backwards-compat alias for stats.parse_pick_log."""
    from pathlib import Path as _Path

    from .stats import parse_pick_log

    return parse_pick_log(_Path(str(path)))


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
    from datetime import UTC, timedelta
    from datetime import datetime as _dt

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
                "[green]EXEC[/green]" if rc == "0" else "[red]EXEC[/red]"
            )
        else:
            detail = " ".join(f"{k}={v}" for k, v in e["details"].items())
            action_style = e["action"]
        table.add_row(when, e["profile"], action_style, detail)

    stderr.print(table)
    emit_text(f"{len(final)} of {len(all_entries)} entries")


# ---------------------------------------------------------------------------
# stats — aggregate over picks.log
# ---------------------------------------------------------------------------


@app.command("stats")
def stats(
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help=(
                "Aggregate only entries within this window: '30m', '1h', '2d', "
                "'1w', or seconds. Default: all-time."
            ),
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Filter to one profile only.",
            autocompletion=_complete_profile_names,
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Aggregate picks.log into pick + exec counts and exec-duration percentiles.

    Reads from `<config>/picks.log` (the audit trail every `pick` and `exec`
    invocation appends to). Useful for "what's been happening lately?"
    without grepping a tab-separated file by hand.

    Output (text mode) is a compact summary on stdout; JSON mode emits
    `{data: ..., meta: ...}` for scripting. No mutation — purely read-side.
    """
    from datetime import UTC, timedelta
    from datetime import datetime as _dt

    from .paths import pick_log_path
    from .stats import aggregate_stats, parse_pick_log

    cutoff: _dt | None = None
    if since is not None:
        secs = _parse_duration(since)
        if secs is None:
            if json_output:
                emit_error_json(
                    "VALIDATION_ERROR",
                    f"invalid --since value: {since!r}",
                )
            stderr.print(
                f"[red]invalid --since value:[/red] {since!r}"
            )
            raise typer.Exit(EXIT_VALIDATION)
        cutoff = _dt.now(UTC) - timedelta(seconds=secs)

    log_path = pick_log_path()
    entries = parse_pick_log(log_path)
    if cutoff is not None:
        entries = [e for e in entries if e["timestamp"] >= cutoff]
    if profile:
        entries = [e for e in entries if e["profile"] == profile]

    report = aggregate_stats(entries)

    if json_output:
        emit_json({
            "data": report.to_json(),
            "meta": {
                "log_path": str(log_path),
                "filters": {"since": since, "profile": profile},
            },
        })
        return

    if report.pick_total == 0 and report.exec_total == 0:
        if not log_path.is_file():
            stderr.print("[yellow]No picks.log yet.[/yellow]")
        else:
            stderr.print("[yellow]No matching entries.[/yellow]")
        return

    stderr.print(f"[bold]picks.log stats[/bold]  ({log_path})")
    if report.window_start and report.window_end:
        stderr.print(
            f"  window: {report.window_start.isoformat()} → "
            f"{report.window_end.isoformat()}"
        )
    if report.pick_total:
        stderr.print(f"\n[bold]Picks[/bold]  total={report.pick_total}")
        for p, n in report.pick_by_profile.most_common():
            stderr.print(f"  {p:<20} {n}")
        stderr.print("  by strategy:")
        for s, n in report.pick_by_strategy.most_common():
            stderr.print(f"    {s:<18} {n}")
    if report.exec_total:
        stderr.print(f"\n[bold]Execs[/bold]  total={report.exec_total}")
        for p, n in report.exec_by_profile.most_common():
            stderr.print(f"  {p:<20} {n}")
        stderr.print("  by rc:")
        for rc, n in report.exec_by_rc.most_common():
            colour = "[green]" if rc == "0" else "[red]"
            stderr.print(f"    {colour}rc={rc}[/]  {n}")
        if report.exec_p50_ms is not None:
            stderr.print(
                f"  duration: p50={report.exec_p50_ms:.0f}ms "
                f"p95={report.exec_p95_ms:.0f}ms"
            )
        if report.exec_failure_rate is not None:
            stderr.print(
                f"  failure rate: {report.exec_failure_rate * 100:.1f}%"
            )


# ---------------------------------------------------------------------------
# report — aggregate over usage-log.ndjson (per-probe history)
# ---------------------------------------------------------------------------


@app.command("report")
def report(
    metric: Annotated[
        str,
        typer.Option(
            "--metric",
            help=(
                "Which metric to aggregate. Options: weekly_pct, session_pct, "
                "fable_pct, sonnet_pct, opus_pct, overage_pct, spend_pct."
            ),
        ),
    ] = "weekly_pct",
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Aggregate only records within this window (default: all).",
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Filter to one profile only.",
            autocompletion=_complete_profile_names,
        ),
    ] = None,
    sparkline_flag: Annotated[
        bool,
        typer.Option(
            "--sparkline",
            help="Include a Unicode sparkline of the time-series per profile.",
        ),
    ] = False,
    project: Annotated[
        bool,
        typer.Option(
            "--project",
            help=(
                "Show a linear burn-rate projection for when each profile's "
                "metric will reach 100%%. Best-effort hint, not a guarantee."
            ),
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Aggregate `<config>/usage-log.ndjson` into per-profile metric summaries.

    Requires usage logging to be enabled (off by default — see
    `roost config usage-log on`). Reports min/max/avg/latest per profile
    for the chosen metric, optionally rendering a Unicode sparkline of the
    time-series and a linear burn-rate projection.
    """
    from datetime import UTC, timedelta
    from datetime import datetime as _dt

    from . import usage_log
    from .stats import project_exhaustion, sparkline, summarise_metric

    valid_metrics = {
        "weekly_pct", "session_pct", "fable_pct", "sonnet_pct", "opus_pct",
        "overage_pct", "spend_pct",
    }
    if metric not in valid_metrics:
        msg = (
            f"invalid --metric: {metric!r}. "
            f"Pick one of: {', '.join(sorted(valid_metrics))}"
        )
        if json_output:
            emit_error_json("VALIDATION_ERROR", msg)
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(EXIT_VALIDATION)

    cutoff: _dt | None = None
    if since is not None:
        secs = _parse_duration(since)
        if secs is None:
            msg = f"invalid --since: {since!r}"
            if json_output:
                emit_error_json("VALIDATION_ERROR", msg)
            stderr.print(f"[red]{msg}[/red]")
            raise typer.Exit(EXIT_VALIDATION)
        cutoff = _dt.now(UTC) - timedelta(seconds=secs)

    if not usage_log.is_enabled():
        # The log file may still exist from a prior enabled period — so don't
        # block, just warn.
        stderr.print(
            "[yellow]usage logging is off[/yellow] — "
            "enable with `roost config usage-log on` "
            "for fresh data going forward."
        )

    records = list(usage_log.iter_records(since=cutoff, profile=profile))
    summaries = summarise_metric(records, metric=metric)

    payload_data: list[dict[str, Any]] = []
    for s in summaries:
        d = s.to_json()
        if sparkline_flag:
            d["sparkline"] = sparkline([v for _, v in s.series])
        if project:
            secs = project_exhaustion(s)
            d["seconds_to_100"] = secs
        payload_data.append(d)

    if json_output:
        emit_json({
            "data": payload_data,
            "meta": {
                "metric": metric,
                "records_total": len(records),
                "filters": {
                    "since": since,
                    "profile": profile,
                },
                "log_path": str(usage_log.usage_log_path()),
            },
        })
        return

    log_path = usage_log.usage_log_path()
    if not summaries:
        if not log_path.is_file():
            stderr.print(f"[yellow]No usage log yet.[/yellow] ({log_path})")
        else:
            stderr.print("[yellow]No matching records.[/yellow]")
        return

    from rich.table import Table

    table = Table(
        title=f"Usage report — {metric}",
        title_justify="left",
        show_lines=False,
        expand=False,
    )
    table.add_column("Profile", no_wrap=True)
    table.add_column("Samples", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("Avg", justify="right")
    table.add_column("Latest", justify="right")
    if sparkline_flag:
        table.add_column("Trend", no_wrap=True)
    if project:
        table.add_column("ETA → 100%", no_wrap=True)

    for s in summaries:
        row = [
            s.profile,
            str(s.samples),
            f"{s.minimum:.1f}" if s.minimum is not None else "—",
            f"{s.maximum:.1f}" if s.maximum is not None else "—",
            f"{s.average:.1f}" if s.average is not None else "—",
            f"{s.latest:.1f}" if s.latest is not None else "—",
        ]
        if sparkline_flag:
            row.append(sparkline([v for _, v in s.series]))
        if project:
            secs = project_exhaustion(s)
            if secs is None:
                row.append("—")
            else:
                row.append(_humanize_elapsed(secs).replace(" ago", ""))
        table.add_row(*row)
    stderr.print(table)
    emit_text(f"{len(records)} records across {len(summaries)} profile(s)")


# ---------------------------------------------------------------------------
# config — opt-in toggles (currently: usage-log on/off/status)
# ---------------------------------------------------------------------------

config_app = typer.Typer(help="Configuration toggles (opt-in features).")
app.add_typer(config_app, name="config")


@config_app.command("usage-log")
def config_usage_log(
    state: Annotated[
        str,
        typer.Argument(
            help="One of: on, off, status.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable, disable, or check the per-probe usage log.

    Disabled by default for privacy. When enabled, every successful probe
    appends one JSON record to `<config>/usage-log.ndjson`, which `report`
    reads back. The toggle persists via a marker file at
    `<config>/usage-log.enabled` so daemons/cron survive across restarts.

    The CLAUDE_LB_USAGE_LOG=1 env var also enables the log without writing a
    marker — useful for one-off scripts or container envs.
    """
    from . import usage_log

    state_lower = state.strip().lower()
    if state_lower == "status":
        currently = usage_log.is_enabled()
        marker = usage_log.usage_log_marker_path()
        env_set = os.environ.get(usage_log.ENV_VAR, "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if json_output:
            emit_json({
                "data": {
                    "enabled": currently,
                    "marker_present": marker.is_file(),
                    "env_var_set": env_set,
                    "log_path": str(usage_log.usage_log_path()),
                    "marker_path": str(marker),
                }
            })
            return
        stderr.print(
            f"usage-log: [{'green' if currently else 'yellow'}]"
            f"{'enabled' if currently else 'disabled'}[/]"
        )
        stderr.print(f"  marker: {'present' if marker.is_file() else 'absent'} ({marker})")
        stderr.print(f"  env:    {'set' if env_set else 'unset'} ({usage_log.ENV_VAR})")
        stderr.print(f"  log:    {usage_log.usage_log_path()}")
        return

    if state_lower == "on":
        path = usage_log.enable_marker()
        if json_output:
            emit_json({"data": {"enabled": True, "marker_path": str(path)}})
            return
        stderr.print(f"[green]usage-log enabled[/green] → marker at {path}")
        stderr.print(
            "  next probe will start appending to "
            f"{usage_log.usage_log_path()}"
        )
        return

    if state_lower == "off":
        removed = usage_log.disable_marker()
        if json_output:
            emit_json({"data": {"enabled": False, "marker_removed": removed}})
            return
        if removed:
            stderr.print("[yellow]usage-log disabled[/yellow] (marker removed)")
        else:
            stderr.print("[yellow]usage-log was not enabled[/yellow] (no marker present)")
        return

    msg = f"invalid state: {state!r}. Use one of: on, off, status."
    if json_output:
        emit_error_json("VALIDATION_ERROR", msg)
    stderr.print(f"[red]{msg}[/red]")
    raise typer.Exit(EXIT_VALIDATION)


# ---------------------------------------------------------------------------
# shellinit — emit shell function templates for `claude` wrapper integration
# ---------------------------------------------------------------------------


@app.command("shellinit")
def shellinit(
    shell: Annotated[
        str | None,
        typer.Option(
            "--shell",
            help=(
                "Override the auto-detected shell. One of: bash, zsh, fish, "
                "pwsh. Default: detect from $SHELL (POSIX) or $PSModulePath "
                "(Windows)."
            ),
        ),
    ] = None,
) -> None:
    """Emit a shell function definition that wraps `claude` through `roost exec`.

    Designed to be evaluated into the user's shell rc:

        # bash/zsh
        eval "$(roost shellinit)"

        # fish
        roost shellinit | source

        # PowerShell
        roost shellinit | Out-String | Invoke-Expression

    Once installed, every `claude` invocation routes through `roost exec
    --auto-refresh -- claude ...` — so profile selection, auth refresh, and
    audit logging happen transparently. Remove the function from your rc to
    revert.
    """
    from .shell_init import SUPPORTED_SHELLS, detect_shell, template_for

    target = (shell or detect_shell()).strip().lower()
    if target not in SUPPORTED_SHELLS:
        msg = (
            f"unknown shell: {target!r}. "
            f"Supported: {', '.join(SUPPORTED_SHELLS)}"
        )
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(EXIT_VALIDATION)
    template = template_for(target)
    sys.stdout.write(template)
    if not template.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# trace — verbose probe with full request/response + classifier reasoning
# ---------------------------------------------------------------------------


def _redact_token(token: str) -> str:
    """Show only the last 4 characters of a token; everything else as `***`."""
    if not token:
        return "***"
    if len(token) <= 4:
        return "***" + token
    return "Bearer ***..." + token[-4:]


@app.command("trace")
def trace(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile to trace.",
            autocompletion=_complete_profile_names,
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verbose-probe one profile and dump request/response + classifier reasoning.

    Like `roost probe --raw`, but also runs the classifier and explains its
    decision. The bearer token is redacted to its last 4 characters in both
    text and JSON output so the trace is shareable in bug reports.
    """
    from .probe import ANTHROPIC_BETA, ANTHROPIC_VERSION, API_URL, probe_raw_many_sync
    from .taxonomy import classify

    profile = get_profile(name)
    if profile is None:
        if json_output:
            emit_error_json("NOT_FOUND", f"No such profile: {name}")
        stderr.print(f"[red]No such profile:[/red] {name}")
        raise typer.Exit(EXIT_NOT_FOUND)

    raw_results = probe_raw_many_sync([profile])
    if not raw_results:
        msg = "trace produced no result"
        if json_output:
            emit_error_json("ERROR", msg)
        stderr.print(f"[red]{msg}[/red]")
        raise typer.Exit(EXIT_ERROR)

    _name, status_code, body, headers = raw_results[0]

    # Reconstruct the request envelope for display.
    req_headers = {
        "Authorization": _redact_token(profile.access_token),
        "anthropic-beta": ANTHROPIC_BETA,
        "anthropic-version": ANTHROPIC_VERSION,
    }

    # Run classifier explicitly to get the same decision the cache write would.
    from .models import ClassificationResult

    # We don't have the original ProbeInput; build one from the raw response.
    from .probe import (  # type: ignore[attr-defined]
        ProbeInput,
    )

    probe_input = ProbeInput(
        status_code=status_code,
        body=body if isinstance(body, dict) else None,
        headers=headers,
        exception_kind=None,
    )
    classification: ClassificationResult = classify(probe_input)

    if json_output:
        emit_json({
            "data": {
                "profile": name,
                "request": {
                    "method": "GET",
                    "url": API_URL,
                    "headers": req_headers,
                },
                "response": {
                    "status_code": status_code,
                    "headers": headers,
                    "body": body,
                },
                "classification": {
                    "health": classification.health.value,
                    "error": (
                        classification.error.model_dump()
                        if classification.error else None
                    ),
                    "retry_after_s": classification.retry_after_s,
                    "session_reset_at": (
                        classification.session_reset_at.isoformat().replace("+00:00", "Z")
                        if classification.session_reset_at else None
                    ),
                    "weekly_reset_at": (
                        classification.weekly_reset_at.isoformat().replace("+00:00", "Z")
                        if classification.weekly_reset_at else None
                    ),
                },
            }
        })
        return

    from rich.panel import Panel

    stderr.print(Panel(
        f"GET {API_URL}\n"
        + "\n".join(f"  {k}: {v}" for k, v in req_headers.items()),
        title=f"Request — {name}",
        title_align="left",
    ))
    stderr.print(Panel(
        f"Status: {status_code}\n"
        + "\n".join(f"  {k}: {v}" for k, v in (headers or {}).items())
        + ("\n\nBody:\n" + json.dumps(body, indent=2) if body is not None else ""),
        title="Response",
        title_align="left",
    ))
    err_line = (
        f"  error: {classification.error.type} — {classification.error.message}"
        if classification.error else ""
    )
    stderr.print(Panel(
        f"  health: {classification.health.value}"
        + (("\n" + err_line) if err_line else ""),
        title="Classifier",
        title_align="left",
    ))


# ---------------------------------------------------------------------------
# top — live-refreshing TUI of the status table
# ---------------------------------------------------------------------------


@app.command("top")
def top(
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            help="Refresh interval in seconds.",
            min=0.1,
        ),
    ] = 2.0,
    iterations: Annotated[
        int | None,
        typer.Option(
            "--iterations",
            help=(
                "(Test seam) Stop after N frames. Default: infinite (Ctrl+C to "
                "exit)."
            ),
            hidden=True,
        ),
    ] = None,
) -> None:
    """Live-refreshing status table. Ctrl+C to exit.

    Reads the cache on every tick; if a profile's entry is stale, the next
    tick re-probes it (same logic as `roost status`). For one-shot
    snapshots, use `roost status` — `top` is for "leave on a side
    monitor" workflows, especially during incidents.
    """
    from .top import run_live

    def _refresh() -> tuple[HealthCache, list[str]]:
        return _load_or_probe(refresh=False, max_age=None)

    run_live(
        refresh_fn=_refresh,
        interval_s=interval,
        max_iterations=iterations,
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
    t = Term()
    failed = [c.name for c in report.checks if not c.passed]
    warned = [c for c in report.checks if c.passed and c.extra.get("warning")]
    lines: list[str] = [
        t.panel_open("roost", "roost · doctor", indicator=f"v{report.version}"),
        t.vert(),
    ]
    for check in report.checks:
        # WARN: passed but flagged a non-fatal concern (e.g. profiles without
        # refresh tokens — valid setup, just won't auto-heal).
        if check.passed and check.extra.get("warning"):
            state = "warn"
        elif check.passed:
            state = "ok"
        else:
            state = "fail"
        lines.append(t.check_row(state, check.name, check.detail))
        # A failure is exactly when the operator needs the full remediation
        # text — surface it untruncated as a red alert sub-row rather than
        # losing it to the leaf-row width budget.
        if state == "fail":
            lines.append(t.alert_panel("critical", check.detail))
    lines.append(t.vert())
    if failed:
        footer = t.health("critical", f"{len(failed)} failed")
    elif warned:
        footer = t.health("warning", f"{len(warned)} warning(s)")
    else:
        footer = t.health("healthy", "all clear")
    lines.append(t.panel_close(left_text="roost doctor --json", right_text=footer))
    emit_panel(lines)
    if failed:
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
            if result.stdout:  # pragma: no branch  -- both stdout-present and stdout-empty paths covered
                stderr.print(result.stdout)
            raise typer.Exit(EXIT_ERROR)
        stderr.print(
            f"[green]Update applied[/green] — "
            f"pulled={result.pulled}, reinstalled={result.reinstalled}"
        )
        if result.stdout:  # pragma: no branch  -- happy-path with/without stdout both tested
            stderr.print(result.stdout)
        return

    status = check_for_update()
    if json_output:
        emit_json(status_to_dict(status))
        return
    stderr.print(f"[bold]roost[/bold] {status.current_version}")
    if status.install_dir:  # pragma: no branch  -- check_for_update almost always finds the install dir
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
