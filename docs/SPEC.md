# roost Specification

> Version 0.1 · 2026-04-24
> CLI shape adapted from [Forma Protocol](file:///X:/Forma/00_forma/docs/protocol/) v1.4 · domain-specific content is roost's own.

---

## Contents

| § | Title |
|---|---|
| 1 | [Philosophy](#1-philosophy) |
| 2 | [Command Architecture](#2-command-architecture) |
| 3 | [Output Specification](#3-output-specification) |
| 4 | [Exit Codes](#4-exit-codes) |
| 5 | [Error Handling](#5-error-handling) |
| 6 | **[Health Taxonomy](#6-health-taxonomy)** |
| 7 | **[Probe Protocol](#7-probe-protocol)** |
| 8 | **[Cache Protocol](#8-cache-protocol)** |
| 9 | **[Pick Algorithm](#9-pick-algorithm)** |
| 10 | [Credential Discovery](#10-credential-discovery) |
| 11 | [Shell Integration](#11-shell-integration) |
| 12 | [Project Structure](#12-project-structure) |
| 13 | [Compliance Checklist](#13-compliance-checklist) |

Sections **6–9 are the core of roost**. Everything else is boilerplate Forma-protocol compliance.

---

## 1. Philosophy

Built as a Forma CLI — agentic-first, composable, parseable, quiet-by-default.

| Principle | Meaning |
|---|---|
| **Stateless, single-shot** | No daemon. Every invocation returns a result and exits. |
| **Cache is best-effort** | If the cache is missing or stale, probe. Never fail because cache is unreadable. |
| **Local credentials only** | Never transmits tokens. Only reads `~/.claude-profiles/*/.credentials.json` that the `claude` CLI already placed on disk. |
| **Fail open, report honestly** | On network error, classify as `network-error` and move on. Don't crash the calling script. |
| **Taxonomy is the value** | Four distinct 429 states, three distinct auth states. The lazy tool rotates on any non-200. The right tool distinguishes. |

### Design axioms (Forma §1)

1. `stdout` is sacred — only data, never progress.
2. `stderr` is for humans — tables, colors, warnings.
3. Exit codes have meaning — scripts branch on failure mode.
4. `--help` is comprehensive and current.
5. JSON shape is predictable — same `{data, meta}` envelope as every other Forma CLI.

---

## 2. Command Architecture

### Structural pattern (Forma §2)

```
roost [global-opts] <resource> <action> [opts]
```

Single resource: `profiles`. For convenience, top-level aliases collapse the resource word.

### Commands

| Command | Alias | Description |
|---|---|---|
| `roost profiles list` | `roost list` | List discovered profiles (no probe) |
| `roost profiles status` | `roost status` | Show cached health + usage per profile |
| `roost profiles show <name>` | `roost show <name>` | One profile's full health detail |
| `roost profiles probe [<name>]` | `roost probe` | Live-probe (all or one); update cache |
| `roost profiles pick [--strategy <s>]` | `roost pick` | Return the best healthy profile name |
| `roost profiles invalidate <name>` | `roost invalidate <name>` | Drop cache for a profile; forces re-probe |
| `roost profiles refresh [<name>\|--all\|--expired]` | `roost refresh ...` | Refresh OAuth tokens (§10) |
| `roost exec <cmd...>` | — | Pick a profile, run a child command with `AXIOM_CLAUDE_PROFILE` set; propagate child rc |
| `roost doctor` | — | Diagnose local setup (§11) |
| `roost update [--apply]` | — | Check or apply an in-place upgrade |
| `roost --version` | — | Print semver, exit 0 |
| `roost --help` | — | Show help, exit 0 |

### Naming conventions (Forma §2)

| Element | Convention |
|---|---|
| Tool name | `roost` (lowercase, 9 chars, one hyphen — kept under 12 per Forma) |
| Resource | `profiles` (plural noun) |
| Actions | lowercase verbs (`list`, `probe`, `pick`) |
| Long flags | kebab-case (`--strategy`, `--no-cache`, `--json`) |
| Short flags | single letter where standard (`-n` count, `-q` quiet, `-v` verbose) |

---

## 3. Output Specification

### Stream separation (Forma §4)

| Stream | Content |
|---|---|
| **stdout** | Data only — JSON if `--json`, text columns otherwise |
| **stderr** | Progress, tables, colors, warnings, debug |

### `profiles status` — default (interactive TTY)

```
Profile       Health          Retry             Weekly   Probed
────────      ──────          ─────             ──────   ──────
account-a        ok              —                 9%       38s ago
account-b    ok              —                 9%       12m ago  (cache)
account-c        auth-dead       claude login      —        1m ago
```

Table to stderr, status summary to stdout:

```
3 profiles · 2 ok · 1 auth-dead
```

### `profiles status --json`

```json
{
  "data": [
    {
      "name": "account-a",
      "health": "ok",
      "probed_at": "2026-04-24T09:45:12Z",
      "usage": {"weekly_pct": 9, "session_pct": 0},
      "retry_after_s": null,
      "weekly_reset_at": "2026-04-26T16:00:00Z",
      "error": null
    },
    {
      "name": "account-c",
      "health": "auth-dead",
      "probed_at": "2026-04-24T09:45:12Z",
      "usage": null,
      "retry_after_s": null,
      "weekly_reset_at": null,
      "error": {"type": "authentication_error", "message": "Invalid authentication credentials"}
    }
  ],
  "meta": {
    "count": 3,
    "ok": 2,
    "rate_limited": 0,
    "session_limit": 0,
    "weekly_limit": 0,
    "auth_dead": 1,
    "network_error": 0,
    "cache_source": "mixed"
  }
}
```

### `profiles pick` — plain output

```
account-a
```

**One profile name, newline-terminated, nothing else.** This is the scripting contract. Break this and every downstream shell script breaks.

### `profiles pick --export`

```
AXIOM_CLAUDE_PROFILE=account-a
```

Shell-sourceable via `eval $(roost pick --export)`. No quoting — profile names are guaranteed `[a-zA-Z0-9_-]+` (same constraint as directory names under `~/.claude-profiles/`).

### `profiles pick --json`

```json
{
  "data": {
    "name": "account-a",
    "health": "ok",
    "score": 0.91,
    "rationale": "lowest weekly usage among healthy"
  }
}
```

### Field conventions (Forma §4)

| Type | JSON type | Example |
|---|---|---|
| Timestamps | ISO 8601 UTC | `"2026-04-24T09:45:12Z"` |
| Durations | integer seconds | `"retry_after_s": 60` |
| Usage fractions | integer % (0–100) | `"weekly_pct": 9` |
| Enums | lower_snake_case | `"health": "rate_limited"` |
| Nulls | explicit | `"error": null` |

**Deviation from Forma:** enum values use `lower_snake_case` rather than `UPPER_SNAKE_CASE` because these map 1:1 to Anthropic's `error.type` values (which are lowercase). Consistency with the upstream API wins.

---

## 4. Exit Codes

Standard Forma (§5) mapping:

| Code | Name | When |
|---|---|---|
| 0 | SUCCESS | Picked a healthy profile, or status rendered cleanly |
| 1 | ERROR | Unexpected failure |
| 2 | AUTH_REQUIRED | `pick` found zero profiles with `auth-dead` across the board — operator must `claude login` |
| 3 | NOT_FOUND | `show <name>` / `probe <name>` given unknown profile |
| 4 | VALIDATION | Bad flag combination (e.g. `--strategy unknown`) |
| 5 | FORBIDDEN | `pick --require=ok` but no profile currently `ok` |
| 6 | RATE_LIMITED | All healthy profiles are `rate_limited` or `session_limit`; no pick available |
| 8 | TIMEOUT | Probe timed out (uses `--timeout`, default 10s) |
| 9 | UNAVAILABLE | All profiles in terminal-bad states (`weekly_limit` / `auth_dead`) |

### Scripting example

```bash
profile=$(roost pick 2>/dev/null)
case $? in
  0) export AXIOM_CLAUDE_PROFILE="$profile" ;;
  5) echo "no ok profiles; sleeping 60s then retrying with rate-limited allowed" >&2; sleep 60 ;;
  6) echo "all profiles throttled; backing off" >&2; sleep 300 ;;
  9) echo "all profiles exhausted or dead; operator intervention required" >&2; exit 1 ;;
  *) exit 1 ;;
esac
```

---

## 5. Error Handling

Forma §6 error envelope:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Unknown strategy 'frobulate'",
    "details": {"allowed": ["round-robin", "least-used", "weighted"]}
  }
}
```

Always: data-structured JSON to stdout when `--json`, human message to stderr, semantic exit code.

---

## 6. Health Taxonomy

**The heart of the tool.** Seven states, each with a distinct signal source, TTL, and next-action. All signals come from a single probe against `/api/oauth/usage` (see §7).

| State | Detection | TTL in cache | Next-action hint |
|---|---|---|---|
| `ok` | HTTP 200 with `seven_day.utilization < 100` and `five_hour.utilization < 100`. Also: HTTP 403 with `"scope requirement user:profile"` (setup-token — valid for inference, can't read usage) | 5 min | — |
| `rate_limited` | HTTP 429 on the usage endpoint itself (rare) | `retry-after` seconds, else 60 s | Retry in N seconds |
| `session_limit` | HTTP 200 with `five_hour.utilization >= 100` | until `five_hour.resets_at` (parsed from body, else +5 h from probe) | Skip until session reset |
| `weekly_limit` | HTTP 200 with `seven_day.utilization >= 100` | until `seven_day.resets_at` (parsed from body, else +7 days from probe) | Skip until weekly reset; operator tier upgrade may be needed |
| `auth_dead` | HTTP 401, body `error.type == authentication_error` | infinite (manual invalidation only) | `claude login --profile <name>` |
| `network_error` | timeout / DNS / TLS / refused | 30 sec | Transient; retry |
| `unknown` | any other response (5xx, non-scope 403, malformed body) | 60 sec | Logged for operator review |

### Classification order (first match wins)

```
1. if exception (timeout, DNS, TLS):        network_error
2. if HTTP 200:
     if seven_day.utilization >= 100:        weekly_limit
     elif five_hour.utilization >= 100:      session_limit
     else:                                    ok
3. if HTTP 401:                              auth_dead
4. if HTTP 403:
     if message contains "scope" + "user:profile":   ok (usage: null)
     else:                                            unknown
5. if HTTP 429:                              rate_limited (with retry-after if present)
6. otherwise:                                unknown
```

**No keyword matching.** Utilization numbers are the direct signal; the 429 keyword lists from v0.1 (`patterns.py`) are retired.

### Usage stats (always populated on a healthy probe)

Fields on `data[].usage` — all integers 0–100 (or `null` when unavailable):

- `session_pct` ← `five_hour.utilization`
- `weekly_pct`  ← `seven_day.utilization`
- `sonnet_pct`  ← `seven_day_sonnet.utilization`
- `opus_pct`    ← `seven_day_opus.utilization`

Reset timestamps on `data[]`:

- `session_reset_at` ← `five_hour.resets_at`
- `weekly_reset_at`  ← `seven_day.resets_at`

For setup-tokens (403 scope-missing fallback), `usage` is `null` because the profile cannot read these numbers — but the token itself is still valid for inference, so the profile classifies as `ok` with the next-probe TTL treated as `auth_dead`-like (manual invalidate only) until a fresh `claude login --profile <name>` swaps in a user:profile-scoped token.

---

## 7. Probe Protocol

### Endpoint

```
GET https://api.anthropic.com/api/oauth/usage
```

**Why `/api/oauth/usage`:** it is the only Anthropic endpoint that (a) accepts Claude Code Max OAuth tokens and (b) returns the utilization numbers the Max dashboard uses. The public `/v1/*` endpoints require an API key and respond with 401 "OAuth authentication is currently not supported" for Max OAuth bearers.

**Key detail:** the endpoint is gated by the `anthropic-beta: oauth-2025-04-20` header. Without that header, the same request returns 401.

### Request

```http
GET /api/oauth/usage HTTP/1.1
Host: api.anthropic.com
Authorization: Bearer <oauth-access-token>
anthropic-version: 2023-06-01
anthropic-beta: oauth-2025-04-20
Accept: application/json
User-Agent: roost/<version>
```

The OAuth access token is read from `~/.claude-profiles/<name>/.credentials.json` (see §10).

### Response shape (HTTP 200)

```json
{
  "five_hour":  {"utilization": 9.0, "resets_at": "2026-04-24T14:00:00.019+00:00"},
  "seven_day":  {"utilization": 6.0, "resets_at": "2026-04-25T04:00:00.019+00:00"},
  "seven_day_sonnet": {"utilization": 0.0, "resets_at": null},
  "seven_day_opus":   null,
  "extra_usage": {"is_enabled": true, "monthly_limit": 100000, "used_credits": 10649, "utilization": 10.6, "currency": "AUD"}
}
```

Only `five_hour`, `seven_day`, `seven_day_sonnet`, and `seven_day_opus` are consumed by the classifier. `extra_usage` (pay-per-use pool) is recorded but does not influence health state in v0.2.

### Response classification

Per §6 table. Parse body as JSON; gracefully handle non-JSON with `unknown`.

### Timeout

Default 10 seconds per probe (configurable via `--timeout`). Concurrent probes across profiles via `httpx.AsyncClient` + `asyncio.gather` — all profiles probed in parallel.

### Rate limit for the probe itself

One probe per profile per invocation unless `--all` is used with `invalidate`. Cache-first read path means routine `status` / `pick` never probes unless cache is stale. The usage endpoint is not known to be rate-limited under normal use, but if it returns 429, the classifier maps it to `rate_limited` with `retry-after` honoured.

---

## 8. Cache Protocol

### Location

| Platform | Path |
|---|---|
| Linux / macOS | `~/.config/claude-lb/health.json` |
| Windows | `%APPDATA%\claude-lb\health.json` |

XDG override respected: `$XDG_CONFIG_HOME/roost/health.json`.

### Schema

```json
{
  "schema_version": 1,
  "updated_at": "2026-04-24T09:45:12Z",
  "profiles": {
    "account-a": {
      "health": "ok",
      "probed_at": "2026-04-24T09:45:12Z",
      "expires_at": "2026-04-24T09:50:12Z",
      "error": null,
      "retry_after_s": null,
      "session_reset_at": null,
      "weekly_reset_at": "2026-04-26T16:00:00Z",
      "usage": {"weekly_pct": 9, "session_pct": 0},
      "probe_latency_ms": 287
    },
    "account-c": {
      "health": "auth_dead",
      "probed_at": "2026-04-24T09:45:12Z",
      "expires_at": null,
      "error": {
        "type": "authentication_error",
        "message": "Invalid authentication credentials"
      }
    }
  }
}
```

### Freshness check

A cached entry is **fresh** iff `probed_at <= now() <= expires_at`. Past `expires_at`, the entry is re-probed on next `status`/`pick` call (unless `--no-cache` forces all-probe).

`auth_dead` entries have `expires_at: null` — they never expire until manually invalidated via `roost invalidate <name>` or until the profile's `.credentials.json` mtime changes (treat mtime bump as implicit invalidation).

### Concurrency

Cache writes use **atomic write-rename** (`tempfile` in same dir + `os.replace`). Reads use a brief shared lock. Conflicts are rare; worst case, last-writer-wins is acceptable.

### Cache flags

- `--no-cache` — ignore cache entirely, probe everything
- `--refresh` — probe everything, write back to cache (same as `probe` action)
- `--max-age <seconds>` — override default TTLs with a single value

---

## 9. Pick Algorithm

### Inputs

- Discovered profile list (§10)
- Health cache (§8), refreshed if stale

### Strategies (`--strategy`)

| Strategy | Behavior |
|---|---|
| `sticky` | **Default.** Prefer the most-recently-picked profile *if still healthy and within stickiness window*. Else fall back to `least-used`. Maximises prompt-cache locality across back-to-back parcels. |
| `least-used` | Prefer `ok` profiles, sorted by `usage.weekly_pct` ascending, then by `probed_at` descending (most recently verified) |
| `round-robin` | Prefer `ok` profiles, sorted by last-picked timestamp from `~/.config/claude-lb/picks.log` ascending. Good when load-spreading beats cache locality. |
| `weighted` | Like `least-used` but multiplies by `1 / (session_pct + 1)` so a profile with low session usage beats one with low weekly usage |
| `first-healthy` | Iterate in discovery order; return first `ok`. Deterministic testing. |

### Stickiness

**Why it matters:** Anthropic's prompt cache is **per-account**. Switching profile across back-to-back parcel dispatches invalidates the cache for the new account's first request. For workloads that touch overlapping context (same codebase, same docs), staying on one profile compounds cache hits and reduces both quota burn and latency.

**Configuration:**

```bash
roost pick --stickiness 300       # Stick for 5 min (default)
roost pick --stickiness 0         # Disable stickiness entirely
roost pick --strategy round-robin # Load-spread explicitly (ignores stickiness)
```

**Algorithm:**

```
1. Read last-picked entry from ~/.config/claude-lb/picks.log
2. If last_pick.profile exists in discovered list
   AND last_pick.profile is currently `ok`
   AND (now - last_pick.timestamp) < stickiness_seconds:
      → return last_pick.profile
3. Else fall through to the configured strategy
```

**Env var:** `CLAUDE_LB_STICKINESS=<seconds>` (default 300). A stickiness of 0 disables the behaviour and reverts to the configured strategy.

**Max plan note:** on pay-per-token API keys, prompt cache hits cost ~10% of input tokens, saving materially on long contexts. On Max subscriptions the cost model is flat-rate but sessions / weekly budgets are token-counted; whether cache hits count less against Max quotas is undocumented as of 2026-04 — see [HANDOFF.md "open questions"]. Stickiness is beneficial either way: cache hits on Max plans reduce latency even if they don't reduce quota burn.

### Filter ladder

```
1. Start with all discovered profiles
2. Drop auth_dead
3. Drop auth_expired (unless --auto-refresh heals them first; see "Auto-refresh" below)
4. Drop weekly_limit where weekly_reset_at > now
5. Drop session_limit where session_reset_at > now
6. Drop rate_limited where retry_after_s > now (from probed_at)
7. If --require=ok: drop anything not ok
8. Sort by strategy
9. Return top 1 — or top N for --count N
```

### Multi-pick (`--count N`, alias `-n N`)

Returns up to N profiles in strategy order. If fewer than N pass the ladder,
returns what's available (exit 0); callers decide whether partial fulfilment
is acceptable. Stickiness is **ignored** when `N > 1` — the "pin to the last
pick" semantic doesn't compose with "give me N distinct profiles". Every
picked profile gets a `picks.log` entry; only the primary (first) pick
updates `last-pick.json`, so a subsequent single-pick call still honours
stickiness against the primary. `--export` is rejected with `--count > 1`
(can't export N vars with one name) and exits `VALIDATION (4)`.

### Auto-refresh (`--auto-refresh`)

Before running the filter ladder, refreshes any cached `auth_expired`
profile whose credentials file has a stored refresh token. Success → re-probe
→ updated health. Failure → stderr warning; the profile stays `auth_expired`
and is filtered out as normal. Profiles without a refresh token are skipped
silently (they need `claude login`, not `refresh`). Collapses the
`refresh --expired && pick` preflight into one call.

### Empty-set behaviour

| Situation | Exit code | stderr message |
|---|---|---|
| All `auth_dead` | 2 | `No authenticated profiles. Run: claude login --profile <name>` |
| All `weekly_limit` | 9 | `All profiles weekly-exhausted. Earliest reset: <timestamp>` |
| All in any terminal-bad state | 9 | `No profiles available. Run: roost status` |
| `--require=ok` with no `ok` | 5 | `No profiles currently ok. Run: roost probe` |
| Some `rate_limited` / `session_limit` still within their TTL | 6 | `All profiles throttled. Earliest retry: <timestamp>` |

### Pick log (audit trail)

Every successful pick appends to `~/.config/claude-lb/picks.log`:

```
2026-04-24T09:45:12Z	account-a	least-used	score=0.91
```

Tab-separated, append-only, automatic rotation at 10MB (drop oldest half).

---

## 10. Credential Discovery

### Discovery paths

First hit wins:

1. `$CLAUDE_LB_PROFILES_DIR` — explicit env override, absolute path to a directory containing `<name>/.credentials.json` subtrees
2. `~/.claude-profiles/` — default, canonical path set by Claude Code CLI's profile system
3. `$CLAUDE_CONFIG_DIR` — single-profile fallback: if the dir contains `.credentials.json` directly, treat as profile named `default`

Profile name = subdirectory name. Constraint: `[a-zA-Z0-9_-]+`. Dirs not matching are skipped silently.

### Credential extraction

Read `<profile_dir>/.credentials.json`. Shape as of Claude Code 1.x:

```json
{
  "claudeAiOauth": {
    "accessToken": "sk-ant-oat01-...",
    "refreshToken": "sk-ant-ort01-...",
    "expiresAt": 1730000000000,
    "scopes": ["user:inference", "user:profile"],
    "subscriptionType": "max"
  }
}
```

Token extraction order (first match wins, to be robust against format drift):

1. `.claudeAiOauth.accessToken` (modern)
2. `.oauthAccessToken` (legacy)
3. `.accessToken` (plain)

If none present → classify profile as `auth_dead` with `error.type = "missing_token"`.

### Token refresh (v0.3+)

Max plan OAuth access tokens live ~5 hours. When `.claudeAiOauth.expiresAt` is past, the stored refresh token can be exchanged for a new access token without re-authenticating interactively.

**Local detection (no network):** `discovery.py` records `access_token_expires_at` and `refresh_token_present` on every `Profile`. `probe.py` short-circuits to `AUTH_EXPIRED` (health state) when the stored token is already past its expiry — no round-trip needed.

**Explicit refresh:** `roost refresh <name> | --all | --expired` POSTs to `https://api.anthropic.com/v1/oauth/token`:

```http
POST /v1/oauth/token HTTP/1.1
Host: api.anthropic.com
Content-Type: application/json
anthropic-beta: oauth-2025-04-20

{
  "grant_type":    "refresh_token",
  "refresh_token": "<stored refresh token>",
  "client_id":     "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
}
```

**Response (200):**

```json
{"access_token": "...", "refresh_token": "...", "token_type": "Bearer", "expires_in": 28800}
```

On success, `.credentials.json` is atomically rewritten (tempfile + `os.replace`) with the new `accessToken`, `refreshToken` (refresh tokens rotate), and `expiresAt = now_ms + expires_in*1000`. All non-oauth fields in the file are preserved. The health cache entry for the refreshed profile is invalidated so the next `status`/`pick` probes with the new token.

**Failure modes:**
- HTTP 400/401 → `REFRESH_REJECTED` (refresh token dead — run `claude login --profile <name>`). Credentials file is not touched.
- Network error → `NETWORK_ERROR` (transient — retry)
- No refresh token in file → `NO_REFRESH_TOKEN` (fail fast, no network call)

**Out of scope:**
- Auto-refresh on probe (deferred to v0.4+ — requires cross-process locking to avoid two parallel `pick` invocations both consuming the same refresh token).
- Browser-based OAuth flow. `roost` cannot create a profile from scratch; use `claude login --profile <name>` for that.

### Never touch

- `~/.claude/` (single-profile state — belongs to the main login)
- `~/.claude-profiles/<name>/projects/` (session state)
- `~/.claude-profiles/<name>/shell-snapshots/` (shell history)
- Anything outside `.credentials.json`

---

## 11. Shell Integration

### `pick --export`

```bash
$ roost pick --export
AXIOM_CLAUDE_PROFILE=account-a
```

Output format:

- `<VAR>=<value>` on stdout, nothing else
- Variable name: `AXIOM_CLAUDE_PROFILE` by default (matches Axiom's var); override with `--var-name <NAME>`

Usage:

```bash
eval $(roost pick --export)
# $AXIOM_CLAUDE_PROFILE is now set in current shell
```

### Shell completion (optional)

Via Typer's `--install-completion`. Bash / Zsh / Fish / PowerShell.

### Exit code scripting

See §4. Every exit code mapped to a concrete operator / retry action.

---

## 12. Project Structure

Standard Forma §16 layout, Python + `uv`:

```
roost/
├── README.md                     # This file
├── SPEC.md                       # This spec
├── HANDOFF.md                    # For the build agent
├── LICENSE                       # MIT
├── pyproject.toml                # Package config + [tool.forma]
├── docs/
│   └── references/               # Links to prior art (TeamClaude etc.)
├── src/claude_lb/
│   ├── __init__.py               # Version
│   ├── cli.py                    # Typer CLI entry point
│   ├── discovery.py              # ~/.claude-profiles/ walk + credentials.json parse
│   ├── probe.py                  # async httpx probe + classification
│   ├── taxonomy.py               # Health enum + classifier (the §6 logic)
│   ├── cache.py                  # Read/write ~/.config/claude-lb/health.json
│   ├── pick.py                   # Strategies + filter ladder
│   ├── output.py                 # stdout/stderr separation, JSON envelope
│   └── patterns.py               # Externalised keyword lists for 429 classification
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   └── 429-responses/        # Real Anthropic 429 bodies for classification tests
    ├── test_taxonomy.py          # Classifier must correctly parse each fixture
    ├── test_discovery.py         # tmp_path + synthetic .credentials.json
    ├── test_cache.py             # TTL + atomic writes + concurrent access
    ├── test_pick.py              # Each strategy against fixed fixtures
    └── test_cli.py               # End-to-end via Typer's test runner
```

### `pyproject.toml`

```toml
[project]
name = "roost"
version = "0.1.0"
description = "Claude Code profile health + load balancer"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "typer>=0.9.0",
    "rich>=13.0.0",
    "httpx>=0.25.0",
    "pydantic>=2.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.3",
    "mypy>=1.8",
]

[project.scripts]
roost = "claude_lb.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/claude_lb"]

[tool.forma]
description = "Pick the healthiest Claude Code Max profile"
resources = ["profiles"]
auth = "none"
status = "experimental"
origin = "forma"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP", "B", "SIM"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

### Install

```bash
# From repo root:
uv tool install --editable .

# Then anywhere:
roost status
```

---

## 13. Compliance Checklist

### Minimum viable

- [ ] `roost profiles list` works
- [ ] `roost profiles probe` live-probes all discovered profiles
- [ ] `roost profiles status` renders cached health in a rich table (stderr) + JSON summary (stdout)
- [ ] `roost profiles pick` returns exactly one healthy profile name on stdout, exit 0
- [ ] All §6 seven states classified correctly from real Anthropic response fixtures
- [ ] `--json` works on every command
- [ ] Semantic exit codes (§4)
- [ ] Cache at `~/.config/claude-lb/health.json` with atomic writes
- [ ] Cross-platform paths (Linux/macOS/Windows)
- [ ] 90%+ test coverage on `taxonomy.py`, `discovery.py`, `pick.py`

### Complete

- [ ] `pick --export` emits shell-sourceable `VAR=value`
- [ ] `pick --strategy round-robin|least-used|weighted|first-healthy`
- [ ] `invalidate <name>` drops cache entry; mtime bump on credentials.json auto-invalidates
- [ ] `probe --parallel` concurrent probes via `asyncio.gather`
- [ ] `--max-age <seconds>` overrides default TTLs
- [ ] Pick log at `~/.config/claude-lb/picks.log` with 10MB rotation
- [ ] `--verbose` dumps full probe payloads to stderr for debugging
- [ ] README includes runnable examples for every command
- [ ] CI (GitHub Actions): lint + type-check + test on 3.11 / 3.12 / 3.13 across Linux / macOS / Windows

### Stretch

- [ ] Token refresh when Claude Code CLI's own refresh mechanism is documented (borrow from [teamclaude](https://github.com/KarpelesLab/teamclaude))
- [ ] Shell completion installer
- [ ] `roost doctor` — diagnose "why is profile X not picked?" with rationale trace
- [ ] Optional Anthropic usage API integration (if/when endpoint available publicly)

---

## References

- [Forma Protocol v1.4](file:///X:/Forma/00_forma/docs/protocol/) — CLI shape parent
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — 28k★ transparent HTTP proxy with multi-account rotation
- [TeamClaude](https://github.com/KarpelesLab/teamclaude) — MIT, Node, closest conceptual prior art; good OAuth refresh logic to borrow
- [vibeproxy](https://github.com/automazeio/vibeproxy) — macOS menu bar GUI wrapping CLIProxyAPIPlus
- [CCS / Claude Code Switch](https://github.com/kaitranntt/ccs) — CLI credential swapper
- [anthropics/claude-code issue #44687](https://github.com/anthropics/claude-code/issues/44687) — open issue: multi-account not built-in

---

*Spec v0.1 · 2026-04-24 · adapted from Forma Protocol v1.4.*
