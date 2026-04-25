# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/)

## [Unreleased]

## [0.6.0] - 2026-04-25

### Added

- **Shell completion** — re-enabled (`add_completion=True`). Run
  `claude-lb --install-completion` to generate completion scripts for
  bash/zsh/fish/PowerShell. Per-argument completers wired up:
  - `claude-lb show <TAB>` / `probe`/`refresh`/`invalidate` complete to
    discovered profile names (read live from `~/.claude-profiles/`).
  - `--strategy <TAB>` completes to `sticky | least-used | round-robin |
    weighted | first-healthy`.
- **`claude-lb history`** — read recent picks + exec runs from `picks.log`.
  Default tails the last 20 entries; filterable by `--profile NAME`,
  `--since 30m|1h|2d|1w`, capped via `--tail N`. Renders a Rich table on
  stderr with elapsed-time formatting; emits structured JSON via `--json`
  (`{data: [...], meta: {count, total_in_log, log_path, filters}}`).
  Malformed log lines are silently skipped (rotation can leave partial
  trailing lines).
- **`claude-lb refresh --soon DURATION`** — anticipatory refresh.
  Refreshes profiles whose access token expires within the window
  (includes already-expired). Cron-friendly:
  `*/15 * * * * claude-lb refresh --soon 30m --json` keeps the fleet warm
  without burning cycles on already-fresh tokens. Mutually exclusive with
  `--all` / `--expired` / explicit names. DURATION grammar: `30s` / `30m`
  / `1h` / `2d` / `1w`, or bare seconds.
- **`claude-lb doctor` refresh-token check** — new `refresh_tokens_present`
  check warns (yellow `WARN`, doesn't fail) for any profile missing a
  stored refresh token. Such profiles silently fall through
  `--auto-refresh` (correctly — there's nothing to refresh) but only
  manifest at expiry; doctor surfaces them proactively.
- WARN level in doctor output — passed checks with `extra.warning=true`
  render as `[yellow]WARN[/yellow]` instead of `OK`. Keeps `all_passed`
  semantics for CI but draws operator attention.

### Internal

- `_parse_duration()` helper shared by `--soon` (refresh) and `--since`
  (history). Grammar: `<int>[smhdw]` or bare integer seconds.
- `_humanize_elapsed()` helper for `Xs/m/h/d ago` formatting in history.
- `_parse_pick_log()` parses tab-separated log lines into structured
  entries; tolerates malformed/partial lines.

## [0.5.0] - 2026-04-25

### Added

- **`claude-lb exec <cmd...>`** — pick a profile, run a child command with
  `AXIOM_CLAUDE_PROFILE` (configurable via `--var-name`) set in its env, and
  propagate the child's exit code as claude-lb's own. Collapses the whole
  spawn-worker wrapper from `profile=$(claude-lb pick --auto-refresh) &&
  AXIOM_CLAUDE_PROFILE=$profile claude ...` into:
  `claude-lb exec --auto-refresh -- claude ...`. Inherits all `pick` flags
  (`--strategy`, `--stickiness`, `--require-ok`, `--auto-refresh`). Additional
  flags:
  - `--retry-on-429 N` (default 1) — if the child exits non-zero AND a
    re-probe of the used profile shows it flipped from `ok` to `rate_limited`
    or `session_limit`, re-pick a different profile and rerun once.
    Heuristic; set 0 to disable. Timeout + command-not-found never trigger
    retry.
  - `--timeout S` — kill the child after S seconds (`rc=124`, POSIX convention).
  - `--dry-run` — print the env assignment and command that would execute,
    exit 0 without running the child.
  - `--log-full-argv` — log the full child argv in `picks.log`. Default logs
    argv[0] only (argv often contains tokens / secrets).
  - `--var-name NAME` — env var name for the picked profile (default
    `AXIOM_CLAUDE_PROFILE`).
  
  stdin/stdout/stderr are inherited so interactive children (like `claude`
  itself) work unchanged. Ctrl+C is forwarded on both POSIX and Windows.
  Every run logs `{ts}\t{profile}\tEXEC\targv=...\trc=...\tdur=...ms` to
  `picks.log` for post-hoc investigation.

- **`claude-lb pick --auto-refresh`** — before picking, inline-refresh any
  profile whose cached health is `auth_expired` and which has a stored refresh
  token. Collapses the two-step `claude-lb refresh --expired && claude-lb pick`
  preflight into a single call. Refresh failures are logged to stderr and fall
  through to the filter ladder (AUTH_EXPIRED profiles are still excluded), so
  pick never hangs on a broken token. Profiles without refresh tokens are
  skipped silently — they require `claude login`, not `refresh`.
- **`claude-lb pick --count N` (alias `-n N`)** — return up to N profiles
  ordered by strategy, newline-separated on stdout. If fewer than N pass the
  filter ladder, returns what's available (exit 0). Each pick is appended to
  `picks.log`; only the primary (first) pick updates `last-pick.json` so a
  subsequent single-pick call still honours stickiness. Ignores stickiness
  itself when `N > 1` — "pin to last pick" doesn't compose with "give me N
  distinct profiles". `--export` is incompatible with `--count > 1` (ambiguous:
  can't export N vars with one name) and exits `VALIDATION (4)`.
- `PickOutcome.chosen_many: list[ProfileHealth]` — ordered multi-pick result
  (length 1 for single-pick; up to `count` for multi-pick). The primary
  `chosen` field is always `chosen_many[0]` on success.
- `src/claude_lb/exec_cmd.py` — subprocess orchestration module. Named
  `exec_cmd` (not `exec`) to avoid shadowing the Python builtin.

### Changed

- **`pick --json` shape is now count-aware.** `--count 1` (explicit or default)
  keeps the single-object `{"data": {...}}` shape (backward compatible).
  `--count > 1` emits `{"data": [...], "meta": {count, requested, strategy, rationale}}`.
  Scripts that never pass `--count` see no change.

## [0.4.1] - 2026-04-25

### Fixed

- **`refresh` no longer crashes with `ModuleNotFoundError` on stale editable installs** ([pigeon#47 from Axiom](docs/findings.md)). `filelock` is now lazy-imported inside `refresh_profile`; if the tool venv is missing the dep (typical: `uv tool install` done at v0.3.0, `pyproject.toml` bumped to require filelock at v0.4.0, source drift picked up but venv not re-synced), refresh returns a clean `RefreshResult(error_code="MISSING_DEPENDENCY", ...)` with a concrete reinstall command instead of a traceback. Maps to `EXIT_ERROR`.

### Added

- **`claude-lb doctor` verifies subcommand imports** — loads every `claude_lb.*` module and reports per-module ImportError. Catches stale-install drift *before* the user trips over it by running a failing subcommand.
- **`claude-lb update --apply`** — actually performs the upgrade in-place (was previously status-only). Runs `git pull --ff-only` followed by `uv tool install --reinstall --editable <dir>`. `--no-pull` skips the git step and just re-syncs deps. Idempotent.
- `MISSING_DEPENDENCY` refresh error code, mapped to the refresh → exit-code table.

### Added (from [Unreleased] buffer)

- **`Plan` column in the status table** — surfaces `claudeAiOauth.subscriptionType` (e.g. `max`, `team`, `pro`) from the credentials file. Only rendered when at least one profile has the data, so older credential shapes aren't affected.
- `subscription_type` field on `Profile`, `ProfileHealth`, and `list`/`show`/`status` JSON payloads. Normalised to lowercase defensively.

### Changed (from [Unreleased] buffer)

- **Status table reset columns split** — a single `Resets (S/W)` cell with `S 34m · W 1d 4h` was hard to scan; now rendered as two right-justified columns `Session in` and `Weekly in` so durations line up vertically across profiles. Broken states (`auth_*`, `rate_limited`) place the remediation in `Session in` and leave `Weekly in` as `—`. The "in " prefix is stripped from cell values since it now lives in the column header.

## [0.4.0] - 2026-04-24

### Added

- **`extra_usage` block on every probe** — monthly overage quota tracking from
  `/api/oauth/usage`. `usage.extra` on `ProfileHealth` exposes `is_enabled`,
  `monthly_limit`, `used_credits`, `utilization`, `currency`. Surfaces as an
  "Overage" column in the status table (colour-coded; red at ≥100%).
- **Per-profile file lock on refresh** — `<credentials-path>.lock` (via
  [`filelock`](https://pypi.org/project/filelock/)). Two parallel
  `claude-lb refresh` invocations against the same profile now serialize
  instead of racing; refreshes of *different* profiles still run concurrently.
- **`LOCK_HELD` refresh error** → exit code `7` (`EXIT_CONFLICT`, Forma §5).
- **`claude-lb pick --warn-at <pct>`** — print a non-fatal warning to stderr
  when the chosen profile's session or weekly utilisation is ≥ N%. Exit code
  remains 0; stdout still just emits the profile name, so scripts are
  unaffected.
- **`claude-lb probe --raw`** — dump the untouched `/api/oauth/usage`
  response body per profile to stdout. Diagnostic only (no cache write);
  useful for capturing fixtures or inspecting unknown fields.
- **Richer status table** — new columns for Session %, Sonnet %, Opus %
  (only rendered when at least one profile has the data), human "Resets in
  37m" column replacing the raw timestamp, and colour coding at ≥80 / ≥100%.
- `output.humanize_until()` public helper for formatting future timestamps.

### Changed

- Status table column order: `Profile · Health · Session · Weekly · [Sonnet]
  · [Opus] · [Overage] · Resets in · Probed`. Columns in brackets are
  conditional on data presence to keep narrow-terminal output readable.

### Fixed

- Refresh no longer reports success when Anthropic returns HTTP 200 with a
  body that omits `access_token`; classifies as `UNEXPECTED_RESPONSE` so the
  credentials file is not rewritten with unchanged tokens. (Carried from the
  0.3.1 fix on `main`; consolidated into this minor bump.)

## [0.3.0] - 2026-04-24

### Added

- **8th health state `auth_expired`** — detected locally (no network call) when
  `.claudeAiOauth.expiresAt` is past. Distinct from `auth_dead`: `auth_expired`
  is transient (refreshable); `auth_dead` still requires `claude login`.
- `claude-lb refresh <name> [<name>...]` — exchange stored refresh tokens for
  fresh access tokens. POSTs to `https://api.anthropic.com/v1/oauth/token` with
  `anthropic-beta: oauth-2025-04-20`.
- `claude-lb refresh --all` — refresh every discovered profile.
- `claude-lb refresh --expired` — refresh only profiles whose access token is
  already past `expiresAt` (zero risk of burning a valid access token early).
- `claude-lb refresh --json` — machine-readable output with `previous_expires_at`
  and `new_expires_at` per profile.
- Successful refresh atomically rewrites `.credentials.json` (tempfile +
  `os.replace`) preserving all non-oauth fields; health cache is invalidated
  for refreshed profiles.
- `Profile.access_token_expires_at` and `Profile.refresh_token_present`
  populated by `discovery.py`.
- New `src/claude_lb/refresh.py` module (OAuth refresh grant client).
- `tests/test_refresh.py` (6 tests: success path, 401/400 rejection preserving
  credentials, network error, missing-token fast-fail, parallel refresh,
  request shape).
- SPEC §10 "Token refresh" — endpoint, headers, request/response shape, failure
  modes, out-of-scope notes.

### Changed

- Pick filter ladder now excludes `auth_expired` alongside `auth_dead`.
- `PickFailureReason.ALL_AUTH_EXPIRED` + CLI message pointing to
  `claude-lb refresh --expired`.
- Status table shows `claude-lb refresh <name>` in the Retry/Reset column when
  a profile is `auth_expired` (was `—`).

## [0.2.0] - 2026-04-24

### Changed

- **Probe endpoint swapped from `GET /v1/models` to `GET /api/oauth/usage`.**
  The `/v1/*` endpoints reject Claude Code Max OAuth tokens with 401
  "OAuth authentication is currently not supported" — so every profile in
  v0.1 was misclassified as `auth_dead` on live probe. The `/api/oauth/usage`
  endpoint accepts OAuth tokens when the `anthropic-beta: oauth-2025-04-20`
  header is set, and returns real utilization numbers.
- Health classifier now derives `session_limit` / `weekly_limit` from
  `five_hour.utilization >= 100` and `seven_day.utilization >= 100` rather
  than keyword-matching 429 error messages.
- Reset timestamps now come from the response body (`five_hour.resets_at`,
  `seven_day.resets_at`) instead of a "next Sunday 02:00 UTC" heuristic.
- `usage.session_pct` and `usage.weekly_pct` are populated on every healthy
  probe. Added `sonnet_pct` and `opus_pct` for per-model visibility.
- 403 responses with "scope requirement user:profile" are now classified
  as `ok` with `usage: null` — these are setup-tokens that work for
  inference but cannot read usage.

### Added

- `claude-lb doctor [--skip-network] [--json]` — diagnose local setup (config dir writability, profile discovery, credential parsing, cache readability, `api.anthropic.com` reachability).
- `claude-lb update [--json]` — report current version, detect git-upstream ahead/behind if applicable, emit a concrete upgrade hint.
- `py.typed` marker — downstream type-checkers now consume claude-lb's type hints.
- Environment variable documentation in README (`CLAUDE_LB_STICKINESS`, `CLAUDE_LB_PROFILES_DIR`, `CLAUDE_CONFIG_DIR`, `XDG_CONFIG_HOME`).
- mypy strict mode across the whole source tree.

### Removed

- `patterns.py` (session/weekly 429 keyword lists) — obsolete.
- "Previously-ok profile returning 403 → weekly_limit" heuristic — utilization numbers are a direct signal.
- 429-response test fixtures for session/weekly keyword matching.

### Fixed

- HANDOFF open question #1: OAuth tokens authenticate against `/api/oauth/usage`, not `/v1/*`.
- HANDOFF open question #3: Anthropic does expose a public usage API for Max plans.

## [0.1.0] - 2026-04-24

### Added

- Initial release.
- Seven-state health taxonomy (`ok`, `rate_limited`, `session_limit`, `weekly_limit`, `auth_dead`, `network_error`, `unknown`).
- `claude-lb profiles list` — enumerate discovered profiles under `~/.claude-profiles/`.
- `claude-lb profiles probe [name]` — live-probe all or one profile via `GET /v1/models`.
- `claude-lb profiles status` — cached health table (stderr) + summary (stdout).
- `claude-lb profiles show <name>` — one profile's full detail.
- `claude-lb profiles pick [--strategy <s>]` — return best healthy profile for scripting.
- `claude-lb profiles invalidate <name>` — drop a profile's cache entry.
- Pick strategies: `sticky` (default), `least-used`, `round-robin`, `weighted`, `first-healthy`.
- Stickiness window (default 300s, `CLAUDE_LB_STICKINESS` override).
- Cache at `~/.config/claude-lb/health.json` (Linux/macOS) or `%APPDATA%\claude-lb\health.json` (Windows); atomic write-rename.
- Pick log at `~/.config/claude-lb/picks.log` (tab-separated, 10 MB rotation).
- Semantic exit codes (0, 1, 2, 3, 4, 5, 6, 8, 9).
- `--json` on all commands with `{data, meta}` envelope.
- `pick --export` for shell-sourceable `AXIOM_CLAUDE_PROFILE=<name>`.
- OAuth token extraction with legacy-shape fallback (`claudeAiOauth.accessToken` → `oauthAccessToken` → `accessToken`).
- Credentials mtime bump = implicit cache invalidation for that profile.
- Cross-platform paths via `platformdirs`.
- Concurrent probing via `httpx.AsyncClient` + `asyncio.gather`.
