# Clockify review cycle: user-systemd rollout

This is the no-sudo deployment contract for the Precision review-cycle host.
It uses the user's systemd manager, the existing user-owned Google Workspace
CLI directory, and the existing operational `runs/` and `state/` trees. It
does not copy credentials or durable evidence into a release.

## Fixed boundaries

- Release code and tracked routing are read-only at
  `/home/blackthorne/Work/automation-clockify-sync-releases/<git-sha>`.
- Durable data remains at
  `/home/blackthorne/Work/automation-clockify-sync/{runs,state}`.
- Collector checkpoint provenance is always rooted below the config's canonical
  `state_dir`. With no environment override, both the collector child and the
  coordinator's sealed-backlog verifier use
  `/home/blackthorne/Work/automation-clockify-sync/state/collector-checkpoints`.
  An explicit `CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT` is accepted only when it is
  absolute, canonical, free of symlink components, and contained by that same
  durable `state_dir`; it must never point into the immutable release. An
  existing checkpoint root must be current-user-owned with exact mode `0700`.
  When absent, the coordinator creates it as mode `0700` and fsyncs it and its
  parent before starting any child. For an absent explicit override, its
  immediate parent must already exist as a canonical, nonsymlink,
  current-user-owned, exact-mode-`0700` directory; the coordinator never
  recursively creates arbitrary descendants.
- Private config and environment files remain mode `0600` under
  `/home/blackthorne/.config/serenichron`.
- The existing `/home/blackthorne/.config/gws` directory is reused in place.
  Never copy its files into a release, environment file, log, or report.
- The user units contain no `User=` or `Group=` directives and never require
  `/etc` or `/var/lib`. `ProtectSystem=strict`, `ProtectHome=read-only`, and
  narrow `ReadWritePaths` exceptions remain enabled. The cycle's result recorder
  writes only its record and record lock; the cycle cannot replace the override.
  The updater can write releases, private configs, the source repository's
  resolved `.git` directory, and durable state; SSH files remain read-only.
- Only `deepseek-v4.1-flash:cloud` is configured. Every inherited
  `CLOCKIFY_ANALYZER_FALLBACK_*` variable is cleared by the unit. The optional
  credential-bearing Precision client environment loads before the final
  non-secret activation override.

## Separate prerequisite: lingering

Lingering is a host prerequisite, not an effect of this rollout. Verify it
before installation:

```bash
loginctl show-user blackthorne -p Linger --value
```

The result must be `yes`. Enabling lingering changes host login policy and is
therefore a separately verified administrative step; these artifacts neither
run nor hide `loginctl enable-linger`. If it is not already enabled, stop the
rollout until that step is separately authorized and read back as `yes`.

## Materialize one exact release atomically

Start from the verified principal-branch checkout after its authoritative
remote SHA has been read back. Use the full 40-character SHA; never use a
branch, tag, moving symlink, or abbreviated revision.

Create the dedicated source clone once during bootstrap:

```bash
git clone git@github.com:serenichron/automation-clockify-sync.git ~/Work/automation-clockify-sync-source
source_repo=/home/blackthorne/Work/automation-clockify-sync-source
release_sha=REPLACE_WITH_40_HEX_GIT_SHA
releases_root=/home/blackthorne/Work/automation-clockify-sync-releases
python3 "$source_repo/ops/systemd/user/clockify_review_cycle_release.py" \
  materialize \
  --source-repository "$source_repo" \
  --releases-root "$releases_root" \
  --sha "$release_sha"
```

The helper archives only that commit, requires `routing.json` and the
review-cycle entrypoint, writes `.clockify-release.json`, fsyncs the material,
makes it read-only, and renames the complete directory into place. The identity
contains a deterministic, path-sorted manifest of every extracted directory
and file (path, type, final mode, and file-content SHA-256) plus a digest of
that manifest; only the identity file itself is excluded. Preflight and
existing-release reuse recompute the complete tree, so additions, deletions,
content changes, and mode changes all fail closed even if `routing.json` is
unchanged. Runtime derives the externally expected SHA from the canonical
release-directory basename; it never bootstraps the expected value from the
identity being checked. The caller SHA, directory basename, identity
`git_sha`, config `root`/`routing`, and final override must all identify the
same release. The identity must itself be a current-user-owned, non-symlink
regular file with exact mode `0444`. It refuses noncanonical or symlinked roots and requires both the
release-root parent and private-config parents to be owned by the current user
and not group/other writable.

Read back identity without exposing private data:

```bash
release_root="$releases_root/$release_sha"
python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); print(p["git_sha"], p["root"], p["routing_sha256"], p["tree_digest"])' \
  "$release_root/.clockify-release.json"
sha256sum "$release_root/routing.json"
```

The printed SHA/root must equal the approved SHA and exact release path; the
routing digest must equal the direct `sha256sum` readback. The plan-only
preflight recomputes and verifies the complete tree manifest/digest before the
coordinator starts.

## Prepare user-owned config without copying secrets

Create the directory with mode `0700`. Build a release-specific config from
`ops/systemd/user/clockify-review-cycle.config.example.json`, replace every
`REPLACE_WITH_GIT_SHA`, and set the approved immutable workspace/member,
recovery boundary, and spreadsheet identity. Write it to a temporary sibling,
validate it as JSON, set mode `0600`, fsync it, then atomically rename it to:

```text
/home/blackthorne/.config/serenichron/clockify-review-cycle.<git-sha>.json
```

Its `root` must be the exact release directory and its `routing` must be that
directory's `routing.json`. Operational paths must continue to name the
existing `automation-clockify-sync/runs` and `automation-clockify-sync/state`.

After the release-specific config is atomically installed, bootstrap the two
private ledgers before activation or any preflight/canary. This command is
idempotent: it preserves valid existing files byte-for-byte and creates only
missing files as zero-byte, current-user-owned, exact-mode-`0600` regular files
using no-follow/exclusive creation with file and parent-directory fsyncs.

```bash
config=/home/blackthorne/.config/serenichron/clockify-review-cycle.$release_sha.json
python3 "$source_repo/ops/systemd/user/clockify_review_cycle_release.py" \
  bootstrap-ledgers --release "$release_root" --config "$config"
stat -c '%a %U %G %s %n' \
  /home/blackthorne/Work/automation-clockify-sync/state/review-corrections.jsonl \
  /home/blackthorne/Work/automation-clockify-sync/state/review-acceptance.jsonl
```

Both paths must be canonical descendants of `state_dir`. Existing symlinks,
non-regular files, wrong ownership, or any mode other than `0600` stop the
bootstrap without rewriting them. If one valid ledger already exists and the
other is absent, only the absent ledger is created. A safe partial result after
an OS-level interruption is intentionally rerunnable. The service preflight
independently requires both ledgers to be present and safe before execution;
empty ledgers load as empty lists, while malformed nonempty JSONL remains a
loader error.

Install `clockify-review-cycle.env.example` as
`~/.config/serenichron/clockify-review-cycle.env` with mode `0600`, replacing
only the pinned approved Flash revision. Keep credentials in the already
protected `precision-inference-client.env`; keep the existing `~/.config/gws`
directory in place. Verify without printing contents:

```bash
stat -c '%a %U %G %n' \
  /home/blackthorne/.config/serenichron \
  /home/blackthorne/.config/serenichron/clockify-review-cycle.env \
  /home/blackthorne/.config/serenichron/precision-inference-client.env \
  /home/blackthorne/.config/gws
```

The Serenichron private directory and private files must be user-owned mode
`0700` and `0600`, respectively. The GWS root itself must be user-owned mode
`0700`. Inside that root, existing user-owned `0755` directories and `0644`
files are accepted because the `0700` root prevents other users from traversing
to them; nested entries may be stricter, but no nested entry may be group/other
writable. Symlinks, special files, and wrong-owner entries are always rejected.
Preflight validates only and never chmods or rewrites credential material.

## Install units and select the release

Install `clockify-review-cycle.service`, `clockify-review-cycle-update.service`,
and the manual `clockify-review-cycle-canary.service` into
`~/.config/systemd/user` with mode `0644`. Do not install a timer: Multica owns
the recurring schedule and the user service owns execution.

Use the helper to atomically create the final, non-secret override. The helper
validates the release identity plus config root/routing before replacing the
pointer; a failed validation leaves the current pointer unchanged.
Activation also creates the mode-`0600` `clockify-review-cycle-override.env.lock`
beside the override. Keep this lock file in place: every override writer locks
that same inode. The cycle's result recorder takes only the record lock in
durable state, without write access to the private config directory.

```bash
override=/home/blackthorne/.config/serenichron/clockify-review-cycle-override.env
python3 "$source_repo/ops/systemd/user/clockify_review_cycle_release.py" \
  activate --release "$release_root" --sha "$release_sha" \
  --config "$config" --override "$override"
systemctl --user daemon-reload
```

The first updater-capable release must be materialized, configured, activated,
and have all three units installed once by hand before `daemon-reload`. Later
starts update automatically. These installation and activation commands require
the usual release authorization; preparing the files does not execute them.

Read back `FragmentPath`, `EnvironmentFiles`, `ExecStart`, `ReadWritePaths`,
`ProtectSystem`, `ProtectHome`, and `UMask` with `systemctl --user show` before
running either unit.

## Automatic release updates and health

Starting `clockify-review-cycle.service` pulls in
`clockify-review-cycle-update.service` with `Wants=` and waits for it with
`After=`. The updater fetches `origin/master` unattended from
the dedicated `/home/blackthorne/Work/automation-clockify-sync-source` clone
with strict SSH host checking, BatchMode, and a 120-second fetch timeout. It authenticates the
current release, materializes the exact fetched SHA, derives private configs,
and runs preflight against a temporary override before switching the pointer.
Config bytes replace the old SHA with the new one, but parsed config changes
outside `root` and `routing` are refused. A private
`clockify-review-cycle.<sha>.routing.json` sibling follows the same derivation.
The cycle reads the final override at exec time. An updater failure leaves the
cycle free to start on the active release; it is deliberately not `Requires=`.
Create the dedicated clone once with the bootstrap command above. Its optional
`ReadWritePaths=-%h/Work/automation-clockify-sync-source/.git` exception lets a
missing clone reach the updater, which skips with `fetch_failed`. The updater
loads `clockify-review-cycle.env` before the override so preflight inherits
`CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT` just as the cycle does.

Update state is the mode-`0600` file
`/home/blackthorne/Work/automation-clockify-sync/state/release-updates.json`.
It retains `previous`, `current`, `health`, pending transaction information,
`rejected` entries shaped as `{sha, reason}`, and the last 50 systemd results.
Override writers take the shared override lock; record writers take the record
lock. The updater takes both, while the cycle's result recorder takes only the
record lock. An interrupted switch is resolved against the actual override on
the next update. Do not edit this record by hand.

`ExecStopPost` records `SERVICE_RESULT`, `EXIT_CODE`, and `EXIT_STATUS`, including
preflight failures. Only `EXIT_CODE=exited` with `EXIT_STATUS=0` or `75` (the
cycle's TEMPFAIL) proves the release healthy. Runner-lock contention exits `73`
and is neutral. The release active before the first update is already proven.
Two consecutive failures (`exit-code`, `core-dump`, `timeout`, `start-limit-hit`,
or `resources`) for the current unproven SHA restore an authenticated,
non-rejected previous release, reject the failed SHA, and stop that update
without fetching. A single failure keeps the release. Neutral results, including
`73` and deliberate `signal` stops, are ignored when finding the last two
decisive results. Any healthy result prevents automatic rollback of that SHA.
Restoring a release preserves its health and clears `previous`, preventing a
second automatic rollback. If rollback authentication, config validation, or
switching is unavailable, the updater clears `previous`, logs
`clockify release rollback unavailable` to standard error, and continues the
normal fetch/forward path in the same run. An override change still stops the
update.

Only `config_drift`, `config_conflict`, `blocked_after_activation`, and
`manual_rollback` reject a SHA. Fetch, materialization, config validation, and
host preflight failures are skipped and retried on the next start. Each update
prints one `clockify release current`, `activated`, `rolled back`, or
`update skipped` line; skipped outcomes go to standard error with exit `0`.
Differing candidate config or routing bytes can be refreshed after a skipped
update only when the SHA has never been active: it is absent from `proven`,
`current`, `previous`, and recorded results. A differing config for a previously
active SHA is rejected as `config_conflict`.

The update unit has no schedule or install target and a 900-second start
budget. It does not collect evidence or publish rows. After the archive-vouching
change, measure the plan-only canary separately; raise only the host's
`clockify-review-cycle-canary.service.d/30-recovery-verification-budget.conf`
if it still exceeds 900 seconds.

## Plan-only canary, then cut over

The canary has no `[Install]` section and its `ExecStart` omits
`--enable-sheet-write`; it cannot publish review rows. Start it explicitly:

```bash
systemctl --user start clockify-review-cycle-canary.service
systemctl --user show clockify-review-cycle-canary.service \
  -p Result -p ExecMainStatus -p ExecStart -p EnvironmentFiles
journalctl --user -u clockify-review-cycle-canary.service --since today
```

Accept only exit `0` with a plan result and exact release/config identity. Exit
`2`, `75`, a timeout, a missing identity, or any attempt to publish fails the
canary.

There must be one execution owner. Before enabling the new recurring unit,
read back both legacy and new activation states. Stop and disable
`clockify-work-accounting.service`; verify it is inactive and disabled. Then
enable `clockify-review-cycle.service`. Never enable a timer for either review
unit, and never leave both accounting and review-cycle services enabled.

Multica's authoritative 07:00 trigger and its bounded follow-up checks invoke
only:

```bash
systemctl --user start clockify-review-cycle.service
```

They do not execute Python directly and do not start
`clockify-work-accounting.service`. Concurrent starts of the same oneshot are
coalesced by systemd. The cycle and canary hold the legacy runner's
`state/autopilot-runner.lock` for their complete execution, so a mistakenly
overlapping legacy process fails closed instead of duplicating collection; the
coordinator's durable `review-cycle.lock` is the second exclusion boundary.
`WantedBy=default.target` provides one recovery
start after the lingering user manager returns from a reboot; it is not a
second calendar schedule. The review cycle resumes the same immutable
receipts/debt and therefore does not recollect completed slices.

Only the recurring service has `[Install] WantedBy=default.target`. The canary
is manual-only and the updater is pulled in by the recurring service. No
`.timer` artifact exists.

## Run-directory retention

After an exit-0 cycle, `OnSuccess` starts
`clockify-runs-retention.service`. It inventories run directories, keeps direct
and transitive references, runs younger than 14 days, and H1–H5 reader holds
(autopilot results, incomplete slices, collector derivations, the latest three
names, and run reports while source coverage is absent). Top-level regular
files stay in place and supply references. The coverage default is the live
`state/source-coverage.json`, matching the legacy runner; set `coverage_file`
to its `CLOCKIFY_AUTOPILOT_COVERAGE_STATE` override if one is configured.

The default is **dry-run**. Private history and a bounded token cache live in
`state/runs-retention/`, excluded from reference matching. Scans hold no producer
locks. Deletion takes the override, release-record, and runner locks in that
order, then requires unchanged unit markers and filesystem snapshots. Missing
inputs, links, special files, scan errors, lock contention, or changed evidence
abort before deletion. A removal failure stops the pass and records deleted,
remaining, and possibly partially removed candidates. A token over 16 MiB also
aborts to bound scan memory.

One-time operator steps, after the release is authorized for integration and
activation, are as follows.

1. Integrate the change to master and let the existing updater materialize and
   activate that release. Confirm the SHA in
   `~/.config/serenichron/clockify-review-cycle-override.env` and confirm the
   selected release contains the retention script.
2. Compare installed units with the release copies, preserving host overrides.
   Install the updated `clockify-review-cycle.service` and the new
   `clockify-runs-retention.service` into `~/.config/systemd/user/` with mode
   `0644`, then run `systemctl --user daemon-reload`. Retention has no install
   target or timer.
3. Run `systemctl --user start clockify-runs-retention.service`. Inspect
   `journalctl --user -u clockify-runs-retention.service --since today` and
   `~/Work/automation-clockify-sync/state/runs-retention/inventory.json`.
   Confirm systemctl connectivity inside the sandbox, a completed dry-run,
   hold counts, candidate ages, closure, and duration. Compare with the manual
   archive inventory at `~/Work/_ops/clockify-runs-archive/20261110/`.
4. To enable deletion after reviewing that dry-run, create or update the
   user-owned mode-`0600` JSON file
   `~/.config/serenichron/clockify-runs-retention.json` with `"mode": "delete"`,
   preserving any other settings. An absent config uses built-in defaults.
   Unknown keys fail closed. `min_age_days` must be at least 7,
   `latest_keep` at least 1, and `deadline_seconds` between 1 and 1500.
   Each new release SHA requires another successful same-schema/tokenizer
   dry-run before deletion. Setting `"mode": "dry-run"` restores preview mode.

Sandbox connectivity and cold/warm performance require the operator dry-run.
Exit 75 from the cycle postpones retention until a later exit-0 cycle. Producer
locks are held only for final validation and removal; a coincident cycle can
still exit 73 on runner-lock contention.

## Roll back atomically

Keep the previous release and its release-specific config. Stop the recurring
oneshot before changing the pointer. The `rollback` command has the same strict
validation and atomic replacement as `activate`:

```bash
old_sha=REPLACE_WITH_PREVIOUS_40_HEX_GIT_SHA
old_release="$releases_root/$old_sha"
old_config=/home/blackthorne/.config/serenichron/clockify-review-cycle.$old_sha.json
systemctl --user stop clockify-review-cycle.service
python3 "$source_repo/ops/systemd/user/clockify_review_cycle_release.py" \
  rollback --release "$old_release" --sha "$old_sha" \
  --config "$old_config" --override "$override" \
  --record /home/blackthorne/Work/automation-clockify-sync/state/release-updates.json
systemctl --user daemon-reload
systemctl --user start clockify-review-cycle-canary.service
```

Passing `--record` rejects the release being left with reason `manual_rollback`,
so automatic starts do not reselect it. A newer master SHA remains eligible.
To deliberately retry a rejected SHA, clear it through the updater instead of
editing the record (this command also performs the normal update):

```bash
python3 "$source_repo/ops/systemd/user/clockify_review_cycle_release.py" update \
  --source-repository "$source_repo" --remote origin --branch master \
  --releases-root "$releases_root" --override "$override" \
  --environment /home/blackthorne/.config/serenichron/clockify-review-cycle.env \
  --credential-environment /home/blackthorne/.config/serenichron/precision-inference-client.env \
  --gws /home/blackthorne/.config/gws \
  --record /home/blackthorne/Work/automation-clockify-sync/state/release-updates.json \
  --clear-rejected REPLACE_WITH_REJECTED_40_HEX_GIT_SHA
```

A rollback to a pre-updater release makes the update unit fail with an unknown
subcommand. The recurring cycle still starts because the updater is wanted,
not required. Use an updater-capable source helper for the commands above.

After the old plan-only canary passes, start the recurring service. Do not
move, delete, restore, or copy `runs/`, `state/`, caches, receipts, GWS files,
or credentials during rollback. If the new service must be abandoned entirely,
disable it first and re-enable the legacy accounting service only after its own
exact release identity and single-owner schedule have been reverified.
