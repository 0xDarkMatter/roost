# HANDOFF: roost v0.1 implementation

> **SUPERSEDED — historical record only.** This was the build brief that
> produced v0.1 in April 2026. Everything in it either shipped or was
> deliberately dropped, and several of its "open questions" have since been
> answered (there IS a usage API — `/api/oauth/usage`; the taxonomy has nine
> states, not seven; refresh shipped in v0.2, not "deferred").
>
> **Do not treat any statement below as current.** The live docs are
> [README.md](README.md) (user-facing), [AGENTS.md](AGENTS.md) (invariants an
> agent cannot intuit), [docs/SPEC.md](docs/SPEC.md) (the specification), and
> [docs/findings.md](docs/findings.md) (empirical answers to the questions
> this document raised). Kept because the reasoning behind the original
> non-goals — no proxy, no daemon, no multi-provider — is still the clearest
> statement of what roost deliberately is not.

**From:** Axiom building lane (0xDarkMatter)
**Date:** 2026-04-24
**Target:** Fresh headless or interactive agent
**Scope:** Build `roost` v0.1 from the spec at `SPEC.md`
**Est effort:** 4–6h focused

---

## The ask

Build the `roost` CLI per [SPEC.md](SPEC.md). Deliver:

1. A pip/uv-installable Python package
2. A `roost` binary on `$PATH` after `uv tool install --editable .`
3. All minimum-viable compliance items from SPEC §13 checked
4. Green CI on Linux + macOS + Windows (3.11 / 3.12 / 3.13)
5. A `CHANGELOG.md` documenting v0.1 features
6. Self-install instructions verified on Windows (this machine) and Linux

---

## Why this exists

The Axiom orchestrator daemon spawns parcel workers using OAuth profiles at `~/.claude-profiles/<name>/.credentials.json`. Right now it round-robins with no health check — spawning against a dead profile burns one of three attempts per parcel. The hackfix was to pin the rotation via `ops/axiom-daemon.ecosystem.config.js`, manually adding/removing profiles when they die or recover. That's fragile.

`roost pick` becomes the daemon's profile source. One call per spawn, exit code scripts the fallback. Any other Claude Code Max user with multiple profiles gets the same benefit.

---

## Build environment

- Python 3.11+ (target 3.11 for compatibility; test on 3.12, 3.13)
- `uv` for env + install
- `typer`, `rich`, `httpx`, `pydantic` — see `pyproject.toml` skeleton in SPEC §12
- Testing: `pytest`, `pytest-asyncio`

Bootstrap:

```bash
cd X:/Forge/claude-lb
uv init --package --name roost       # if not already done
uv add typer rich httpx pydantic
uv add --dev pytest pytest-asyncio ruff mypy
uv tool install --editable .
roost --version                      # should work
```

---

## The non-obvious parts

### 1. Health taxonomy (SPEC §6) is the differentiator

Every other tool in this space (CLIProxyAPI, TeamClaude, vibeproxy, CCS) treats "429 means wait" and "401 means auth bad." That's two states. We need **seven**. The classification logic in `taxonomy.py` is the value-add. Write it test-first against real Anthropic 429 response fixtures. Keyword lists must be externalised and documented (not buried in match statements).

**Ask the operator** for example 429 bodies if you don't have any. They'll be captured from production. Save them under `tests/fixtures/429-responses/<scenario>.json` with a matching `<scenario>.expected.json` asserting the classified state. Each scenario = one row in the taxonomy table.

### 2. Cross-platform paths

Windows config dir is `%APPDATA%\roost\` not `~/.config/roost/`. Use `platformdirs` (add to deps) or `pathlib.Path.home() / ...` with os-sniffing — either is fine, just pick one and document.

Credentials discovery path on Windows: `C:\Users\<user>\.claude-profiles\<name>\.credentials.json`. On Linux: `~/.claude-profiles/<name>/.credentials.json`. Both should work via `Path.home() / ".claude-profiles"`.

### 3. Cache concurrency

Multiple `roost pick` invocations can race — e.g. Axiom daemon spawning two parcels 100ms apart. Atomic write-rename (`tempfile.NamedTemporaryFile` + `os.replace`) is enough. No need for OS locks.

**Reads** don't need a lock; worst case a reader sees a slightly-stale entry and we re-probe.

### 4. Token extraction

Do NOT assume the modern `.claudeAiOauth.accessToken` shape. Claude Code's credential format has drifted. See SPEC §10 for the extraction order — try each shape, degrade gracefully. Log at `--verbose` which path succeeded for each profile so drift is observable.

### 5. Don't build the proxy

Seriously: **no HTTP proxy**. We are NOT CLIProxyAPI. We are a one-shot CLI that answers "which profile?" and exits. Every invocation is stateless. The minute someone suggests a daemon, push back.

### 6. Stickiness is the default, not an afterthought

Prompt caches are per-account. Switching profile mid-workload thrashes cache and (on pay-per-token) costs ~125% input on the first request to the new account. `sticky` is the default strategy precisely because most callers want cache locality more than they want load-spread. Document this clearly in `--help` and README. A caller who genuinely wants round-robin will ask for it explicitly via `--strategy round-robin`. See SPEC §9 "Stickiness" + open question #5.

### 6. Usage API is a hope, not a guarantee

The Max plan dashboard (see operator screenshot at `X:\Forge\claude-lb\docs\screenshots\max-dashboard.png` if provided) shows session % and weekly %. It's unknown whether Anthropic exposes a public API for these numbers. If `GET /v1/organizations/usage` or similar works — great, enrich `data[].usage`. If not, leave it null and rely on 429 classification alone. **Don't block v0.1 on this.**

---

## Open questions to resolve during implementation

Please document answers in SPEC.md or a new `docs/findings.md`:

1. **What endpoint does Anthropic return for a bad OAuth token?** 401 with `error.type=authentication_error`? Confirm by temporarily munging a credentials.json and probing.
2. **Are session-limit vs weekly-limit 429 bodies actually distinguishable?** The spec assumes substring matching on messages is enough. Confirm or refine.
3. **Is there a public usage API?** Check Anthropic docs; try `GET /v1/usage`, `GET /v1/organizations/<id>/usage`, `GET /v1/me`. If yes, integrate. If no, leave the usage field null and document the finding.
4. **Does `claude login --profile <name>` successfully refresh an expired OAuth?** If yes, the v0.1 `auth_dead` state reliably surfaces via `claude login`. Confirm.
5. **Prompt cache accounting on Max plans.** On pay-per-token API keys, cache hits cost ~10% of input; misses cost 100%; writes cost ~125% for 5-min cache. On Max subscriptions, the currency is session + weekly token budgets — do cache hits count less against those budgets, full-weight, or are they uncounted? **This determines whether stickiness saves quota in addition to latency.** Check the Max FAQ / billing dashboard; if undocumented, flag it as undocumented and default to the conservative assumption (hits still count full-weight against Max quotas, so stickiness saves latency but not quota).

---

## Out of scope for v0.1

- HTTP proxying (SPEC §1 Non-goals)
- Multi-provider (OpenAI, Gemini, etc.) — roost is Anthropic-only
- OAuth token refresh (defer to v0.2; borrow from teamclaude when we pick it up)
- Usage API if it doesn't exist publicly — leave null
- GUI / menu bar / system tray (vibeproxy's space)
- Daemon mode (one-shot only)
- Windows service / systemd unit

---

## Success criteria

1. `roost pick` on this machine (3 profiles: account-a, account-b, account-c) returns a healthy profile name, exit 0, under 500ms (when cache warm) or under 10s (when probing)
2. Running Axiom's `ops/axiom-daemon.ecosystem.config.js` replaced with `AXIOM_CLAUDE_PROFILE=$(roost pick)` in a wrapper script, restarted via pm2, and the next parcel dispatch succeeds on the first attempt
3. `roost status --json | jq '.meta'` returns accurate counts matching each profile's actual state
4. CI green on GitHub Actions across 9 matrix cells (3 OS × 3 Python)
5. README documents the primitive's value clearly enough that a Claude Code user unfamiliar with Axiom understands why they'd use this

---

## What NOT to do

- Don't add a `roost daemon start` subcommand. We are not a daemon.
- Don't write a transparent proxy. CLIProxyAPI and TeamClaude already exist.
- Don't widen scope to other providers. Claude Code Max specifically.
- Don't put it under `src/axiom/`. This is a separate standalone project. If we wanted Axiom-coupling we'd have done that.
- Don't hand-roll YAML/TOML frontmatter parsing. Use stdlib `tomllib` or `PyYAML`.
- Don't use API keys for probing. OAuth tokens from profile credentials only.

---

## Deliverables structure (when complete)

```
X:/Forge/claude-lb/
├── README.md                 (exists — user-facing)
├── SPEC.md                   (exists — this document's sibling)
├── HANDOFF.md                (this file)
├── CHANGELOG.md              (NEW — v0.1 notes)
├── LICENSE                   (NEW — MIT)
├── pyproject.toml            (NEW)
├── uv.lock                   (NEW — after first uv sync)
├── src/claude_lb/
│   ├── __init__.py           (__version__ = "0.1.0")
│   ├── cli.py
│   ├── discovery.py
│   ├── probe.py
│   ├── taxonomy.py
│   ├── cache.py
│   ├── pick.py
│   ├── output.py
│   └── patterns.py
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── credentials/       (synthetic .credentials.json for discovery tests)
│   │   └── 429-responses/     (captured from live probes)
│   ├── test_taxonomy.py
│   ├── test_discovery.py
│   ├── test_cache.py
│   ├── test_pick.py
│   └── test_cli.py
├── .github/workflows/
│   └── ci.yml                 (3×3 matrix)
└── docs/
    └── findings.md            (NEW — answers to open questions above)
```

---

## Where to commit

Either:

- **Option A** — Initialise this directory as its own git repo: `git -C X:/Forge/claude-lb init -b main`, publish to `github.com/0xDarkMatter/roost` when done. **Recommended for v0.1.**
- **Option B** — Commit inline as a sibling to Axiom (monorepo-ish) if that matches the operator's preference. Ask before assuming.

---

## Timeline

- **v0.1 target:** 4–6h focused build
- **v0.1 deadline:** Before next Axiom sweep (operator will confirm window)

---

## Questions / blockers

Pigeon the Axiom building lane via:

```bash
pigeon send 'Axiom' 'roost: <your question>'
```

Or leave a `BLOCKERS.md` at repo root. Don't guess on the taxonomy — that's the whole reason to build this.

---

*HANDOFF v0.1 · 2026-04-24 · Axiom building lane.*
