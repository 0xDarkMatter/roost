# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/)

## [Unreleased]

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

- **`roost exec <cmd...>`** — pick a profile, set `AXIOM_CLAUDE_PROFILE`
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
- `pick --export` for shell-sourceable `AXIOM_CLAUDE_PROFILE=<name>`.
- Monthly overage tracking (`usage.extra`) from `/api/oauth/usage` —
  `is_enabled`, `monthly_limit`, `used_credits`, `utilization`, `currency`.
- `Plan` column in status table from `claudeAiOauth.subscriptionType`.
- `probe --raw` — dump untouched `/api/oauth/usage` response body.
- OAuth token extraction with three-shape fallback:
  `claudeAiOauth.accessToken` → `oauthAccessToken` → `accessToken`.
- Cache at `~/.config/claude-lb/health.json` (Linux/macOS) or
  `%APPDATA%\claude-lb\health.json` (Windows); atomic write-rename.
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
