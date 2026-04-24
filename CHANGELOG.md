# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/)

## [Unreleased]

### Added

- `claude-lb doctor [--skip-network] [--json]` — diagnose local setup (config dir writability, profile discovery, credential parsing, cache readability, `api.anthropic.com` reachability).
- `claude-lb update [--json]` — report current version, detect git-upstream ahead/behind if applicable, emit a concrete upgrade hint.
- `py.typed` marker — downstream type-checkers now consume claude-lb's type hints.
- Environment variable documentation in README (`CLAUDE_LB_STICKINESS`, `CLAUDE_LB_PROFILES_DIR`, `CLAUDE_CONFIG_DIR`, `XDG_CONFIG_HOME`).

### Changed

- mypy strict mode now passes across the whole source tree (was not enforced in v0.1.0).

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
