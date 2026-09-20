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
  the three narrow `ReadWritePaths` exceptions remain enabled.
- Only `deepseek-v4-flash:cloud` is configured. Every inherited
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

```bash
source_repo=/home/blackthorne/Work/automation-clockify-sync
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

Copy only the two reviewed files into `~/.config/systemd/user` with mode
`0644`. Do not install a timer: Multica owns the recurring schedule and the
user service owns execution.

Use the helper to atomically create the final, non-secret override. The helper
validates the release identity plus config root/routing before replacing the
pointer; a failed validation leaves the current pointer unchanged.

```bash
override=/home/blackthorne/.config/serenichron/clockify-review-cycle-override.env
python3 "$source_repo/ops/systemd/user/clockify_review_cycle_release.py" \
  activate --release "$release_root" --sha "$release_sha" \
  --config "$config" --override "$override"
systemctl --user daemon-reload
```

Read back `FragmentPath`, `EnvironmentFiles`, `ExecStart`, `ReadWritePaths`,
`ProtectSystem`, `ProtectHome`, and `UMask` with `systemctl --user show` before
running either unit.

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
coalesced by systemd. Both new units hold the legacy runner's
`state/autopilot-runner.lock` for their complete execution, so a mistakenly
overlapping legacy process fails closed instead of duplicating collection; the
coordinator's durable `review-cycle.lock` is the second exclusion boundary.
`WantedBy=default.target` provides one recovery
start after the lingering user manager returns from a reboot; it is not a
second calendar schedule. The review cycle resumes the same immutable
receipts/debt and therefore does not recollect completed slices.

Only the recurring service has `[Install] WantedBy=default.target`. The canary
is manual-only. No `.timer` artifact exists.

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
  --config "$old_config" --override "$override"
systemctl --user daemon-reload
systemctl --user start clockify-review-cycle-canary.service
```

After the old plan-only canary passes, start the recurring service. Do not
move, delete, restore, or copy `runs/`, `state/`, caches, receipts, GWS files,
or credentials during rollback. If the new service must be abandoned entirely,
disable it first and re-enable the legacy accounting service only after its own
exact release identity and single-owner schedule have been reverified.
