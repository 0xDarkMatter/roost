# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/)

## [Unreleased]

### Documentation

- **OAuth auto-refresh finding (2026-05-11).** Empirical verification that
  `claude` (Claude Code CLI) auto-refreshes its OAuth chain when the
  access_token expires, writing the new chain back to the file it read from
  (`$CLAUDE_CONFIG_DIR/.credentials.json`). This makes the snapshot pattern
  introduced in v0.5.0 unsafe for any workload long enough to trigger a
  refresh — the snapshot's new refresh_token diverges from the live source
  profile's, server-side-invalidating the source. New recommended
  trial-dispatch pattern: `CLAUDE_CONFIG_DIR=~/.claude-profiles/$(roost
  pick)` directly against the live profile dir; let claude own the refresh
  chain. See `docs/findings.md` §6 and the new "Trial dispatch (recommended
  pattern)" section of the README. No code changes — the `--lease` and
  `roost snapshot` surface remain available for sub-8h cases.

## [0.5.0] - Unreleased

### Added

**Credential rotation safety**

Fixes a real production failure where `roost refresh` (running at background
probe cadence) rotated a profile's refresh_token while a long-lived consumer
held a copy of the old credentials file. The consumer's SDK refresh attempt
returned `invalid_grant` → 401 on all subsequent API calls — indistinguishable
from model failure and wasting trial budget.

- **`roost exec --lease` (default on)** — `exec` now auto-leases the picked
  profile for the child's lifetime so that background probe pressure cannot
  rotate the credential under a long-running child. The lease is released in a
  `finally` block so Ctrl+C and exceptions both clean up correctly. Use
  `--no-lease` for short-lived children where the overhead is unwanted. Use
  `--lease-for <duration>` to override the TTL (defaults to `--timeout * 1.2`
  when a timeout is set, else 30m). While leased, `roost refresh` returns
  `LEASE_HELD` (exit 7) without touching the credentials file. Leases expire
  automatically at read time — no daemon or cleanup process needed.

- **`roost snapshot <profile> <out-path>`** — copy a profile's `credentials.json`
  to a stable path with an explicit stderr warning that roost will never touch
  the snapshot. Makes the "copy once, use for duration" pattern discoverable
  without silently reading the live file. Does not acquire a lease; for full
  rotation protection on arbitrary workloads use `roost exec` (which auto-leases).

**Short-session impact: zero.** `roost pick` never reads the lease store.
`roost exec --no-lease` opts out entirely. The lease overhead for a short-lived
child is a few milliseconds of JSON I/O at exec start and exit.

## [0.4.0] - 2026-04-27

### Added

**Symmetric profile management**

- **`roost remove <name>`** — symmetric counterpart to `add`. Recursively
  removes the profile directory, drops its cache entry, and clears
  `last-pick.json` if it pointed at the removed profile. Top-level + `profiles
  remove` namespace alias. The original `~/.claude/.credentials.json` is
  never touched.
- **`roost rename <old> <new>`** — atomic dir rename, drops old cache entry
  (new name re-discovers fresh on next probe), clears stale stickiness.
  `--force` overwrites an existing destination.
- **`roost which`** — read-only counterpart to `pick`. Returns what `pick`
  *would* choose right now without writing to `picks.log` or
  `last-pick.json`. Useful for debugging "why is it choosing X?" without
  contaminating stickiness state. Pairs naturally with `pick --explain`.

**Pick algorithm**

- **`pick --avoid <name>`** — repeatable. Excludes named profiles from
  selection. Composes with all strategies and with `--count`. Bypasses
  stickiness if the sticky pick is in the avoid list.
- **`pick --fallback <name>`** — if the primary pick fails for any reason,
  return this profile name with a stderr warning. Useful for scripts that
  prefer "any profile" over "no profile". Non-existent fallbacks
  propagate the original failure (so typos don't silently succeed).
- **`pick --max-cost <pct>`** — skip profiles whose monthly overage
  utilization is ≥ N%. Profiles without overage data (Pro/Team plans, or
  Max plans with overage disabled) are NEVER excluded — they have no
  cost signal to gate against.
- **`pick --strategy lowest-overage`** — new strategy: minimum monthly
  overage utilization wins. Different signal from `least-used` (weekly%);
  meaningful for users on Max overage budgets where the monthly cap
  matters more than rolling weekly use.
- **`pick --explain`** — render the pick decision tree to stderr (or fold
  into `data.explain` in JSON mode): discovered names, ladder exclusions
  with reasons, surviving candidates with strategy scores, plus the
  rationale string. Works for both success and failure paths.

**Observability**

- **`roost stats [--since] [--profile]`** — aggregate over `picks.log`:
  pick counts by profile + strategy, exec counts by rc, p50/p95
  duration, failure rate. JSON-first; text mode renders a summary.
- **`roost report --metric <m> [--sparkline] [--project]`** — aggregate
  over the new `usage-log.ndjson`: per-profile min/max/avg/latest for
  one metric, optional Unicode sparkline of the time-series, optional
  linear burn-rate projection of when each profile will reach 100%.
  Metrics: `weekly_pct | session_pct | sonnet_pct | opus_pct | overage_pct`.
- **`roost config usage-log {on|off|status}`** — opt-in toggle for the
  per-probe usage log at `<config>/usage-log.ndjson`. Disabled by
  default for privacy. Marker file at `<config>/usage-log.enabled`
  persists the toggle across daemon restarts; `CLAUDE_LB_USAGE_LOG=1`
  env var also enables. Once on, every successful probe appends one
  JSON record (ts, profile, health, percentages, latency).

**Reliability**

- **Per-profile `network_error` exponential backoff** — 30s → 60s → 120s →
  240s → 480s, capped. Stops thundering-herd against a flapping endpoint
  without forcing operators to invalidate the cache by hand. Counter
  resets to 0 on any non-NETWORK_ERROR outcome. New
  `ProfileHealth.consecutive_failures` field (Pydantic default 0 keeps
  backwards-compat with v0.3.0 cache files).
- **`refresh --jitter <seconds>`** — random 0..N second delay before
  each per-profile refresh. Cron-friendly: spreads concurrent
  invocations across the window so N machines don't all hit Anthropic's
  OAuth endpoint at the top of the minute.

**Integration surfaces**

- **`roost shellinit [--shell <name>]`** — emit a shell function
  definition that wraps `claude` through `roost exec --auto-refresh --
  claude ...`. One-time setup; afterwards every `claude` invocation
  routes through roost transparently. Templates for bash, zsh, fish,
  PowerShell. Auto-detects shell from `$SHELL` or `$PSModulePath`.
- **`roost trace <name>`** — verbose probe with full request/response
  envelope dump + classifier reasoning. Bearer token redacted to last 4
  characters in both text and JSON modes so traces are shareable in bug
  reports.
- **`roost top [--interval N]`** — live-refreshing TUI of the status
  table. Rich `Live` loop, Ctrl+C to exit. Hidden `--iterations N`
  flag bounds the loop for tests.

**Testing**

- **Live integration test suite** — `tests/test_live.py`, marked with
  `pytest.mark.live`, default-skipped via the `-m 'not live'` addopts.
  Run with `pytest -m live` against a real `~/.claude-profiles/` fleet.
  Mirrors major mocked tests: probe, status, pick (each strategy),
  pick --auto-refresh, pick --explain, refresh --soon, doctor, update,
  trace, stats, list, show. Asserts shape (not specific values) so
  fleet drift doesn't cause flakiness.

### Changed

- **`pick` JSON envelope** now includes `data.explain` (or
  `error.details.explain`) when `--explain` is set. Existing scripts that
  don't pass `--explain` see no change.
- **`PickOutcome`** dataclass gains `excluded_reasons: dict[str, str]`
  and `filter_scores: dict[str, float]` fields, populated on every
  `pick()` call regardless of `--explain` mode (cost is negligible).
- **`probe_many` / `probe_many_sync`** signatures gain `prev_failures:
  dict[str, int] | None` for backoff state propagation. Backwards-compat
  default is `None`.

### Internal

- New modules: `usage_log.py` (NDJSON append/read + opt-in toggle),
  `stats.py` (picks.log aggregation + metric summaries + sparkline +
  projection), `shell_init.py` (shell templates), `top.py` (Rich Live
  loop).
- New `discovery.MutationResult` + helpers (`remove_profile_dir`,
  `rename_profile_dir`).
- `cli._parse_pick_log` is now a thin alias for `stats.parse_pick_log`
  (refactored for reuse by `stats` command).
- Test count: **723 mocked + 28 live = 751 total** (up from 534 baseline).
  Coverage maintained.

## [0.3.0] - 2026-04-25

### Added

- **`roost add <name> [--from PATH] [--force]`** — onboard an existing
  `.credentials.json` into the multi-profile layout under
  `~/.claude-profiles/<name>/`. Default source is `~/.claude/.credentials.json`
  (where `claude login` writes). Validates JSON + token shape before committing;
  refuses overwrite without `--force`.
- **`roost status` platform-status header** — fetches
  `https://status.claude.com/api/v2/summary.json` (60s cache at
  `<config>/platform-status.json`, stale-fallback on fetch failure) and renders
  a one-line stderr header above the profile table when Anthropic reports a
  non-resolved incident or degraded component. Silent when everything's
  operational. JSON envelope folds into `meta.platform_status`. Opt out with
  `--no-platform-status`; `--no-cache` propagates through.
- **`roost doctor` status.claude.com check** — same endpoint, always-fresh
  (no cache), WARN-level. Distinguishes "my setup is broken" from "Anthropic
  is degraded right now". Skipped under `--skip-network`.

### Changed

- Audience widened — works for any Claude Code OAuth account (Max / Pro / Team),
  with richer usage data on Max. Pro/Team profiles return 403 on
  `/api/oauth/usage` (correctly classified as `ok` with `usage: null`).
- Identifying account names removed from docs, examples, test fixtures, and code
  comments. Test profiles now use neutral `account-a` / `account-b` / `account-c`.

### Internal

- New `src/claude_lb/platform_status.py` — shared fetcher + cache + format
  helpers used by both `status` and `doctor`.
- `_check_subcommand_imports` (doctor's stale-install detector) now covers
  `exec_cmd` and `platform_status`.

## [0.2.0] - 2026-04-25

### Added

- **`roost exec <cmd...>`** — pick a profile, set `ROOST_PROFILE`
  (configurable via `--var-name`) in the child's env, exec the command,
  propagate child rc. stdin/stdout/stderr inherited so interactive children
  work unchanged. Ctrl+C forwarded on both POSIX and Windows. Key flags:
  `--auto-refresh`, `--retry-on-429 N`, `--timeout S`, `--dry-run`,
  `--log-full-argv`. Every run audited to `picks.log`.
- **`roost pick --auto-refresh`** — before picking, inline-refresh any cached
  `auth_expired` profile that has a stored refresh token. Collapses the
  two-step `roost refresh --expired && roost pick` preflight into one call.
  Refresh failures are non-fatal — expired profile is filtered out and a
  healthy one is returned if any exists.
- **`roost pick --count N` (alias `-n N`)** — return up to N profiles ordered
  by strategy, newline-separated. Stickiness is disabled when N > 1.
  `--export` is rejected with `--count > 1`; use `--json` instead. JSON shape
  flips to array when N > 1.
- **Shell completion** (`roost --install-completion`) — bash/zsh/fish/pwsh,
  with profile-name + strategy completion.
- **`roost history`** — read recent picks + exec runs from `picks.log`.
  Filterable by `--profile NAME`, `--since 30m|1h|2d|1w`, `--tail N`.
  Structured JSON via `--json`.
- **`roost refresh --soon DURATION`** — anticipatory refresh; refreshes
  profiles expiring within the window. Cron-friendly:
  `*/15 * * * * roost refresh --soon 30m --json`.
- **`roost doctor` refresh-token check** — WARN-level check for profiles
  missing a stored refresh token (won't auto-heal at expiry).
- `PickOutcome.chosen_many` — ordered multi-pick result list.

### Internal

- `_parse_duration()` helper shared by `--soon` and `--since`.
- `_humanize_elapsed()` helper for `Xs/m/h/d ago` in history.
- `src/claude_lb/exec_cmd.py` — named to avoid shadowing the Python builtin.

## [0.1.0] - 2026-04-24

### Added

- Eight-state health taxonomy: `ok`, `rate_limited`, `session_limit`,
  `weekly_limit`, `auth_expired`, `auth_dead`, `network_error`, `unknown`.
  Classification order: network exception → local-expired → 200 + utilization
  → 401 → 403 scope-check → 429 → unknown.
- `roost add <name>` — import credentials into multi-profile layout.
- `roost list` — enumerate discovered profiles.
- `roost probe [<name>]` — live-probe all or one profile against
  `GET /api/oauth/usage` (with `anthropic-beta: oauth-2025-04-20`).
- `roost status` — cached health table + stdout summary. Columns: Profile,
  Plan, Health, Session %, Weekly %, Sonnet %, Opus %, Overage, Session in,
  Weekly in, Probed. Conditional columns rendered only when data is present.
- `roost show <name>` — full health detail for one profile.
- `roost pick [--strategy S]` — return best healthy profile name, exit 0.
  Strategies: `sticky` (default, 300s window), `least-used`, `round-robin`,
  `weighted`, `first-healthy`. `--require-ok`, `--export`, `--warn-at`,
  `--stickiness`.
- `roost refresh <name> | --all | --expired | --soon DURATION` — exchange
  stored refresh tokens for fresh access tokens. Atomic credentials rewrite
  (tempfile + `os.replace`). Per-profile file lock via `filelock`.
- `roost exec <cmd...>` — pick + set env + exec; see v0.2.0 for details
  (shipped together in the initial feature-complete release).
- `roost invalidate <name>` — drop a profile's cache entry.
- `roost doctor [--skip-network] [--json]` — diagnose local setup: config
  dir writability, profile discovery, credential parsing, cache readability,
  API reachability, refresh-token presence, subcommand import check.
- `roost update [--json] [--apply] [--no-pull]` — version + upstream
  ahead/behind check; `--apply` runs `git pull --ff-only` +
  `uv tool install --reinstall --editable`.
- `roost history` — read picks.log audit trail; see v0.2.0 for full flags.
- `--json` on every command with `{data, meta}` envelope.
- `pick --export` for shell-sourceable `ROOST_PROFILE=<name>`.
- Monthly overage tracking (`usage.extra`) from `/api/oauth/usage` —
  `is_enabled`, `monthly_limit`, `used_credits`, `utilization`, `currency`.
- `Plan` column in status table from `claudeAiOauth.subscriptionType`.
- `probe --raw` — dump untouched `/api/oauth/usage` response body.
- OAuth token extraction with three-shape fallback:
  `claudeAiOauth.accessToken` → `oauthAccessToken` → `accessToken`.
- Cache at `~/.config/roost/health.json` (Linux/macOS) or
  `%APPDATA%\roost\health.json` (Windows); atomic write-rename.
- Pick log at `<config>/picks.log` (tab-separated, 10 MB rotation).
- Shell completion via `--install-completion`.
- Semantic exit codes 0–9 (Forma Protocol §4).
- mypy strict mode across the source tree.
- `py.typed` marker.
- Graceful `MISSING_DEPENDENCY` error on stale editable installs — lazy
  `filelock` import returns a clean error + reinstall hint instead of a
  traceback.
- Windows self-upgrade detection: `updater._would_self_lock()` detects
  in-use `.pyd` files and emits the `uv tool install --reinstall` workaround
  instead of failing silently.
