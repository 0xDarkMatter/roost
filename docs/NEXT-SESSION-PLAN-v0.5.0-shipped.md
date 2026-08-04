# v0.5.0 plan + handoff from 2026-04-25 session

Self-note. Picking this up cold in a future session — everything you
need is in this doc. Current state: v0.4.1 on main, commit `084b630`.

---

## Session context (what just happened)

### Commits landed this session

1. `c928a53` — refactor: split `Resets (S/W)` into separate `Session in` / `Weekly in` columns (user feedback: combined cell was hard to scan)
2. `11e305d` — feat: Plan column + dual session/weekly resets in status table (user asked for plan column; surfaces `claudeAiOauth.subscriptionType`)
3. `084b630` — **fix(refresh): graceful MISSING_DEPENDENCY on stale install + doctor import check + update --apply** — THIS IS THE BIG ONE

### The Axiom bug report (pigeon #47, resolved)

Axiom reported `roost refresh --expired` crashing with `ModuleNotFoundError: No module named 'filelock'` on their v0.3.0 install. Root cause: editable install drift — v0.4.0 added `filelock` dep + import, but `uv tool install --editable` done against v0.3.0 doesn't re-sync the tool venv when `pyproject.toml` gets new deps. Source changes are picked up live; dep changes are NOT.

Three-part fix shipped as v0.4.1:

1. **Lazy-import `filelock` inside `refresh_profile`** — catch `ImportError`, return `RefreshResult(error_code="MISSING_DEPENDENCY", error_message=<reinstall command>)`. Every other subcommand keeps working (graceful per-subcommand degradation).
2. **`roost doctor` runs a `subcommand_imports` check** — loads every `claude_lb.*` module, reports drift explicitly. Catches the failure *before* the user trips over it.
3. **`roost update --apply`** — actually runs `git pull --ff-only` + `uv tool install --reinstall --editable <dir>` now. Was status-only before. `--no-pull` flag skips git for dep-only refreshes.

Dogfooded the fix by self-upgrading with `roost update --apply --no-pull`. Axiom notified via pigeon reply.

### Key session finding worth remembering

**Editable install drift is a real recurring failure mode, not a one-off.** Any dep added after initial install → stale tool venv → cryptic runtime crash. The fix is structural (lazy import + doctor check + self-heal command), not just "this bug". Future feature additions should keep this pattern: lazy-import optional heavy deps, verify in doctor, self-heal via update.

### Live fleet state (captured 2026-04-25)

For context when eyeballing behaviour later:

| Profile    | Plan | Session% | Weekly% | Sonnet% | Overage   | Reset window |
|------------|------|----------|---------|---------|-----------|--------------|
| account-b | max  | 97-99%   | 22-23%  | 3%      | 100% AUD  | ~22m (hot!)  |
| account-c     | max  | 17%      | 8%      | 1%      | 99% USD   | ~1h 52m      |
| account-a     | max  | 11%      | 6%      | 0%      | 11% AUD   | ~2m (freshest) |

account-b is typically running the hottest; account-a has the most headroom. account-b has already burned through its monthly overage budget (100% AUD); account-c is about to (99% USD). Use this fleet as the testing/demo ground — variety of states without needing mocks.

### Tier 2/3 items I recommended but deferred

For the next "what could we add?" conversation:

- **`roost watch`** — live-updating status table (TUI), Rich-based, ~30 lines
- **`~/.config/roost/usage-log.ndjson` + `roost history`** — append-on-probe, enables burn-rate projection and monthly reports
- **`refresh --soon 30m`** — anticipatory refresh for tokens expiring within N
- **`pick --max-cost <amount>`** — skip profiles near monthly overage cap (we have the data)
- **`pick --format path|token`** — return creds dir or raw token instead of name
- **Shell completion** — Typer supports free, we just disabled it (one line to re-enable)

Skipped (feature creep / premature): MCP server, PyPI publish, profile groups/tags, telemetry.

---

## North-star idiom (v0.5.0 target)

```bash
roost exec --count 4 --auto-refresh --strategy least-used \
  -- axiom launch-parcel "$@"
```

Picks 4 healthy profiles ordered by headroom, inline-refreshes any with expired tokens, dispatches `axiom launch-parcel` once per profile with `$AXIOM_CLAUDE_PROFILE` set. Three features, ~30 lines of bash collapse into one.

---

## Implementation order (why this order)

1. **`pick --auto-refresh`** first — smallest, unblocks the others. `exec` will need auto-refresh behaviour baked in; easier to build once as a reusable helper than to duplicate.
2. **`pick --count N`** second — enables multi-profile workflows. Touches the pick output contract (single line → newline-separated list when N>1), so CLI tests need updating.
3. **`exec <cmd...>`** last — composes both. New subcommand, bigger surface area, more tests.

Budget: 90m for auto-refresh + tests, 60m for count + tests, 120m for exec + tests. Plus 30m docs + commit messages. ~5h total if the session is uninterrupted.

---

## Feature 1: `roost pick --auto-refresh`

### Contract

```bash
roost pick --auto-refresh
# stdout: single profile name, newline-terminated (unchanged)
# exit 0 on success
# exit 2 if auto-refresh fails AND no other healthy profile exists
# exit 7 if auto-refresh loses a lock race AND no fallback available
```

### Behaviour

When the chosen profile is `auth_expired`, inline-refresh it before returning the name:

1. Run `pick()` as normal against the current cache.
2. If `outcome.chosen.health` is `AUTH_EXPIRED` AND the profile has `refresh_token_present`, call `refresh_profile` for that one profile.
3. On refresh success: invalidate cache entry, re-probe just this one, re-run `pick`, return.
4. On refresh failure (`LOCK_HELD`, `REFRESH_REJECTED`, etc.): emit a stderr warning, filter the expired profile out of the candidate pool, re-run `pick` against the remaining candidates.
5. If no other candidates: propagate the original pick failure.

### Implementation sketch

In `src/claude_lb/cli.py::profiles_pick`, new flag:

```python
auto_refresh: Annotated[
    bool,
    typer.Option(
        "--auto-refresh",
        help=(
            "If the chosen profile is auth_expired, refresh it inline "
            "before returning. Falls through to next candidate if refresh fails."
        ),
    ),
] = False,
```

After the initial `pick()` call:

```python
if auto_refresh and outcome.ok and outcome.chosen.health is Health.AUTH_EXPIRED:
    outcome = _attempt_auto_refresh(outcome, cache, names, strategy)
```

New helper in `cli.py` (kept here rather than in `pick.py` because it depends on refresh + probe + cache; `pick.py` should stay pure-algorithm):

```python
def _attempt_auto_refresh(
    outcome: PickOutcome,
    cache: HealthCache,
    names: list[str],
    strategy: Strategy,
) -> PickOutcome:
    """Refresh the chosen AUTH_EXPIRED profile inline, re-probe, re-pick.
    On refresh failure, filter the profile out and re-pick."""
    chosen = outcome.chosen
    profile = get_profile(chosen.name)
    if profile is None or not profile.refresh_token_present:
        return outcome  # can't help, let caller see auth_expired
    results = refresh_many_sync([profile])
    if not results or not results[0].refreshed:
        # Refresh failed. Invalidate from cache so re-pick excludes it.
        remove_profile(chosen.name)
        return pick(
            load_cache(),
            [n for n in names if n != chosen.name],
            strategy=strategy,
        )
    # Refresh succeeded. Re-probe just this profile to refresh its health
    # entry (cache entry was invalidated by refresh already).
    probe_many_sync([profile])
    return pick(load_cache(), names, strategy=strategy)
```

### Tests to add

- `test_auto_refresh_happy_path` — pre-populate cache with AUTH_EXPIRED, stub `refresh_many_sync` + `probe_many_sync`, assert pick returns the refreshed profile name.
- `test_auto_refresh_falls_through_on_refresh_failure` — same setup but stub refresh to return REFRESH_REJECTED; assert pick falls through to another healthy candidate.
- `test_auto_refresh_no_refresh_token_is_noop` — AUTH_EXPIRED + no `refresh_token_present` → return original outcome unchanged.
- `test_auto_refresh_last_candidate_falls_through_to_exit_2` — only one profile, expired, refresh fails → exit 2 (AUTH_REQUIRED).

### Gotchas

- Recursive `pick()` could loop forever if not careful. Pass a shrunken `names` list rather than calling with the full list again.
- Must preserve the caller's original `strategy`/`stickiness` when re-picking. Simplest: hoist into a local and thread through.
- The `--warn-at` warning path runs AFTER auto-refresh — check the refreshed profile's usage against the threshold, not the original.

---

## Feature 2: `roost pick --count N`

### Contract

```bash
roost pick --count 3
# stdout:
# account-a
# account-c
# account-b
# One profile name per line, no trailing blank line.
# Exit 0 if >= 1 candidate returned (even if fewer than N requested).
# Exit 9 (UNAVAILABLE) if 0 candidates.
```

JSON variant:

```bash
roost pick --count 3 --json
# { "data": [ { "name": ..., "health": ..., "rationale": ... }, ... ],
#   "meta": { "count": 3, "requested": 3, "strategy": "least-used" } }
```

### Behaviour

1. Run the filter ladder as normal.
2. Sort by strategy (least-used, weighted, etc.).
3. Return up to N candidates. If fewer than N pass the ladder, return what we have — caller decides whether partial fulfilment is OK.
4. `--require-ok` combined with `--count` still filters to OK-only.
5. Stickiness is IGNORED when count > 1 — sticky is a "keep returning the same profile" semantic that doesn't compose with multi-pick. Document this clearly in help.
6. Each picked profile is appended to `picks.log`.
7. `write_last_pick` is called for the FIRST profile only (the "primary" pick), so subsequent single-picks without `--count` honour stickiness against the primary.

### Export behaviour

`--export --count N` would be ambiguous (can't export N vars with the same name). Reject with `EXIT_VALIDATION`, pointing to `--json`. Alternative (multiple enumerated vars) is cute but error-prone; skip.

### Implementation sketch

Extend `PickOutcome`:

```python
@dataclass
class PickOutcome:
    chosen: ProfileHealth | None = None
    chosen_many: list[ProfileHealth] = field(default_factory=list)  # NEW
    strategy_used: Strategy | None = None
    reason: PickFailureReason | None = None
    earliest_recovery_at: datetime | None = None
    rationale: str = ""
```

Extend `pick.pick()`:

```python
def pick(
    cache: HealthCache,
    discovered_names: list[str],
    *,
    count: int = 1,
    ...
) -> PickOutcome:
    ...
    # After sort:
    if count == 1:
        return PickOutcome(chosen=ordered[0], chosen_many=[ordered[0]], ...)
    top = ordered[:count]
    return PickOutcome(chosen=top[0], chosen_many=top, ...)
```

CLI layer emits `chosen_many` in order.

### Tests to add

- `test_pick_count_returns_n_candidates` — cache with 5 profiles, `--count 3`, assert 3 names on stdout in expected order.
- `test_pick_count_returns_fewer_when_candidates_limited` — 2 healthy, `--count 5` → returns 2, exit 0.
- `test_pick_count_zero_candidates_exits_9` — all auth_dead, `--count 3` → stdout empty, exit 9.
- `test_pick_count_disables_stickiness` — last-pick set to 'a' within window, `--count 2` with least-used should return top-2-by-least-used regardless of last-pick.
- `test_pick_count_with_export_rejected` — `--count 2 --export` → EXIT_VALIDATION with message mentioning `--json`.
- `test_pick_count_json_envelope` — `--count 2 --json` yields data array of length 2 with meta.count=2.
- `test_pick_count_respects_strategy` — `--count 3 --strategy round-robin` rotates past last-pick.

### Gotchas

- `append_pick_log` should be called for each profile returned, not just the first. Otherwise we lose the audit trail for parallel dispatch.
- Existing picks.log rotation policy still applies.
- Backwards compat: existing `--json` output without `--count` is a single-object `{"data": {...}}`. With `--count` it becomes `{"data": [...]}`. This IS a shape change. Go with: single-object for `--count 1` (implicit or explicit), array for `--count > 1`. Cleaner for scripts that don't use `--count`. Test both shapes explicitly.

---

## Feature 3: `roost exec <command...>`

### Contract

```bash
roost exec claude --dangerously-skip-permissions "write function"
# → picks a profile
# → sets AXIOM_CLAUDE_PROFILE (or custom via --var-name)
# → execs the command
# → on exit, logs { profile, argv0, duration, exit_code } to picks.log
# → roost's own exit code = child's exit code
```

Flags:

- `--auto-refresh` (inherited from pick)
- `--strategy` / `--stickiness` / `--require-ok` / `--warn-at` (all from pick)
- `--var-name` (default AXIOM_CLAUDE_PROFILE)
- `--retry-on-429` (default 1) — if the child exits with a signal mapping to rate-limit (non-zero + stderr match), re-pick and re-run
- `--timeout <seconds>` (default: none) — kill the child after N seconds
- `--dry-run` — print what would be executed, don't run

Everything after `--` is the command + args (standard convention). Without `--`, unknown options belong to argv.

### Behaviour

1. Run pick logic (with auto-refresh if requested).
2. If no healthy profile: propagate exit code from pick.
3. Build env: `os.environ` + `{var_name: profile.name}`.
4. `--dry-run`: `emit_text(f"{var_name}={profile.name} {command_repr}")` and exit 0.
5. Otherwise: `subprocess.run(argv, env=env)`. On Windows, subprocess.run is cleaner than `os.execvpe`.
6. Measure wall-clock duration. Capture child rc.
7. Append to picks.log: `{ts}\t{profile}\tEXEC\trc={rc}\tdur={ms}`.
8. If rc is non-zero AND matches a rate-limit signal AND retry budget left: re-pick (excluding just-used profile), re-run once.
9. Return child's final rc as roost's exit code.

### Rate-limit detection heuristic (tricky)

Options:

- Parse stderr for 429 / "rate limit" / "Please wait" patterns.
- Check rc — claude CLI uses exit 1 for most errors, so rc alone isn't reliable.
- **Re-probe the profile after the child exits**; if it shifted to RATE_LIMITED or SESSION_LIMIT, infer the child hit that.

Ship with option 3 (re-probe, infer state change). Document that retry-on-429 is best-effort, not guaranteed.

### Security considerations

- NEVER log full argv if it contains tokens or secrets.
- Default: log only `argv[0]` (low-risk, preserves what-command-ran).
- `--verbose`: log full argv (escape hatch).
- `--var-name` value lands in env; shell expansion avoided because Typer passes it as a string literal.

### Implementation sketch

New module `src/claude_lb/exec_cmd.py` (not `exec.py` — shadows builtin):

```python
async def run_exec(
    command: list[str],
    *,
    strategy: Strategy,
    stickiness_s: int | None,
    auto_refresh: bool,
    var_name: str,
    retry_on_429: int,
    timeout: float | None,
    dry_run: bool,
    warn_at: int | None,
    verbose: bool,
) -> int:
    """Pick a profile, exec the command, handle retry, return child rc."""
    ...
```

CLI wiring:

```python
@app.command("exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def exec_cmd(
    ctx: typer.Context,
    strategy: Annotated[str, typer.Option("--strategy")] = "sticky",
    ...,
) -> None:
    command = ctx.args  # everything after `roost exec`
    if not command:
        stderr.print("[red]exec requires a command.[/red]")
        raise typer.Exit(EXIT_VALIDATION)
    rc = asyncio.run(run_exec(command, ...))
    raise typer.Exit(rc)
```

### Tests to add

- `test_exec_happy_path` — profile_factory, stub subprocess.run to return rc=0, assert roost exits 0 and `var_name` ended up in child env.
- `test_exec_no_healthy_profile_exits_9` — no profiles, assert exit 9.
- `test_exec_dry_run_prints_without_running` — `--dry-run`, assert no subprocess call, stdout contains var assignment.
- `test_exec_propagates_child_exit_code` — stub child rc=42, assert roost exits 42.
- `test_exec_retry_on_rate_limit` — first invocation re-probes profile as RATE_LIMITED, second picks different profile and succeeds. Assert picks.log has two entries with two different profiles.
- `test_exec_timeout_kills_child` — stub child that sleeps > timeout, assert SIGTERM (or equivalent) and rc != 0.
- `test_exec_logs_argv0_not_full_argv_by_default` — picks.log contains "claude" but NOT the secret-looking arg.

### Gotchas

- Windows doesn't support `os.execvpe` cleanly; stick with subprocess.run.
- stdin/stdout/stderr must be inherited (not captured) so the child can be interactive. `subprocess.run` with `stdin=None, stdout=None, stderr=None` does this.
- Ctrl+C during child execution should forward to the child, not be swallowed by roost. `subprocess.run` handles this on both platforms.
- picks.log rotation happens inside `append_pick_log`; reuse it.

---

## Cross-cutting work

### SPEC.md

- §2 Command Architecture — add `exec` as a new top-level command (it's not a `profiles-*` subcommand; it's an action on a profile).
- §4 Exit Codes — no new codes; EXIT_VALIDATION for `--count`+`--export`, existing codes cover the rest.
- §9 Pick Algorithm — add "Multi-pick" subsection documenting `--count` behaviour (ladder → sort → top N, stickiness ignored, picks.log gets N entries).

### README.md

- New "Scripting" section showing the north-star idiom.
- Update "Picking" section with `--auto-refresh` + `--count` examples.
- New "Running commands" section for `exec`.

### CHANGELOG.md

Single v0.5.0 entry covering all three features.

### AGENTS.md

- Rule #14 update: for multi-pick + auto-refresh, the lock-race advice extends to concurrent auto-refreshes. Each profile's refresh still serializes via the per-profile file lock.
- Add new rule: "`exec` forwards stdin/stdout/stderr to the child. Don't try to capture claude's output by redirecting roost's stdout — redirect the child directly."

### Axiom integration note

Ping Axiom (pigeon) when `--auto-refresh` ships — their Conductor OAuth preflight can shrink from:

```
roost refresh --expired 2>/dev/null
profile=$(roost pick 2>/dev/null) || handle_failure $?
```

to:

```
profile=$(roost pick --auto-refresh 2>/dev/null) || handle_failure $?
```

When `exec` ships, their whole spawn-worker wrapper reduces further.

---

## Ordering for the actual session

1. Ship `--auto-refresh` alone first. Smaller blast radius. Commit.
2. Ship `--count` alone. Touches `pick.py` core but cleanly. Commit.
3. Ship `exec`. Bump to 0.5.0 proper.
4. Final commit + pmail to Axiom with the upgrade blurb.

---

## References in the existing codebase

- `src/claude_lb/pick.py:pick` — algorithm core, extend with count parameter
- `src/claude_lb/cli.py:profiles_pick` — flag plumbing
- `src/claude_lb/refresh.py:refresh_profile` — called by auto-refresh
- `src/claude_lb/probe.py:probe_many_sync` — re-probe after refresh
- `src/claude_lb/pick.py:append_pick_log` — reuse for exec audit trail

---

*Planned 2026-04-25 · Current state v0.4.1 · Target v0.5.0.*
