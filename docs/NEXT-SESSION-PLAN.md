# v0.5.0 plan — credential rotation safety

Self-note. Picking this up cold — everything needed is in this doc.
Current state: v0.4.0 on `main`, commit `5d65d8f`.

---

## Session context

### The problem (Axiom pigeon, 2026-05-02)

Real production failure during Axiom direct trials. Sequence:

1. `roost refresh --expired` rotates `mknv74`. Writes new access_token + new refresh_token atomically.
2. Axiom copies `~/.claude-profiles/mknv74/.credentials.json` → WSL → Docker bind-mount.
3. Inside the container, `@anthropic-ai/claude-code` runs a 15–60 min trial.
4. At some point the container's SDK tries to refresh its token — but roost has since rotated
   the refresh_token again (probe cadence ~11 min). The container holds an invalidated
   refresh_token → `invalid_grant` → 401 on all subsequent `/v1/messages` calls.
5. Trial fails in <140s with 0 useful tokens. Indistinguishable from model failure.

**Direct evidence:** probing mknv74 with the freshly-rotated access token returns 429 (valid token)
but the container sees 401 — proving the SDK's refresh path is broken, not the token itself.

**Axiom workaround for now:** snapshot the credentials.json at trial start into a temp path,
set `AXIOM_HOST_CLAUDE_CREDENTIALS` to the snapshot, never re-read roost's live file.

### Root cause

OAuth token rotation is one-shot: POST refresh_token → Anthropic invalidates it, returns
new refresh_token + access_token. Any consumer holding the old refresh_token is now broken.
Roost's atomic write guarantees the file is never half-written, but doesn't prevent a consumer
from reading the file *between* two rotations and getting the old refresh_token.

---

## Implementation plan

### Priority 1 (ship v0.5.0): `roost lease`

Smallest viable fix that gives consumers protection for long-lived workloads.

**Contract:**
```bash
roost lease mknv74 --for 30m
# stdout: lease-id (e.g. "lease-mknv74-1234567890")
# exit 0 on success
# exit 3 if profile not found
# exit 1 if already leased (with stderr saying which lease holds it + TTL)

roost release lease-mknv74-1234567890
# exit 0 always (idempotent; no-op if lease already expired)

roost lease list
# table: profile, lease-id, expires-at, created-by (argv0 of leaseholder)
```

**Behaviour:**
- Lease is stored in `<config>/leases.json` (atomic write, same pattern as health.json).
- While a lease is active on a profile, `roost refresh <name>` returns exit 7 (CONFLICT)
  with a stderr message "profile <name> is leased until <expires_at> by <creator>".
  The credentials are NOT touched.
- `roost probe <name>` still runs (freshness, not state). Only refresh is blocked.
- `roost pick --auto-refresh` respects the lease: if the chosen profile is leased and
  auth_expired, falls through to the next candidate instead of refreshing.
- Leases expire automatically. The check is done at read time (no daemon needed).
- `roost doctor` reports any expired-but-not-released leases as INFO (housekeeping prompt).

**`roost exec` extension:**
Add `--lease` flag (default: on) to auto-lease the picked profile for the child's lifetime.
The lease TTL is set to `--timeout` (if given) or 30m (heuristic). Lease is released in a
`finally` block so Ctrl+C also cleans up.

```bash
roost exec --auto-refresh -- claude "long task"
# Internally: pick → lease(picked, 30m) → child → release
```

`--no-lease` to opt out for short-lived children.

**Implementation sketch:**

New module `src/claude_lb/lease.py`:
```python
@dataclass
class Lease:
    lease_id: str
    profile: str
    expires_at: datetime
    created_at: datetime
    creator: str  # sys.argv[0]

def acquire(profile: str, duration_s: int) -> Lease: ...
def release(lease_id: str) -> bool: ...          # returns False if not found
def get_active(profile: str) -> Lease | None: ... # None if no active lease
def list_leases() -> list[Lease]: ...
def purge_expired() -> int: ...                   # returns count purged
```

Storage: `<config>/leases.json` — dict keyed by lease_id, atomic write.

CLI additions in `cli.py`:
- `roost lease <profile> [--for DURATION]` — duration defaults to "30m", parses "Nm"/"Nh"/"Ns".
- `roost release <lease-id>` — idempotent.
- `roost lease list [--json]`

`refresh.py` check: call `get_active(profile)` at the top of `refresh_profile`; if active,
return `RefreshResult(error_code="LEASE_HELD", error_message=...)`.

**Tests to add:**
- `test_lease_acquire_and_release` — acquire, confirm active, release, confirm gone.
- `test_lease_blocks_refresh` — acquire lease, attempt refresh, assert LEASE_HELD result.
- `test_lease_expired_is_transparent` — acquire with 1s TTL, sleep 2s, attempt refresh → succeeds.
- `test_lease_auto_release_on_exec_exit` — stub child, confirm lease is released in finally.
- `test_lease_pick_auto_refresh_skips_leased_expired` — leased profile is auth_expired,
  auto-refresh picks the next candidate instead.
- `test_lease_list_filters_expired` — list only returns active leases.

---

### Priority 2 (v0.5.0 or v0.5.1): `roost snapshot <profile> <out-path>`

Smallest possible improvement for Axiom's manual workflow:

```bash
roost snapshot mknv74 /tmp/axiom-trial-creds-123.json
# Copies the profile's credentials.json to out-path.
# Prints a warning to stderr: "snapshot will not be updated by roost"
# exit 0

# JSON mode — also surfaces the profile's current health for sanity check
roost snapshot mknv74 /tmp/... --json
# { "data": { "path": "...", "profile": "mknv74", "health": "ok", ... }, "meta": {...} }
```

This doesn't prevent the race; it just documents intent (this is a point-in-time copy)
and makes it one command instead of a manual `cp`.

**Note:** snapshot does NOT acquire a lease. If the caller wants rotation protection
during the snapshot's lifetime, they need `roost lease` separately.

---

### Priority 3 (future): probe endpoint parity check

`roost doctor --use-test` mode that POSTs a 1-token `/v1/messages` with the oauth-beta
header — same auth path the consumer uses — and surfaces any probe-OK-but-use-fails gap.

Deferred: costs a real API call. Opt-in only. Lower priority now that lease solves
the immediate rotation problem.

---

### Priority 4 (future): `.credentials.version` monotonic counter

Sibling file to `.credentials.json`, updated atomically alongside it. Consumers that
snapshot the file can detect rotation by re-reading the version. More robust than mtime.

Deferred until there's a second consumer type that needs it (Axiom's snapshot approach
is sufficient for now).

---

## Cross-cutting work

### SPEC.md
- §2 Command Architecture — add `lease`, `release`, `snapshot`.
- §4 Exit Codes — `LEASE_HELD` maps to exit 7 (reuses CONFLICT semantics; rename to
  `LOCK_CONFLICT` in docs to cover both file-lock and lease conflicts).

### README.md
- New "Credential rotation safety" section explaining the rotation race and when to use lease.
- Add `lease` / `release` / `snapshot` to the command reference table.

### AGENTS.md
- New rule: while a profile is leased, `refresh` is blocked. The `pick --auto-refresh` path
  filters leased-and-expired profiles to the next candidate rather than attempting a refresh.
- Extend rule 14 (refresh race): lease is the user-facing escape hatch; document that leases
  serialize rotation correctly but consumers must still use `snapshot` or `exec --lease`
  to avoid the initial-read race.

### CHANGELOG.md
- Single v0.5.0 entry covering lease + release + snapshot + exec --lease.

### Pigeon reply to Axiom
Send a reply to message 109 when lease ships:
```
roost 0.5.0 shipped with `roost lease / release / snapshot` + `exec --lease` (default on).
Upgrade: uv tool install --reinstall --editable "X:/Forge/claude-lb"
Lease a profile explicitly: LEASE_ID=$(roost lease mknv74 --for 30m --json | jq -r .data.lease_id)
roost exec auto-leases for exec's child lifetime — your trial worktree can drop the manual snapshot.
```

---

## Ordering for the actual session

1. `src/claude_lb/lease.py` — storage + acquire/release/list/purge. Tests first.
2. `cli.py` wiring for `lease`, `release`, `lease list`. CLI tests.
3. Hook lease check into `refresh.py::refresh_profile`. Add LEASE_HELD tests.
4. `pick --auto-refresh` respects leases — test skips leased+expired profile.
5. `exec --lease` (default on) — auto-lease for child lifetime. Tests.
6. `roost snapshot` — tiny new subcommand. Tests.
7. Docs pass: SPEC, README, AGENTS, CHANGELOG.
8. Bump to v0.5.0. Pigeon reply to Axiom.

---

## References in the existing codebase

- `src/claude_lb/cache.py` — pattern to follow for atomic JSON storage
- `src/claude_lb/refresh.py::refresh_profile` — where LEASE_HELD check goes
- `src/claude_lb/pick.py::pick` — where auto-refresh falls-through logic lives
- `src/claude_lb/exec_cmd.py` — extend with auto-lease
- `src/claude_lb/paths.py` — add `leases_path()` following existing platform-aware pattern
- `src/claude_lb/cli.py::profiles_pick` — `--auto-refresh` wiring to reference

---

*Planned 2026-05-03 · Current state v0.4.0 · Target v0.5.0.*
*Source: Axiom pigeon #109 (wizardly-antonelli, 2026-05-02).*
