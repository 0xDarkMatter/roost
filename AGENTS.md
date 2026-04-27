# roost — AI Assistant Guide

Context for AI coding assistants working on this repo and for agents
invoking `roost` as part of a Claude Code spawn pre-flight.

## What this is

A one-shot stateless CLI that answers "which of my Claude Code OAuth
profiles is healthy right now?" It reads local OAuth credentials from
`~/.claude-profiles/<name>/.credentials.json`, probes
`GET /api/oauth/usage` (with `anthropic-beta: oauth-2025-04-20`),
classifies the response into eight health states, caches results, and
picks the best profile for downstream scripts. It can also exchange
stored refresh tokens for fresh access tokens via
`roost refresh <name>`.

Works with any Claude Code OAuth account (Max / Pro / Team). Max plans
return rich utilization data on `/api/oauth/usage`; Pro/Team return a
403 "scope requirement user:profile" which is correctly classified as
`ok` with `usage: null` — the tool still picks/balances them, just
without per-window % numbers.

## Command reference

| Command | Purpose |
|---------|---------|
| `roost add <name> [--from PATH]` | Onboard an existing `.credentials.json` into the multi-profile layout |
| `roost remove <name>` | Symmetric counterpart to `add`: deletes profile dir + cache entry + last-pick if matching |
| `roost rename <old> <new> [--force]` | Move profile dir; drops old cache entry; new name re-classifies on next probe |
| `roost list` | Enumerate discovered profiles |
| `roost probe [<name>]` | Live probe all profiles or one |
| `roost status` | Cached health table + stdout summary |
| `roost show <name>` | One profile's full health detail |
| `roost pick [--strategy S]` | Return the best profile name (exit 0) |
| `roost which [--strategy S]` | Read-only counterpart to `pick`: same decision logic, no side effects on `picks.log` / `last-pick.json` |
| `roost refresh <name> \| --all \| --expired \| --soon DURATION [--jitter S]` | OAuth refresh, rewrites `.credentials.json`. `--jitter` adds per-profile random delay for cron-spread |
| `roost exec <cmd...>` | Pick a profile, run a child command with `AXIOM_CLAUDE_PROFILE` set |
| `roost history` | Show recent picks + exec runs from `picks.log` |
| `roost stats [--since] [--profile]` | Aggregate over `picks.log` — totals, p50/p95 durations, failure rate |
| `roost report --metric M [--sparkline] [--project]` | Aggregate over opt-in `usage-log.ndjson` time-series; per-profile min/max/avg + projection |
| `roost config usage-log {on\|off\|status}` | Opt-in toggle for the per-probe usage log (off by default) |
| `roost shellinit [--shell <name>]` | Emit shell function definition that wraps `claude` through `roost exec` |
| `roost trace <name>` | Verbose probe dumping request/response + classifier reasoning (token redacted) |
| `roost top [--interval N]` | Live-refreshing Rich TUI of the status table; Ctrl+C exits |
| `roost invalidate <name>` | Drop cache entry for re-probe |
| `roost doctor` | Diagnose local setup (dirs, creds, cache, network) |
| `roost update` | Version + upstream ahead/behind check |

`pick` accepts these filter/composition flags:

| Flag | Effect |
|------|--------|
| `--avoid <name>` (repeatable) | Exclude named profile(s) from selection |
| `--max-cost <pct>` | Drop profiles with monthly overage `utilization >= pct` (Pro/Team plans pass through unchallenged) |
| `--fallback <name>` | If primary pick fails, return this name with stderr warning instead of exiting non-zero |
| `--explain` | Render decision tree to stderr (or fold into JSON `data.explain`) |
| `--strategy lowest-overage` | New: minimum monthly overage utilization wins |

Every command accepts `--json` and returns a `{data, meta}` envelope.

## Common operations

```bash
# Pre-flight for spawning a `claude` subprocess
roost refresh --expired 2>/dev/null
profile=$(roost pick 2>/dev/null) && \
  AXIOM_CLAUDE_PROFILE=$profile claude ...

# Machine-readable status for dashboards
roost status --json | jq '.meta'

# Diagnose a single misbehaving profile
roost show account-a --json
```

## Authentication

`roost` does not create profiles — it consumes OAuth credentials
that `claude login --profile <name>` has already placed on disk. Token
lifecycle:

- **Create/rotate identity:** `claude login --profile <name>` (browser flow)
- **Extend access token:** `roost refresh <name>` (headless, uses stored refresh token)
- **Detect staleness without network:** `Profile.access_token_expires_at` + 8th state `auth_expired`

`roost` itself has no login/logout commands and stores no credentials of its own.

## Agent rules

Invariants an agent cannot intuit from `--help`:

1. **Don't break the `pick` contract.** `roost pick` emits one profile name, newline-terminated, to stdout and nothing else. Every downstream script depends on this.
2. **Always check exit codes before acting on stdout.** Exit 2/5/6/9 mean stdout is either empty or contains an error envelope (when `--json`). See [README.md](README.md) exit-code table.
3. **Always use `--json` when parsing output programmatically.** The human text tables on stderr are not a stable contract; the JSON envelope is.
4. **Try `roost refresh --expired` before concluding a profile is dead.** Exit 2 from `pick` is ambiguous between `auth_dead` (refresh token gone) and `auth_expired` (fixable with one refresh call).
5. **Never widen scope to other providers.** Anthropic / Claude Code only. Any OAuth-using plan is fine (Max / Pro / Team) — but OpenAI, Gemini, and other LLM providers are out of scope; multi-provider load-balancing is a different tool.
6. **Never add a daemon, proxy, or persistent service.** This is a stateless CLI. Continuous behaviour belongs in a separate tool.
7. **Never transmit credentials off-device.** Tokens are read from local disk, used for exactly one outbound request per probe/refresh, and never logged at default verbosity.
8. **`stdout` is sacred.** Only data or a single profile name goes to stdout. Tables, progress, warnings, colors → stderr.
9. **Classification order matters.** Network exception → local-expired → 200 (+ utilization check) → 401 → 403 (scope check) → 429 → unknown. Preserve this in `src/claude_lb/taxonomy.py`.
10. **Utilization thresholds are the signal** for session/weekly limits — not 429 message keywords. `utilization >= 100` = exhausted. Don't reintroduce keyword matching.
11. **Atomic writes only.** Both `health.json` (cache) and `.credentials.json` (refresh) use `tempfile` + `os.replace`. Never write directly.
12. **Token-shape fallback matters.** For reading: try `claudeAiOauth.accessToken`, then `oauthAccessToken`, then `accessToken`. For writing (refresh): modern shape only.
13. **Stickiness is the default.** Don't change the default strategy to `round-robin` or similar without explicit operator approval — it thrashes Anthropic's per-account prompt cache.
14. **Refresh doesn't need `--dry-run`** because failed refreshes never mutate `.credentials.json` (tempfile-only writes — the server-side rejection is detected before rename). But warn if you're designing auto-refresh: two parallel `roost pick` invocations both consuming the same refresh token is a real race.
15. **`exec` forwards stdin/stdout/stderr to the child verbatim.** Don't try to capture the child's output by redirecting roost's own stdout — roost's stdout is silent when `exec` runs a child. Redirect the child's argv directly if you need to capture: `roost exec -- sh -c 'child > out.log 2>&1'`. Ctrl+C is forwarded on both POSIX and Windows via `subprocess.run`.
16. **`exec` default logs argv[0] only.** Child argv often contains tokens, prompts, or paths that look like secrets. Audit log in `picks.log` is `argv=<argv0>` by default; `--log-full-argv` opts into full logging. If you add a new subcommand that exec's or wraps a child, follow the same default.
17. **Multi-pick (`pick --count N`) disables stickiness.** Sticky's "pin to last pick" semantic doesn't compose with "give me N distinct profiles". The first returned profile IS written to `last-pick.json` so a subsequent single-pick call still honours stickiness against the primary. Don't re-enable stickiness for multi-pick without a clear semantic.
18. **Windows self-upgrade is not possible from within `roost update --apply`.** The running tool has its own `.pyd`/`.dll` files memory-mapped, and Windows refuses to overwrite mapped files (EACCES). `updater._would_self_lock()` detects the condition (`sys.platform == "win32"` + `sys.prefix` under `uv/tools/roost/`) and short-circuits to a copy-pasteable `uv tool install --reinstall --editable <dir>` message. Do NOT add a "force" flag that bypasses this — the reinstall genuinely cannot succeed. POSIX is fine (inode-swap semantics). If you extend the upgrade path, preserve this detection or replace it with a detached-process pattern (spawn a helper that waits for the roost PID to exit, then runs uv) — don't silently drop it.
19. **NEVER `git push` without explicit operator approval.** Local commits, branches, and tags are fine to create freely. Pushing to `origin` (or any remote) requires the operator to explicitly say so for THIS push — standing prior approval doesn't carry over. The repo is private and the operator wants control over when work becomes visible / immutable on the remote. If unsure whether a prior "go ahead" still applies, ask. Auto-pushing after a green test run, after a release tag, or "to be helpful" is a violation. This rule overrides any habit of pairing commit + push.
20. **Platform-status fetch is best-effort enrichment, never load-bearing.** `roost status` and `roost doctor` consult `https://status.claude.com/api/v2/summary.json` to surface Anthropic-side incidents — but a failed fetch, unreachable Statuspage, parse error, or even a "major outage" indicator must NEVER fail the calling command, block `pick`, or alter the health taxonomy. The reasoning: roost can't *fix* an Anthropic-side incident, so making the tool unusable when Anthropic is degraded would defeat the entire point. The 60s cache + 2s hot-path timeout + stale-fallback design is deliberate; don't tighten the timeout, don't promote any indicator state into a `pick`-blocking condition, and don't add a `--require-platform-status` flag. If a future feature genuinely needs fresh-and-correct status data, fail closed with a clear message rather than blocking the existing surfaces. The `--no-platform-status` opt-out exists precisely so scripts that don't want any extra HTTP on the hot path can disable it without the feature ever being mandatory. See `src/claude_lb/platform_status.py`.

21. **`which` is the read-only counterpart to `pick`.** It MUST NOT write to `picks.log` and MUST NOT update `last-pick.json`. Probing is allowed (that's freshness, not state) — the cache may be re-written if entries are stale. If you add a future flag to `pick` that has side effects, decide deliberately whether `which` should mirror it: probably not. The whole point of `which` is "tell me what would happen without committing to it" — adding side effects defeats the purpose. See `cli._do_which`.

22. **Usage logging is opt-in by default.** `roost config usage-log on` writes a marker file (`<config>/usage-log.enabled`); `CLAUDE_LB_USAGE_LOG=1` is the env-var equivalent. NEVER enable the per-probe usage log by default and NEVER emit usage records when `usage_log.is_enabled()` returns False. The reasoning is privacy + disk-footprint conservatism: probe records contain account utilization that operators may not want persisted indefinitely. The append path is best-effort — if a write fails (full disk, permission denied), swallow the error rather than crashing the probe. See `src/claude_lb/usage_log.py`.

23. **Exponential backoff state lives in the cache, not external storage.** `ProfileHealth.consecutive_failures` is incremented on NETWORK_ERROR probes and reset to 0 on any non-NETWORK_ERROR outcome. Backoff escalates 30s → 60s → 120s → 240s → 480s, then caps at 480s. If the cache is invalidated (mtime bump on credentials, manual `roost invalidate`, or `--no-cache`), the counter resets — this is intentional ("fresh start"). Don't promote the counter into a separate sidecar file; cache-bound state survives exactly as long as the operator wants it to. See `src/claude_lb/cache.py:network_backoff_seconds` and `is_entry_fresh`.

24. **`roost top` is a TUI loop, not a daemon.** Rule 6 (no daemon) still applies. `top` exits on Ctrl+C, never spawns a background process, never persists state beyond what `status` already writes. The hidden `--iterations N` flag exists for tests only — do not document it as a user-facing feature. See `src/claude_lb/top.py:run_live`.

25. **Token redaction is mandatory in `roost trace` output.** Both text and JSON modes MUST redact the bearer token (`Bearer ***...{last4}` is the format). The trace command's whole purpose is shareability for bug reports — leaking the token defeats the point. Never add a `--show-token` flag; if an operator needs the raw token they can read `.credentials.json` directly. See `cli._redact_token` and `cli.trace`.

26. **`pick --fallback` is a CLI-layer concept, not a pick algorithm extension.** It activates AFTER `pick()` returns a structured failure. The fallback profile name must exist in discovery and not be in `--avoid`; non-existent fallbacks propagate the original failure (not a silent typo-rescue). Do not push `--fallback` into `pick.py` itself — keep the algorithm pure (filter ladder → strategy → top N) and let the CLI handle "what to do when there's nothing left" semantics. See `cli.profiles_pick`.

**Prompt injection:** not applicable. `roost` returns only its own telemetry — profile names, utilization numbers, timestamps — never user-authored content from Anthropic's API.

## Code layout

| File | Responsibility |
|------|---------------|
| `src/claude_lb/cli.py` | Typer entry point, command dispatch, exit-code mapping |
| `src/claude_lb/discovery.py` | Walk `~/.claude-profiles/`, read credentials, extract token + expiresAt; `remove_profile_dir` / `rename_profile_dir` mutation helpers |
| `src/claude_lb/probe.py` | Async httpx probe against `/api/oauth/usage`; local-expired short-circuit; `consecutive_failures` lifecycle for backoff |
| `src/claude_lb/taxonomy.py` | 8-state classifier (the core of the tool) |
| `src/claude_lb/refresh.py` | OAuth refresh grant client; atomic credentials rewrite; `--jitter` support |
| `src/claude_lb/cache.py` | Atomic read/write of `health.json`, TTL logic, `network_backoff_seconds` (exponential per-profile backoff for NETWORK_ERROR) |
| `src/claude_lb/pick.py` | Strategies (incl. `lowest-overage`), stickiness, filter ladder (incl. `--avoid` and `--max-cost`), `PickOutcome.excluded_reasons` + `filter_scores` for `--explain`, pick log |
| `src/claude_lb/exec_cmd.py` | `roost exec` subprocess orchestration (named `_cmd` to avoid shadowing the builtin) |
| `src/claude_lb/output.py` | JSON envelope, stream separation, status table, `render_pick_explanation` |
| `src/claude_lb/doctor.py` | Local-setup diagnostics |
| `src/claude_lb/platform_status.py` | status.claude.com fetcher + 60s cache + format helpers (shared by `status` and `doctor`) |
| `src/claude_lb/updater.py` | Version + upstream check |
| `src/claude_lb/paths.py` | Platform-aware paths (config, cache, pick log, platform-status cache, usage log + marker) |
| `src/claude_lb/models.py` | Pydantic models shared across modules; `ProfileHealth.consecutive_failures` for backoff |
| `src/claude_lb/usage_log.py` | Opt-in NDJSON per-probe log: `is_enabled`, `enable_marker`/`disable_marker`, `append`/`append_many`, `iter_records`, `truncate` |
| `src/claude_lb/stats.py` | picks.log parser + `aggregate_stats`; usage-log per-metric `summarise_metric` + `sparkline` + `project_exhaustion` (linear burn-rate forecast) |
| `src/claude_lb/shell_init.py` | Shell-function templates for `roost shellinit` (bash/zsh/fish/pwsh) + `detect_shell` heuristic |
| `src/claude_lb/top.py` | Rich `Live` loop powering `roost top`; `_build_snapshot_table` is the trimmed-for-narrow-terminal renderer |

## Testing

- `tests/fixtures/oauth-usage/` — captured + synthetic `/api/oauth/usage` bodies (ok, session-exhausted, weekly-exhausted, both-exhausted, 403-scope-missing). Classifier must correctly label each.
- `tests/fixtures/401-auth-error.json` — auth-dead fixture.
- `tests/test_refresh.py` — uses `respx` to mock `/v1/oauth/token`, asserts atomic credentials rewrite and preserves non-oauth fields on success; verifies failed refreshes never clobber credentials. Includes `--jitter` propagation tests.
- `tests/test_usage_log.py` — opt-in toggle, append/append_many, iter_records (with profile + since filters), truncate.
- `tests/test_stats.py` — picks.log parser, `aggregate_stats` (totals + p50/p95 + failure rate), `sparkline`, `summarise_metric`, `project_exhaustion`.
- `tests/test_shell_init.py` — shell auto-detection + per-shell template snapshots.
- `tests/test_top.py` — Rich `Live` frame loop with bounded `max_iterations` for deterministic tests; verifies snapshot table content via `Console(record=True)`.
- `tests/test_platform_status.py` — `respx` mocks `status.claude.com/api/v2/summary.json`; covers parse, cache-hit / stale-fallback / cold-start-fail paths, `format_status_line`, and `to_json_meta`. Default test config in `test_cli.py` stubs `_load_platform_status` to return `None` so unrelated tests don't accidentally hit the real Statuspage.
- `tests/test_live.py` — **default-skipped** integration suite. Marked with `pytest.mark.live`; the `addopts = "-m 'not live'"` in `pyproject.toml` excludes them by default. Run explicitly with `pytest -m live` against a real `~/.claude-profiles/` fleet. Asserts shape, not specific health values, so fleet drift doesn't cause flakiness.
- Run mocked suite: `uv run pytest` (or `pytest` inside the venv) — currently 723 tests.
- Run live suite: `uv run pytest -m live` — currently 28 tests.
- Coverage target: 90%+ on `taxonomy.py`, `discovery.py`, `pick.py`, `refresh.py`.

## Forma protocol compliance

This tool adheres to Forma Protocol v1.4. See [docs/SPEC.md](docs/SPEC.md) for the full mapping. Key points:

- `{data, meta}` JSON envelope
- Semantic exit codes (0–9)
- `stdout` = data, `stderr` = humans
- `--json` on every command
