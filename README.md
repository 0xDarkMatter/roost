![roost](docs/assets/roost-banner.png)

[![Forma](https://img.shields.io/badge/forma-experimental-orange.svg)](https://github.com/forma-tools/forma)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.3.0-blue.svg)](CHANGELOG.md)
[![Hackathon](https://img.shields.io/badge/Claude%20Opus%204.7-Hackathon-blueviolet?logo=anthropic)](https://www.anthropic.com/)
![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)

> Pick the healthiest Claude Code OAuth profile — health taxonomy + load balancer for local OAuth profiles. Works with any plan (Max / Pro / Team), with richer usage data on Max.

**Built for the Claude Opus 4.7 Hackathon.**

![roost](docs/assets/hackathon.gif)

## Architecture

```
  ~/.claude-profiles/<name>/.credentials.json
                    │
                    ▼  discover_profiles()
         ┌──────────────────┐    probe (async)    ┌───────────────────────┐
         │    discovery     │───────────────────► │   api.anthropic.com   │
         └────────┬─────────┘ ◄── classify ────── │   GET /oauth/usage    │
                  │               8 states         └───────────────────────┘
                  ▼
         ┌────────────────────────────────────────┐
         │  cache  ·  health.json  ·  picks.log   │
         └───────────────────┬────────────────────┘
                             │  pick(strategy)
                             ▼
                  ┌──────────────────────┐
                  │  sticky              │
                  │  least-used          │──► AXIOM_CLAUDE_PROFILE=‹name›
                  │  round-robin         │              → claude ...
                  │  weighted            │
                  └──────────────────────┘
```

## Why this exists

If you run more than one Claude Code account — for redundancy, quota
spreading, or just personal-vs-work separation — there's nothing built-in
that picks the right one at dispatch time. Spawning against an expired,
dead, or quota-exhausted profile burns attempts on 401s and rate-limit
retries.

`roost` is a **stateless one-shot CLI primitive** that answers
"which of my profiles is healthy right now?" as an exit-code-friendly
scripting call, reading utilization numbers straight from Anthropic's
OAuth usage endpoint.

```bash
AXIOM_CLAUDE_PROFILE=$(roost pick) my-script ...
```

It works with any Claude Code OAuth account (Max / Pro / Team). On Max
plans you get the full session/weekly utilization picture; on Pro/Team
the tool still classifies health correctly (network / auth / rate-limit
states) but `usage` data is null.

## Install

Not on PyPI. Install from a local clone:

```bash
git clone https://github.com/0xDarkMatter/roost.git
cd roost
uv tool install --editable .

roost --version    # → roost 0.3.0
```

To upgrade later, pull + reinstall:

```bash
git -C roost pull && uv tool install --reinstall --editable roost
```

Or use the built-in `roost update --apply` on POSIX (Windows has a
[self-upgrade caveat](#diagnostics)).

**Why `--editable`?** It links the install to your clone, so `git pull`
picks up new code without reinstalling. The downside: deps added in a new
release require a re-sync (`uv tool install --reinstall --editable .` from
the clone), which `roost update --apply` handles automatically.

## Setup

`roost` needs more than one OAuth profile on disk to balance between
them. The standard `claude` CLI writes a single profile to
`~/.claude/.credentials.json`. `roost add` imports that file into a
multi-profile layout under `~/.claude-profiles/<name>/.credentials.json`,
which is what `roost` discovers and rotates between.

Typical onboarding:

```bash
# 1. Sign in to your first account (uses the standard Claude Code flow)
claude login

# 2. Import that credentials file as a named profile
roost add personal

# 3. Sign in to a different account (overwrites ~/.claude/.credentials.json)
claude logout && claude login

# 4. Import the new state as another named profile
roost add work

# 5. Confirm both are visible and ready
roost status
```

`roost add` copies (doesn't move) the source file, so the standard
`claude` command continues to work against whichever account you most
recently logged into.

**Want one command per profile?** Wrap it in a shell function — roost
deliberately doesn't ship its own browser-login orchestration (the
`claude` CLI owns that):

```bash
add_profile() {
  claude logout 2>/dev/null
  claude login && roost add "$1"
}

add_profile personal
add_profile work
```

**Custom locations:**

| Variable | Purpose |
|----------|---------|
| `CLAUDE_LB_PROFILES_DIR` | Override the profiles tree (default `~/.claude-profiles`) |
| `CLAUDE_CONFIG_DIR` | Single-profile fallback: if set to a dir with a direct `.credentials.json`, loaded as profile `default` |

## Quick Start

```bash
roost probe                          # Live-probe all profiles
roost status                         # Cached health table
roost pick                           # → personal  (one profile name, exit 0)
roost refresh --expired              # Refresh any profile whose token expired
eval "$(roost pick --export)"        # AXIOM_CLAUDE_PROFILE=personal in shell
roost status --json | jq '.meta'     # Machine-readable summary
```

## Commands

### Discovery

```bash
roost list                           # Names of discovered profiles
roost list --json                    # {data: [...], meta: {count}}
```

### Health

```bash
roost probe                          # Live-probe all (concurrent)
roost probe account-a                   # Live-probe one
roost status                         # Cached table + Anthropic platform-status header (stderr) + summary (stdout)
roost status --no-cache              # Ignore cache; full re-probe (incl. status.claude.com)
roost status --no-platform-status    # Skip the status.claude.com fetch
roost show account-a                    # Detail for one profile
roost invalidate account-a              # Drop this profile's cache entry
```

### Picking

```bash
roost pick                           # Default: sticky (cache-locality)
roost pick --strategy least-used     # Lowest weekly % wins
roost pick --strategy round-robin    # Rotate through profiles
roost pick --strategy weighted       # Combine weekly + session usage
roost pick --require-ok              # Exit 5 if none are ok
roost pick --export                  # Shell-sourceable VAR=value
roost pick --json                    # {data: {name, health, rationale}}
roost pick --warn-at 80              # Stderr warning when session/weekly ≥80%
roost pick --auto-refresh            # Refresh auth_expired profiles inline, then pick
roost pick --count 3                 # Return up to 3 profiles (newline-separated)
roost pick -n 3 --strategy least-used  # Short form; lowest-3 weekly%
```

`--auto-refresh` folds the `refresh --expired` preflight into `pick` itself:
any cached `auth_expired` profile with a stored refresh token is refreshed
before selection. Refresh failures are non-fatal — the expired profile is
filtered out and a healthy one is returned if any exists.

`--count N` returns up to N profiles in strategy order. If fewer than N pass
the filter ladder, returns what's available (exit 0). Each pick is logged to
`picks.log`; only the primary (first) pick updates the stickiness state, so
a subsequent single-pick call honours the primary. Stickiness is ignored when
`N > 1`. `--export` is rejected with `--count > 1` — use `--json` to consume
multiple names programmatically. The JSON shape flips to an array when
`N > 1`:

```bash
roost pick --count 3 --json
# {"data": [{...}, {...}, {...}], "meta": {"count": 3, "requested": 3, ...}}
```

### Refresh

```bash
roost refresh account-a                 # Refresh one profile's OAuth token
roost refresh --all                  # Refresh every discovered profile
roost refresh --expired              # Refresh only past-expiry profiles
roost refresh --soon 30m             # Refresh anything expiring within 30 min
roost refresh --soon 1h --json       # Cron-friendly: */15 * * * * <-- this
```

`--soon DURATION` is anticipatory: it covers already-expired tokens AND tokens
expiring within the window. Combined with a 15-min cron, it keeps the fleet
warm without `--auto-refresh` having to fire mid-spawn (with the latency cost
that implies).

`refresh` POSTs the stored refresh token to Anthropic's OAuth endpoint,
atomically rewrites `.credentials.json` (preserving non-oauth fields), and
invalidates the health cache. A cron hook like
`0 * * * * roost refresh --expired --json` keeps the fleet warm.

### Running commands

```bash
# The composed idiom: pick + refresh + dispatch in one call.
roost exec --auto-refresh -- claude --dangerously-skip-permissions "write fn"

# With explicit strategy, timeout, and retry-on-rate-limit:
roost exec --strategy least-used --timeout 300 --retry-on-429 1 -- claude ...

# Dry-run: show what would execute without talking to the API
roost exec --dry-run -- claude --help
# → AXIOM_CLAUDE_PROFILE=account-a claude --help
```

`roost exec` picks a profile, sets `AXIOM_CLAUDE_PROFILE=<name>` in the
child's env, and execs the command. The child's exit code becomes roost's
exit code, so scripts can treat `exec` as a transparent wrapper. stdin/stdout/
stderr are inherited (interactive children work unchanged). Every run is
audited to `picks.log` with duration + rc.

Key flags:

- `--auto-refresh` — inline-refresh expired tokens before picking
- `--retry-on-429 N` — if the child fails AND the profile re-probes as
  rate-limited/session-exhausted, retry once with a different profile (best-effort)
- `--timeout S` — kill after S seconds (exit 124)
- `--dry-run` — print the env + command, don't execute
- `--var-name NAME` — override `AXIOM_CLAUDE_PROFILE`
- `--log-full-argv` — log the full child argv instead of just argv[0] (may leak secrets)

### Scripting (lower-level)

```bash
# If `exec` doesn't fit, drop to pick + eval yourself:
profile=$(roost pick --auto-refresh 2>/dev/null)
case $? in
  0) export AXIOM_CLAUDE_PROFILE="$profile" ;;
  2) echo "All profiles dead or expired: run claude login --profile <name>" ;;
  5) echo "No profile is currently ok — retry or relax --require-ok" ;;
  6) echo "All profiles throttled — back off" ;;
  9) echo "All profiles exhausted — operator intervention required" ;;
esac

# Parallel dispatch to N accounts via --count
for profile in $(roost pick --count 3 --strategy least-used); do
    AXIOM_CLAUDE_PROFILE=$profile axiom launch-parcel &
done
wait
```

### History

```bash
roost history                        # Last 20 picks + exec runs
roost history --tail 50              # Last 50
roost history --profile account-a       # Filter to one profile
roost history --since 1h             # Last hour ('30m', '1h', '2d', '1w', or seconds)
roost history --json                 # Structured output
```

Reads `picks.log` (the audit trail every pick + exec writes to). Useful for
"what did the daemon dispatch in the last hour?" without grepping a
tab-separated file.

### Shell completion

```bash
roost --install-completion           # Install for your shell (bash/zsh/fish/pwsh)
roost --show-completion              # Print the script without installing
```

Once installed, `<TAB>` completes commands, flags, profile names (for `show`,
`probe`, `refresh`, `invalidate`, `history --profile`), and `--strategy`
values (`sticky | least-used | round-robin | weighted | first-healthy`).

### Diagnostics

```bash
roost probe --raw                    # Dump untouched /api/oauth/usage body
roost probe --raw --json             # Machine-readable raw dump
```

`--raw` bypasses classification and the cache write; useful for inspecting
unknown fields Anthropic might add, or for capturing test fixtures.

## Health Taxonomy

Eight states. Signals come from a single probe against `/api/oauth/usage`
plus a local check of the stored token's `expiresAt`.

| State | Detection | TTL | Next action |
|-------|-----------|-----|-------------|
| `ok` | HTTP 200 with `five_hour < 100` and `seven_day < 100`; also HTTP 403 "scope requirement user:profile" | 5 min | — |
| `rate_limited` | 429 on usage endpoint | `retry-after` s, else 60 s | Retry in N seconds |
| `session_limit` | HTTP 200 with `five_hour.utilization >= 100` | until `five_hour.resets_at` | Skip until session reset |
| `weekly_limit` | HTTP 200 with `seven_day.utilization >= 100` | until `seven_day.resets_at` | Skip until weekly reset |
| `auth_expired` | Local: stored `expiresAt` is past (no network call) | invalidates on refresh | `roost refresh <name>` |
| `auth_dead` | HTTP 401 on usage endpoint | manual | `claude login --profile <name>` |
| `network_error` | timeout / DNS / TLS / refused | 30 s | Transient; retry |
| `unknown` | other 4xx/5xx, malformed body | 60 s | Inspect with `show` |

Classification order: network exception → local-expiry → 200+utilization → 401 → 403+scope-check → 429 → unknown. See [`docs/SPEC.md`](docs/SPEC.md) §6 + [`src/claude_lb/taxonomy.py`](src/claude_lb/taxonomy.py).

## Picker Strategies

| Strategy | Behaviour |
|----------|-----------|
| `sticky` **(default)** | Stick to last-picked profile inside a 300 s window (cache-locality). Falls through to `least-used` otherwise. |
| `least-used` | Lowest `weekly_pct` wins. |
| `round-robin` | Rotate past the last-picked profile. |
| `weighted` | `weekly_pct / (session_pct + 1)` — lower is better. |
| `first-healthy` | First `ok` in discovery order. Deterministic for tests. |

Stickiness window: `--stickiness <s>` or `CLAUDE_LB_STICKINESS=<s>`. Set to `0` to disable.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Unexpected error |
| 2 | `AUTH_REQUIRED` — all profiles `auth_dead` or `auth_expired`; run `roost refresh --expired` or `claude login` |
| 3 | `NOT_FOUND` — unknown profile given to `show` / `probe` / `refresh` / `invalidate` |
| 4 | `VALIDATION` — bad flag (e.g. unknown strategy, or `refresh` with no args) |
| 5 | `FORBIDDEN` — `--require-ok` but no profile is `ok` |
| 6 | `RATE_LIMITED` — all profiles throttled |
| 7 | `CONFLICT` — `refresh` lost a lock race; another process is refreshing |
| 8 | `TIMEOUT` |
| 9 | `UNAVAILABLE` — no profiles, or all terminal-bad |

## Environment Variables

| Variable | Default | Effect |
|----------|---------|--------|
| `CLAUDE_LB_STICKINESS` | `300` | Stickiness window in seconds. `0` disables. Overridden by `--stickiness <s>`. |
| `CLAUDE_LB_PROFILES_DIR` | `~/.claude-profiles` | Absolute path to the profiles tree. Useful for testing or multi-tenant setups. |
| `CLAUDE_CONFIG_DIR` | unset | Single-profile fallback: if set to a dir containing a direct `.credentials.json`, loaded as profile `default`. |
| `XDG_CONFIG_HOME` | unset | Linux/macOS: overrides `~/.config/claude-lb/`. Windows uses `%APPDATA%\claude-lb\`. |

## Diagnostics

```bash
roost doctor                 # Check config dir, profiles, credentials, cache, network, status.claude.com
roost doctor --skip-network  # Offline variant (also skips status-page check)
roost doctor --json          # Machine-readable

roost update                 # Version + git-upstream ahead/behind check
roost update --json          # meta.update_available tells you if behind
roost update --apply         # git pull --ff-only + uv tool install --reinstall --editable
roost update --apply --no-pull  # Just re-sync deps (useful when a new dep was added locally)
```

**Windows self-upgrade caveat:** on Windows, `roost update --apply` can't
reinstall itself while running — the current process has its own `.pyd` files
memory-mapped, so `uv tool install --reinstall` hits `EACCES` on those files.
roost detects this and prints the exact workaround. Run the reinstall
from a shell that isn't roost (bash, cmd, or PowerShell):

```bash
uv tool install --reinstall --editable "X:/Forge/claude-lb"
```

POSIX platforms (macOS, Linux) are unaffected — inode-swap semantics let
`os.replace` overwrite in-use shared libraries atomically.

## Cache & Filesystem

| Purpose | Path |
|---------|------|
| Cache (Linux/macOS) | `~/.config/claude-lb/health.json` |
| Cache (Windows) | `%APPDATA%\claude-lb\health.json` |
| Pick log | `<config>/picks.log` (tab-separated, 10 MB rotation) |
| Last-pick state | `<config>/last-pick.json` |
| Platform-status cache | `<config>/platform-status.json` (60s TTL, used by `status`) |
| Profiles source | `~/.claude-profiles/<name>/.credentials.json` |

Cache writes are atomic (tempfile + `os.replace`). A credentials file's
`mtime` change implicitly invalidates its cache entry — so `claude login` or
`roost refresh` automatically refreshes the classifier on the next probe.

## Requirements

- Python 3.11+
- One or more OAuth profiles under `~/.claude-profiles/<name>/.credentials.json`
  — created via `roost add <name>` (see [Setup](#setup) above)
- Outbound HTTPS access to `api.anthropic.com`

## Monthly Overage (Max-only)

Anthropic Max plans support pay-as-you-go overage when the weekly window is
exhausted. `roost` surfaces it via `usage.extra` on every probe (Pro/Team
profiles return `usage: null` since they don't expose this surface):

```json
"extra": {
  "is_enabled": true,
  "monthly_limit": 31000,
  "used_credits": 31280.0,
  "utilization": 100,
  "currency": "AUD"
}
```

`utilization ≥ 100` means the monthly overage budget is spent — the account
still works but falls back to the hard weekly/session caps until the next
month. The status table shows an **Overage** column (colour-coded) when any
profile has overage enabled.

> **Unit caveat:** `monthly_limit` and `used_credits` are integers from
> Anthropic's API. The unit is undocumented. Empirically, the numbers only
> make sense as **currency minor units (×100)** — so `monthly_limit: 31000`
> with `currency: "AUD"` is **$310 AUD**, not $31,000 AUD. `roost`
> doesn't reinterpret these — it stores them raw and reports `utilization`
> as a percentage, which is the only field guaranteed to mean the same thing
> regardless of unit. If you need the displayed dollar amount, divide by 100
> at the call site.

## Platform status (status.claude.com)

`roost status` consults Anthropic's public status page
(`https://status.claude.com/api/v2/summary.json`) and renders a one-line
header above the profile table whenever there's a non-resolved incident
or a degraded component. When everything's operational the header is
omitted entirely. The intent is narrow: catch the case where a profile
*looks* misbehaving but the actual cause is platform-wide.

```text
· Anthropic: All Systems Operational  incident: 'Elevated errors on Claude Opus 4.7' (monitoring, minor)
┌──────────────┬──────┬─────────┬─────────┬──────────────┐
│ Profile      │ Plan │ Health  │ Session │ Weekly       │
│ ...          │      │         │         │              │
```

**Caching:** the fetch is cached at `<config>/platform-status.json` with
a 60s TTL. On cache miss the timeout is 2s. If a fresh fetch fails
(network down, Statuspage flaky, ...) the stale entry is returned
anyway — going dark during an actual incident defeats the point.

**JSON consumers:** `status --json` folds the data into
`meta.platform_status`:

```json
{
  "meta": {
    "count": 3,
    "ok": 3,
    "platform_status": {
      "indicator": "none",
      "description": "All Systems Operational",
      "active_incidents": [
        {
          "name": "Elevated errors on Claude Opus 4.7",
          "status": "monitoring",
          "impact": "minor",
          "shortlink": "https://stspg.io/..."
        }
      ],
      "degraded_components": [],
      "fetched_at": "2026-04-25T10:52:11Z",
      "age_seconds": 0,
      "fetch_error": null
    }
  }
}
```

**Opt out:** `--no-platform-status` skips the fetch entirely (useful for
scripts that don't want any extra HTTP on the hot path). `--no-cache` /
`--refresh` forces a fresh fetch alongside re-probing profiles.

`roost doctor` runs the same check as a WARN-level diagnostic — see
[Diagnostics](#diagnostics).

## Recent updates

[**Releases on GitHub**](https://github.com/0xDarkMatter/roost/releases) ·
[Full CHANGELOG](CHANGELOG.md)

### v0.3.0 — onboarding + observability

- **`roost add <name>`** — import an existing `.credentials.json` (default
  source: `~/.claude/.credentials.json`) into the multi-profile layout. Cuts
  onboarding from "manually copy files into a directory I haven't created"
  to one command.
- **`roost status` surfaces Anthropic-side incidents** above the profile table
  by polling `https://status.claude.com/api/v2/summary.json` (60s cache,
  stale-fallback). Silent when all clean; folds into `meta.platform_status`
  on `--json`. Opt out with `--no-platform-status`.
- **`roost doctor` status.claude.com check** — always-fresh, WARN-level.
  Helps distinguish "my setup is broken" from "Anthropic is degraded".
- Audience widened: any Claude Code OAuth account (Max / Pro / Team).

### v0.2.0 — exec, multi-pick, operational tooling

- **`roost exec <cmd...>`** — pick a profile, set `AXIOM_CLAUDE_PROFILE`,
  exec the command, propagate child rc. With `--auto-refresh` and
  `--retry-on-429`.
- **`roost pick --auto-refresh`** — inline-refresh expired tokens before
  picking. Folds the two-step preflight into one call.
- **`roost pick --count N` (alias `-n N`)** — multi-pick for parallel
  dispatch workflows.
- **Shell completion** (`roost --install-completion`) — bash/zsh/fish/pwsh.
- **`roost history`** — read the picks.log audit trail with `--profile`,
  `--since 30m`, `--tail N` filters.
- **`refresh --soon DURATION`** — anticipatory refresh; cron-friendly
  (`*/15 * * * * roost refresh --soon 30m --json`).

## Non-goals

- **Not a proxy.** Doesn't sit in the request path. Use
  [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) if you want that.
- **Not an interactive authenticator.** `claude login` creates the
  underlying credentials; `roost add` imports them, `roost refresh`
  extends their lifetime — the browser-based OAuth flow itself is owned by
  the `claude` CLI.
- **Not multi-provider.** Anthropic / Claude Code only.
- **Not persistent.** No daemon, no watchdog. Every invocation is one-shot.

## Forma Protocol

This tool follows the [Forma Protocol](https://github.com/forma-tools/forma):

- `--json` on every command with `{data, meta}` envelope
- Semantic exit codes (0–9)
- `stdout` = data only; `stderr` = tables, progress, warnings
- `[tool.forma]` metadata in [`pyproject.toml`](pyproject.toml)
- See [`docs/SPEC.md`](docs/SPEC.md) for the full specification

## License

MIT — see [LICENSE](LICENSE).
