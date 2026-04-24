# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/)

## [Unreleased]

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
