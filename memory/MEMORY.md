# roost — session memory

Auto-loaded by `/sync`. Notes that should survive across sessions.

## Standing operator directives

- **No `git push` without explicit approval.** Local commits, branches, and tags are fine. Pushing requires the operator to say so for THIS push — prior approvals don't carry forward. See AGENTS.md rule 19 for the canonical statement.
- **Use `uv` (not `pip`) for everything.** Editable install: `uv tool install --reinstall --editable "X:/Forge/claude-lb"`.
- **Run reinstalls from a non-roost shell on Windows.** The tool can't overwrite its own `.pyd` files while running. `roost update --apply` detects this and prints the workaround; don't try to "force" past it.

## Current state (last update: 2026-08-04)

- **Version:** v0.5.0 on `main`, plus an unreleased batch (see below). The
  tool was **renamed `claude-lb` → `roost`**; `paths.py` migrates the old
  `~/.config/claude-lb` config dir on first resolution. The Python package is
  still `src/claude_lb/` — only the CLI name and config dir moved.
- **Remote:** `origin` = https://github.com/0xDarkMatter/roost (private). Tags pushed: v0.1.0, v0.2.0, v0.3.0, v0.4.0, plus `hackathon-submission` (annotated, points at the v0.3.0 squash commit). **Nothing since v0.5.0 has been pushed** — see the standing no-push directive above.
- **Tests:** 871 mocked + 28 live (`uv run pytest` for the mocked suite, `uv run pytest -m live` for the integration suite). All green at last check.
- **Live fleet:** 4 profiles under `~/.claude-profiles/` — `agent-01`,
  `evolution7`, `mknv74`, `roamhq`. All `max` plan. (Earlier notes said three
  named `account-a/b/c`; that was wrong.) Variety of overage/usage states —
  useful as a real-world testbed for `pytest -m live`.

### [Unreleased] Fable capacity + model_limit (2026-08-04)

The `/api/oauth/usage` shape changed under us and roost did not notice —
worth remembering as a class of failure, not just an incident.

- **`seven_day_opus` / `seven_day_sonnet` are `null` on every profile now.**
  Model-scoped capacity moved to a top-level `limits[]` array. **Fable** is
  the model there today. Roost kept reporting `null` per-model usage as
  though the accounts had no data — silent degradation, no error, for weeks.
- **`Health.MODEL_LIMIT` is the ninth state.** A scoped limit exhausts
  independently of the aggregate weekly window: mknv74 read `weekly_all` 76%
  with Fable at 90%. Before this, roost called that `ok` and picked it.
  Weekly still beats model when both fire.
- **`is_active` means "the constraint currently binding", NOT "enforced".**
  Exactly one limit per profile carries it. The classifier gates on it;
  reporting must not (filtering the display makes a genuine 0% read as "no
  data"). This distinction caused a real bug — see AGENTS.md.
- New surfaces: `roost widget` (self-contained HTML fleet dashboard for
  `show_widget`, styled to match fleetflow's ff-monitor), `roost status
  --cards` (terminal cards via `term.py`), `report --metric fable_pct |
  spend_pct`, and a `usage_field_drift` doctor check.
- Built via a fleetflow run (`fable`) — 9 lanes, file-disjoint, orchestrator
  owned all `cli.py` wiring. A Codex refuter lane found four real defects
  including a dropped `model_reset_at` in a seam no lane owned.
- **Tests: 934 mocked + 28 live.** Nothing pushed — 36 commits ahead of
  `origin/main`.

### The widget went through heavy design iteration (2026-08-04)

Most of the operator's feedback was about *honesty of display*, and the
resulting invariants are now AGENTS.md rules 33–39. The ones most likely to
be undone by accident:

- **The byte budget sheds detail before it sheds a profile** (rule 33). It
  shipped the other way round and silently rendered three cards for a
  four-profile fleet. Bars are the CSS default, so dropping bar markup
  without the `rw-sq-only` class leaves cards with *no* gauges.
- **The Bars/Grid toggle is CSS-only on purpose** (rule 34) — that is what
  keeps "no `<script>`" structurally true. The checkbox must precede what it
  restyles; the service rows and `.rw-strips` must stay siblings. Both break
  silently, with no error.
- **Square grids are gradient-painted on whole-pixel stops** (rule 35).
  Percentages made them fluid and antialiased every edge.
- **Exec failure rate and median were removed as misleading** (rule 37).
  Don't re-add them without a recency window — `rc` is the child's exit code.

## Standing design calls from the operator

- Title case for widget labels, never uppercase micro-labels (fleetflow uses
  uppercase; roost deliberately does not).
- The status panel is **Claude Status**, never "Anthropic".
- Reset times are absolute local wall-clock ("Resets Sat 2:00 PM"), relative
  only inside the last hour. "in 144h" answers nothing.
- **v0.3.0 highlights:** platform-status awareness — `status` and `doctor` consult `https://status.claude.com/api/v2/summary.json`, surfaced as a one-line stderr header above the `status` table when there's a non-resolved incident. Cache at `<config>/platform-status.json`, 60s TTL with stale-fallback. Shared module `src/claude_lb/platform_status.py`. AGENTS.md rule 20 codifies "best-effort, never load-bearing".

### [Unreleased] Tier 1–4 batch (2026-04-27)

Built and tested in one session. Six phases, ~28 new mocked tests per
phase + 28 live tests. CHANGELOG `[Unreleased]` has the complete list.
Highlights worth remembering:

- **Symmetric API**: `roost remove`, `roost rename`, `roost which` (read-only `pick`).
- **Pick filters**: `--avoid` (repeatable), `--max-cost <pct>`, `--fallback <name>`, `--explain` (decision tree to stderr or fold into JSON `data.explain`).
- **New strategy**: `lowest-overage` — minimum monthly overage utilization wins. Profiles without overage data treat as 0 (best).
- **Observability**: opt-in `usage-log.ndjson` (toggle via `roost config usage-log on` or `CLAUDE_LB_USAGE_LOG=1`). `roost stats` aggregates `picks.log`; `roost report` aggregates the usage log with sparklines + linear burn-rate projection.
- **Reliability**: per-profile exponential `network_error` backoff (30s → 60s → 120s → 240s → 480s, capped). New `ProfileHealth.consecutive_failures` field — Pydantic default 0 keeps backwards-compat with v0.3.0 cache files. `refresh --jitter <s>` for cron-spread.
- **Integration surfaces**: `roost shellinit` (bash/zsh/fish/pwsh templates), `roost trace` (verbose probe with token redaction), `roost top` (Rich `Live` TUI).
- **Live tests**: `tests/test_live.py` marked `pytest.mark.live`, default-skipped via `addopts = "-m 'not live'"`. Run explicitly with `pytest -m live`.
- **AGENTS.md** gained 6 new rules (21–26) covering the new surfaces — read these before extending any of the new features.

## Known traps that already cost time

1. **Auto-refresh stale-Profile bug** (fix in `_attempt_auto_refresh`, commit `ae03041`): after `refresh_many_sync` mutates disk, you MUST re-discover the Profile before re-probing — otherwise `_local_auth_expired` short-circuits on the stale `access_token_expires_at` and silently undoes the refresh. Tests that stub `probe_many_sync` at the top level miss this; the regression test (`test_auto_refresh_rediscovers_profile_after_refresh`) seeds the disk and asserts the second probe sees a post-refresh mtime.
2. **Windows EACCES on self-upgrade** (handled): `roost update --apply` from within the uv-tool install can't overwrite its own mapped `.pyd` files. `updater._would_self_lock()` detects + emits the workaround. Never bypass.
3. **CI workflow file** removed (commit `1a700c9`): pushing the workflow needs the gh token's `workflow` scope. Don't re-add `.github/workflows/` until CI is actually wanted.
4. **JSON-shape vs in-process-shape mismatch** (fixed 2026-08-04). The CLI
   passes the status payload to renderers **in-process**, where
   `build_status_payload` hand-formats top-level timestamps to ISO strings but
   dumps nested structures (`usage.limits[]`) wholesale — so their `resets_at`
   is still a `datetime`. A `_parse_dt` that accepted only `str` silently
   dropped every model-scoped reset line while the whole test suite stayed
   green, because the tests fed JSON-shaped strings. **If you write a renderer
   that consumes "the `--json` shape", test it against the in-process payload
   too** — they are not the same object.
5. **Windows console codepage eats panel glyphs** (fixed 2026-08-04). Both
   stdout and stderr default to cp1252/cp437, which cannot encode `•`, `—`, or
   the box-drawing runs — they arrive as U+FFFD, including when redirected to
   a file. `term.ensure_utf8(stream)` upgrades the stream; `emit_text` and
   `emit_panel` both call it. The ASCII fallback (`TERM_ASCII=1`) is for
   terminals that genuinely cannot display Unicode — don't reach for it to
   paper over an encoding default.

## Useful one-liners

```bash
# Reinstall (run from any shell that ISN'T roost)
uv tool install --reinstall --editable "X:/Forge/claude-lb"

# Full mocked test suite (default: 723 tests, live skipped)
uv run pytest

# Live integration tests (28 tests against real fleet)
uv run pytest -m live

# Spot-check the live install
roost status --json | jq '.data[] | {name, health, weekly_pct: .usage.weekly_pct}'
roost history --tail 10
roost refresh --soon 1h --json

# New observability stack
roost config usage-log on            # one-time opt-in
roost probe                          # appends to usage-log.ndjson
roost report --metric weekly_pct --sparkline --project
roost stats --since 1d

# New decision-debugging
roost which --explain                # what would pick choose, why?
roost pick --avoid account-a --fallback account-c

# New shell integration
eval "$(roost shellinit)"           # bash/zsh; fish/pwsh have own pipe forms
```

## Deferred ideas (not yet wanted)

Most of the original deferred list shipped in the [Unreleased] batch. Still
worth not building without a concrete driver:

- `exec --concurrent N` (compose with `pick --count` for parallel dispatch)
- Detached-process pattern for Windows `update --apply` (current "print workaround" is fine)
- `--raw` schema diff (warn on unknown response fields from Anthropic)
- PyPI publish (still install-from-clone only; pyproject ready)
- Standalone binary via pyinstaller (separate distribution decision)
- `cli.py` refactor — currently ~2900 lines; works fine, would only be hygiene
- Snapshot tests for Rich tables (cosmetic-regression coverage)

Add when there's a concrete driver, not before.
