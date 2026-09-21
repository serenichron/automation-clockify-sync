from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import runpy
import stat
import sys
import tempfile
import unittest
from unittest import mock

from scripts import clockify_sync_collect as collector
from scripts import collector_receipts
from scripts import clockify_source_debt_recover as recovery
from scripts import clockify_review_run as review
from scripts import clockify_sheet_publish as publisher
from scripts import work_accounting_pipeline as pipeline


TZ = dt.timezone(dt.timedelta(hours=3))
SINCE = dt.datetime(2026, 7, 1, tzinfo=TZ)
UNTIL = dt.datetime(2026, 7, 2, tzinfo=TZ)
SOURCE = "peer/macbook"
ATTEMPT_1 = "sha256:" + "1" * 64
ATTEMPT_2 = "sha256:" + "2" * 64
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecoverySheetGateway:
    """In-memory publisher boundary with real scan, dedupe, and readback."""

    def __init__(self) -> None:
        self.rows: list[list[object]] = [list(publisher.HEADER)]

    def spreadsheet(self, _spreadsheet_id):
        return {"sheets": [{"properties": {
            "title": "September 2026 review",
            "sheetId": 7,
            "gridProperties": {"rowCount": 1000},
        }}]}

    def values(self, _spreadsheet_id, range_name):
        start = int(range_name.rsplit("!A", 1)[1].split(":", 1)[0])
        end = int(range_name.rsplit("O", 1)[1])
        return [list(row) for row in self.rows[start - 1:end]]

    def duplicate_sheet(self, *_args):
        raise AssertionError("existing test Sheet must not be duplicated")

    def prepare_sheet(self, *_args):
        return None

    def clear_values(self, *_args):
        raise AssertionError("existing test Sheet must not be cleared")

    def update_values(self, _spreadsheet_id, ranges):
        if ranges:
            raise AssertionError("identical replay must not update existing rows")

    def append_values(self, _spreadsheet_id, _range_name, rows):
        self.rows.extend(list(row) for row in rows)

    def prepare_new_rows(self, *_args):
        return None


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class SourceDebtRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runs = self.root / "runs"
        self.checkpoints = self.root / "state" / "collector-checkpoints"
        self.routing = {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
        self.fleet = {
            "machines": [{"name": "macbook", "enabled": True, "kind": "ssh"}],
            "ssh_options": [],
        }
        (self.root / "routing.json").write_text(json.dumps(self.routing) + "\n")
        (self.root / "fleet.json").write_text(json.dumps(self.fleet) + "\n")
        self.patches = [
            mock.patch.object(collector, "ROOT", self.root),
            mock.patch.object(collector, "RUNS", self.runs),
            mock.patch.object(recovery, "ROOT", self.root),
            mock.patch.object(recovery, "RUNS", self.runs),
            mock.patch.object(review, "ROOT", self.root),
            mock.patch.object(review, "RUNS", self.runs),
            mock.patch.dict(
                collector.os.environ,
                {
                    "CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(self.checkpoints),
                    "CLOCKIFY_AUTOPILOT_COORDINATOR": "omarchy-precision",
                },
            ),
            mock.patch.object(collector, "fetch_clockify", return_value={"status": "ok", "complete": True, "entries": []}),
            mock.patch.object(collector, "fetch_fathom", return_value={"status": "ok", "complete": True, "meetings": []}),
            mock.patch.object(collector, "fetch_multica_issues", return_value={"status": "ok", "complete": True, "issues": []}),
            mock.patch.object(collector, "machine_is_local", return_value=False),
            mock.patch.object(collector, "collector_runtime_identity", return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture", "dirty": False}),
            mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_direct_run_rejects_symlink_and_lexical_aliases(self) -> None:
        run_dir = self.runs / "source"
        run_dir.mkdir(parents=True)
        alias = self.runs / "alias"
        alias.symlink_to(run_dir, target_is_directory=True)
        lexical = self.runs / "nested" / ".." / "source"
        for candidate in (alias, lexical):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(
                recovery.SourceDebtRecoveryError, "canonical"
            ):
                recovery._direct_run(candidate, label="recovery run")

    def test_immutable_attempt_marker_fsyncs_file_then_parent_directory(self) -> None:
        """A durable marker must persist both its bytes and directory entry."""
        marker = self.root / "attempt" / "attempt-marker.json"
        synced: list[str] = []
        real_fsync = recovery.os.fsync

        def observe_fsync(descriptor: int) -> None:
            synced.append(
                "directory"
                if stat.S_ISDIR(recovery.os.fstat(descriptor).st_mode)
                else "file"
            )
            real_fsync(descriptor)

        with mock.patch.object(recovery.os, "fsync", side_effect=observe_fsync):
            recovery._write_immutable_json(marker, {"schema_version": "fixture/v1"})

        self.assertEqual(["file", "directory"], synced)

    def test_primary_write_error_survives_unlink_cleanup_failure(self) -> None:
        """Cleanup failure must not hide the write error that made the marker unsafe."""
        marker = self.root / "attempt" / "attempt-marker.json"
        with (
            mock.patch.object(
                recovery.os, "write", side_effect=OSError("primary write failure")
            ),
            mock.patch.object(
                Path, "unlink", side_effect=PermissionError("unlink cleanup failure")
            ),
        ):
            with self.assertRaisesRegex(OSError, "primary write failure"):
                recovery._write_immutable_json(
                    marker, {"schema_version": "fixture/v1"}
                )

    def test_primary_write_error_survives_descriptor_close_failure(self) -> None:
        """Descriptor cleanup failure must not replace the primary write error."""
        marker = self.root / "attempt" / "attempt-marker.json"
        opened: list[int] = []
        real_open = recovery.os.open
        real_close = recovery.os.close

        def observe_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        try:
            with (
                mock.patch.object(recovery.os, "open", side_effect=observe_open),
                mock.patch.object(
                    recovery.os, "write", side_effect=OSError("primary write failure")
                ),
                mock.patch.object(
                    recovery.os, "close", side_effect=OSError("close cleanup failure")
                ),
                mock.patch.object(recovery, "_fsync_directory"),
            ):
                with self.assertRaisesRegex(OSError, "primary write failure"):
                    recovery._write_immutable_json(
                        marker, {"schema_version": "fixture/v1"}
                    )
        finally:
            for descriptor in opened:
                try:
                    real_close(descriptor)
                except OSError:
                    pass

    def test_existing_marker_retry_repeats_parent_directory_fsync(self) -> None:
        """A prior directory-fsync failure must remain retryable without rewriting."""
        marker = self.root / "attempt" / "attempt-marker.json"
        value = {"schema_version": "fixture/v1"}
        with mock.patch.object(
            recovery,
            "_fsync_directory",
            side_effect=[OSError("directory fsync failure"), None],
        ) as sync_directory:
            with self.assertRaisesRegex(OSError, "directory fsync failure"):
                recovery._write_immutable_json(marker, value)
            recovery._write_immutable_json(marker, value)

        self.assertEqual(2, sync_directory.call_count)
        self.assertEqual(value, json.loads(marker.read_text(encoding="utf-8")))

    def test_preserved_partial_fsyncs_parent_after_atomic_rename(self) -> None:
        """A preserved crash partial must survive with its deterministic inventory."""
        run_dir = self.runs / "source-debt-recovery-fixture"
        run_dir.mkdir(parents=True)
        (run_dir / "partial.json").write_text("{}\n", encoding="utf-8")
        synced: list[str] = []
        real_fsync = recovery.os.fsync

        def observe_fsync(descriptor: int) -> None:
            synced.append(
                "directory"
                if stat.S_ISDIR(recovery.os.fstat(descriptor).st_mode)
                else "file"
            )
            real_fsync(descriptor)

        with mock.patch.object(recovery.os, "fsync", side_effect=observe_fsync):
            recovery._preserve_partial(run_dir)

        self.assertEqual(["directory"], synced)
        self.assertFalse(run_dir.exists())
        self.assertEqual(1, len(list(self.runs.glob("source-debt-recovery-fixture-incomplete-*"))))

    @staticmethod
    def failed_peer() -> dict[str, object]:
        return {
            "machine": "macbook", "status": "error", "complete": False,
            "collector_contract": "canonical_export_v1", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "unavailable",
            "errors": ["bounded peer timeout"],
        }

    @staticmethod
    def healthy_peer() -> dict[str, object]:
        return {
            "machine": "macbook", "status": "ok", "complete": True,
            "collector_contract": "canonical_export_v1", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "complete", "errors": [],
        }

    @staticmethod
    def session_only_peer() -> dict[str, object]:
        return {
            "machine": "macbook", "status": "ok", "complete": True,
            "collector_contract": "canonical_export_v1", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "unavailable",
            "errors": [],
        }

    def make_terminal_recovery(
        self, parent: Path, source: str, attempt: str,
        *, peer: dict[str, object], seal: bool = True,
    ) -> tuple[Path, collector_receipts.SliceCompletionBundle]:
        with mock.patch.object(collector, "collect_remote_sessions", return_value=peer):
            derived = recovery.recover(parent, source, attempt).run_dir
        review._snapshot_recovery_inputs(derived, parent)
        for name in (
            "semantic-analysis.json", "work-accounting-result.json",
            "quality_report.json", "review-snapshot.json",
        ):
            (derived / name).write_text("{}\n")
        bundle = review._finalize_recovery_completion(derived)
        transition = json.loads((derived / "run-report.json").read_text())[
            "source_debt_recovery"
        ]
        status = review._recovery_source_status(bundle, source)
        (derived / "autopilot-result.json").write_text(json.dumps({
            "quality_status": "pass",
            "completion_bundle_digest": bundle.bundle_digest,
            "source_debt_recovery": {
                "source": source, "attempt_id": attempt, "status": status,
                "transition_digest": transition["transition_digest"],
            },
        }) + "\n")
        if seal:
            recovery.seal_recovery_receipt(derived)
        return derived, bundle

    def make_parent(self) -> Path:
        slices = collector.plan_slices(SINCE, UNTIL, zone=collector.BUCHAREST)
        compatibility = collector._backlog_compatibility_version(
            self.routing, self.fleet, calendly_optional=True,
            coordinator="omarchy-precision",
        )
        identity = collector.BacklogIdentity(
            since_utc=collector.iso_utc(SINCE), until_utc=collector.iso_utc(UNTIL),
            timezone=collector.BUCHAREST.key, max_days=2,
            compatibility_version=compatibility,
        )
        store = collector.BacklogStore(self.checkpoints)
        state = store.open(identity, slices)
        parent = collector._slice_run_dir(slices[0], compatibility)
        with mock.patch.object(collector, "collect_remote_sessions", return_value=self.failed_peer()):
            collector._collect_slice(
                argparse.Namespace(enrich=False, calendly_optional=True),
                self.routing, self.fleet, {"_missing": True}, {"_missing": True},
                SINCE, UNTIL, "fixture", collector.PageCheckpointStore(state.directory / "source-checkpoints"),
                parent, calendly_env={"_missing": True}, coordinator="omarchy-precision",
            )
        collector._write_pending_slice_finalization(parent, identity, slices[0])
        for name, content in {
            "period-manifest.json": b"{\"schema_version\":1}\n",
            "routing.json": (self.root / "routing.json").read_bytes(),
            "review-corrections.jsonl": b"",
            "review-acceptance.jsonl": b"",
            "semantic-analysis.json": b"{}\n",
            "work-accounting-result.json": b"{}\n",
            "quality_report.json": b"{\"status\":\"pass\"}\n",
            "review-snapshot.json": b"{}\n",
        }.items():
            (parent / name).write_bytes(content)
        bundle = collector_receipts.build_completion_bundle(parent, slice_=slices[0])
        bundle_path = parent / "completion-bundle.json"
        collector_receipts.write_completion_bundle(bundle_path, bundle)
        store.record_complete(
            state, slices[0].slice_id, bundle_path.resolve(),
            "sha256:" + hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        )
        return parent

    def test_distinct_attempt_collects_once_reuses_and_preserves_parent(self) -> None:
        """Returning the parent or recollecting a completed attempt is a bug."""
        parent = self.make_parent()
        before = tree_hashes(parent)
        with mock.patch.object(
            collector, "collect_remote_sessions", return_value=self.healthy_peer()
        ) as transport:
            first = recovery.recover(parent, SOURCE, ATTEMPT_1)
            second = recovery.recover(parent, SOURCE, ATTEMPT_1)
            third = recovery.recover(parent, SOURCE, ATTEMPT_2)

        self.assertNotEqual(parent, first.run_dir)
        self.assertEqual(first.run_dir, second.run_dir)
        self.assertNotEqual(first.run_dir, third.run_dir)
        self.assertEqual(2, transport.call_count)
        self.assertEqual(before, tree_hashes(parent))
        transition = json.loads((first.run_dir / "run-report.json").read_text())["source_debt_recovery"]
        self.assertEqual(SOURCE, transition["source"])
        self.assertEqual(ATTEMPT_1, transition["attempt_id"])

    def test_recovery_collects_peer_once_and_binds_rebuilt_ledger(self) -> None:
        """Only bundle-bound ledger events may cross the parent boundary."""
        parent = self.make_parent()
        before = tree_hashes(parent)
        recovered_peer = self.healthy_peer()
        recovered_peer["codex_sessions"] = [{
            "session_id": "stable-session", "start": "2026-07-01 09:00",
            "end": "2026-07-01 09:10", "machine": "macbook",
        }]
        with mock.patch.object(
            collector, "fetch_clockify", side_effect=AssertionError("must adopt clockify")
        ), mock.patch.object(
            collector, "fetch_fathom", side_effect=AssertionError("must adopt fathom")
        ), mock.patch.object(
            collector, "fetch_multica_issues", side_effect=AssertionError("must adopt multica")
        ), mock.patch.object(
            collector, "fetch_calendly", side_effect=AssertionError("must adopt calendly")
        ), mock.patch.object(
            collector, "collect_remote_sessions", return_value=recovered_peer
        ) as peer_transport:
            result = recovery.recover(parent, SOURCE, ATTEMPT_1)

        self.assertEqual(1, peer_transport.call_count)
        self.assertEqual(before, tree_hashes(parent))
        self.assertFalse((result.run_dir / "source-adoption.json").exists())
        lineage = json.loads((result.run_dir / "ledger-recovery.json").read_text())
        self.assertEqual("source-recovery-ledger/v1", lineage["schema_version"])
        self.assertEqual(parent.name, lineage["parent_run_id"])
        self.assertEqual(SOURCE, lineage["requested_source"])
        self.assertEqual(
            "sha256:" + hashlib.sha256(
                (parent / "evidence" / "evidence-ledger.json").read_bytes()
            ).hexdigest(),
            lineage["parent_ledger_digest"],
        )

    def test_mutated_parent_raw_peer_evidence_is_rejected_before_transport(self) -> None:
        parent = self.make_parent()
        sessions_path = parent / "evidence" / "sessions.json"
        sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
        sessions[0]["codex_sessions"] = [{
            "source": "codex", "machine": "macbook", "session_id": "forged",
            "start": "2026-07-01 09:00",
        }]
        sessions_path.write_text(json.dumps(sessions) + "\n", encoding="utf-8")

        with mock.patch.object(collector, "collect_remote_sessions") as transport:
            with self.assertRaisesRegex(
                recovery.SourceDebtRecoveryError, "raw evidence.*bound ledger"
            ):
                recovery.recover(parent, SOURCE, ATTEMPT_1)
        transport.assert_not_called()

    def test_invalid_attempt_and_compatibility_drift_block_before_collection(self) -> None:
        """Unsafe identity or opaque compatibility drift must never reach transport."""
        parent = self.make_parent()
        with mock.patch.object(collector, "collect_remote_sessions") as transport:
            with self.assertRaisesRegex(recovery.SourceDebtRecoveryError, "attempt"):
                recovery.recover(parent, SOURCE, "attempt-1")
            self.fleet["ssh_options"] = ["-o", "BatchMode=yes"]
            (self.root / "fleet.json").write_text(json.dumps(self.fleet) + "\n")
            with self.assertRaisesRegex(recovery.SourceDebtRecoveryError, "compatibility"):
                recovery.recover(parent, SOURCE, ATTEMPT_1)
        transport.assert_not_called()

    def test_verified_legacy_release_parent_uses_schema_compatibility_adapter(self) -> None:
        """Catches release SHA churn blocking a bundle-bound bab6 parent."""
        bab6_runtime = {
            "collector_path": "/repo/scripts/clockify_sync_collect.py",
            "canonical_root": "/repo",
            "git_sha": "bab6bdf8c05e8a37790d90cda5810bd902ca0371",
            "git_dirty": False,
        }
        with mock.patch.object(
            collector, "collector_runtime_identity", return_value=bab6_runtime
        ):
            parent = self.make_parent()
        finalization_path = parent / "slice-finalization.json"
        finalization = json.loads(finalization_path.read_text(encoding="utf-8"))
        compatibility_payload = {
            "contract": "collector-slice-bundles/v1",
            "collector_sha256": (
                "df85b284c6d99f34e4c68f2012ee052e27bdceea73de1bf3382c4ef49efacccd"
            ),
            "routing": self.routing,
            "fleet": self.fleet,
            "calendly_optional": True,
            "coordinator": "omarchy-precision",
        }
        legacy_compatibility = "collector-slice-bundles/v1:" + hashlib.sha256(
            json.dumps(
                compatibility_payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        finalization["backlog_identity"]["compatibility_version"] = legacy_compatibility
        finalization_path.write_text(json.dumps(finalization) + "\n", encoding="utf-8")
        identity = collector.BacklogIdentity(**finalization["backlog_identity"])
        slices = collector.plan_slices(
            SINCE, UNTIL, zone=collector.BUCHAREST, max_days=identity.max_days
        )
        store = collector.BacklogStore(self.checkpoints)
        legacy = store.open(identity, slices)
        bundle_path = (parent / "completion-bundle.json").resolve()
        store.record_complete(
            legacy, slices[0].slice_id, bundle_path,
            "sha256:" + hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        )

        with mock.patch.object(
            collector, "collect_remote_sessions", return_value=self.healthy_peer()
        ) as transport:
            result = recovery.recover(parent, SOURCE, ATTEMPT_1)

        self.assertEqual(parent, result.parent_run_dir)
        self.assertEqual(1, transport.call_count)

        bundle = collector_receipts.load_completion_bundle(
            parent / "completion-bundle.json", run_dir=parent
        )
        coverage = collector_receipts.completion_coverage(bundle)
        self.assertFalse(recovery._legacy_parent_compatibility_matches(
            legacy_compatibility,
            routing=self.routing,
            fleet=self.fleet,
            coverage_sources=coverage["sources"],
            coordinator="omarchy-precision",
            current_coordinator="omarchy-precision",
            calendly_optional=True,
            runtime_identity={**bab6_runtime, "git_dirty": True},
        ))

        drifted = {
            **self.fleet,
            "machines": [{"name": "desktop", "enabled": True, "kind": "ssh"}],
        }
        (self.root / "fleet.json").write_text(
            json.dumps(drifted) + "\n", encoding="utf-8"
        )
        with mock.patch.object(collector, "collect_remote_sessions") as blocked:
            with self.assertRaisesRegex(
                recovery.SourceDebtRecoveryError, "fleet.*parent"
            ):
                recovery._validate_parent(parent, SOURCE)
        blocked.assert_not_called()

    def test_current_coordinator_drift_blocks_before_collection(self) -> None:
        """Using the parent's coordinator in current compatibility bypasses environment drift."""
        parent = self.make_parent()
        with mock.patch.dict(
            collector.os.environ, {"CLOCKIFY_AUTOPILOT_COORDINATOR": "desktop"}
        ), mock.patch.object(collector, "collect_remote_sessions") as transport:
            with self.assertRaisesRegex(recovery.SourceDebtRecoveryError, "compatibility"):
                recovery.recover(parent, SOURCE, ATTEMPT_1)
        transport.assert_not_called()

    def test_non_debted_or_excluded_source_is_never_recovery_success(self) -> None:
        """Only the exact canonical incomplete source can enter recovery."""
        parent = self.make_parent()
        with mock.patch.object(collector, "collect_remote_sessions") as transport:
            with self.assertRaisesRegex(recovery.SourceDebtRecoveryError, "incomplete"):
                recovery.recover(parent, "peer/desktop", ATTEMPT_1)
            with self.assertRaisesRegex(recovery.SourceDebtRecoveryError, "source"):
                recovery.recover(parent, "sessions/../macbook", ATTEMPT_1)
        transport.assert_not_called()

    def test_terminal_incomplete_bundle_is_sealed_without_parent_receipt_mutation(self) -> None:
        """Treating a verified failed attempt as unfinalizable would force recollection."""
        parent = self.make_parent()
        manifest = next(self.checkpoints.glob("*/backlog-manifest.json"))
        parent_receipt_before = manifest.read_bytes()
        with mock.patch.object(
            collector, "collect_remote_sessions", return_value=self.failed_peer()
        ):
            derived = recovery.recover(parent, SOURCE, ATTEMPT_1).run_dir
        snapshots = review._snapshot_recovery_inputs(derived, parent)
        for name, content in {
            "semantic-analysis.json": b"{}\n",
            "work-accounting-result.json": b"{}\n",
            "quality_report.json": b"{\"status\":\"pass\"}\n",
            "review-snapshot.json": b"{}\n",
        }.items():
            (derived / name).write_bytes(content)

        bundle = review._finalize_recovery_completion(derived)

        self.assertEqual(parent_receipt_before, manifest.read_bytes())
        self.assertEqual(
            "incomplete", review._recovery_source_status(bundle, SOURCE)
        )
        for name in recovery.RECONCILIATION_SNAPSHOTS:
            self.assertEqual((parent / name).read_bytes(), snapshots[name].read_bytes())

    def test_recovery_snapshot_drift_fails_closed(self) -> None:
        """A mutable reconciliation copy must never receive a recovery bundle."""
        parent = self.make_parent()
        with mock.patch.object(
            collector, "collect_remote_sessions", return_value=self.healthy_peer()
        ):
            derived = recovery.recover(parent, SOURCE, ATTEMPT_1).run_dir
        review._snapshot_recovery_inputs(derived, parent)
        for name in (
            "semantic-analysis.json", "work-accounting-result.json",
            "quality_report.json", "review-snapshot.json",
        ):
            (derived / name).write_text("{}\n")
        (derived / "routing.json").write_text("{}\n")

        with self.assertRaisesRegex(review.ReviewRunError, "snapshot"):
            review._finalize_recovery_completion(derived)

    def test_same_attempt_after_collection_crash_reuses_checkpoint_and_preserves_partial(self) -> None:
        """A crash must not allocate a new attempt or discard its partial evidence."""
        parent = self.make_parent()
        validated = recovery._validate_parent(parent, SOURCE)
        _transition, locator = recovery._transition(validated, ATTEMPT_1)
        run_dir = self.runs / ("source-debt-recovery-" + locator.rsplit("/", 1)[1])
        with mock.patch.object(collector, "fetch_clockify") as clockify, \
             mock.patch.object(collector, "collect_remote_sessions", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                recovery.recover(parent, SOURCE, ATTEMPT_1)
        clockify.assert_not_called()
        real_replace = recovery.os.replace
        archived: list[Path] = []

        def observe_replace(source: Path, target: Path) -> None:
            archived.append(Path(target))
            real_replace(source, target)

        with mock.patch.object(collector, "fetch_clockify") as clockify, \
             mock.patch.object(collector, "collect_remote_sessions", return_value=self.healthy_peer()), \
             mock.patch.object(recovery.os, "replace", side_effect=observe_replace):
            result = recovery.recover(parent, SOURCE, ATTEMPT_1)

        self.assertEqual(run_dir, result.run_dir)
        self.assertEqual(1, len(archived))
        self.assertTrue(archived[0].is_dir())
        clockify.assert_not_called()
        self.assertEqual(
            result.checkpoint_dir.parent / "attempt-marker.json",
            next(self.checkpoints.glob("source-debt-recovery/*/attempt-marker.json")),
        )

    def test_same_attempt_preserves_two_identical_crash_partials_before_success(self) -> None:
        """An existing archive name must not strand a later identical crash replay."""
        parent = self.make_parent()
        real_replace = recovery.os.replace
        archived: list[Path] = []

        def observe_replace(source: Path, target: Path) -> None:
            archived.append(Path(target))
            real_replace(source, target)

        with mock.patch.object(recovery.os, "replace", side_effect=observe_replace), \
             mock.patch.object(collector, "collect_remote_sessions", side_effect=RuntimeError("crash one")):
            with self.assertRaisesRegex(RuntimeError, "crash one"):
                recovery.recover(parent, SOURCE, ATTEMPT_1)
            with self.assertRaisesRegex(RuntimeError, "crash one"):
                recovery.recover(parent, SOURCE, ATTEMPT_1)
        with mock.patch.object(recovery.os, "replace", side_effect=observe_replace), \
             mock.patch.object(collector, "collect_remote_sessions", return_value=self.healthy_peer()):
            result = recovery.recover(parent, SOURCE, ATTEMPT_1)

        self.assertTrue(result.run_dir.is_dir())
        self.assertEqual(2, len(archived))
        self.assertNotEqual(archived[0], archived[1])
        self.assertTrue(all(path.is_dir() for path in archived))

    def test_completed_recovery_adoption_rejects_tampered_terminal_identity(self) -> None:
        """A matching quality and bundle digest cannot authorize a different debt result."""
        parent = self.make_parent()
        with mock.patch.object(collector, "collect_remote_sessions", return_value=self.healthy_peer()):
            derived = recovery.recover(parent, SOURCE, ATTEMPT_1).run_dir
        review._snapshot_recovery_inputs(derived, parent)
        for name in (
            "semantic-analysis.json", "work-accounting-result.json",
            "quality_report.json", "review-snapshot.json",
        ):
            (derived / name).write_text("{}\n")
        bundle = review._finalize_recovery_completion(derived)
        transition = json.loads((derived / "run-report.json").read_text())["source_debt_recovery"]
        (derived / "autopilot-result.json").write_text(json.dumps({
            "quality_status": "pass",
            "completion_bundle_digest": bundle.bundle_digest,
            "source_debt_recovery": {
                "source": "sessions/desktop",
                "attempt_id": ATTEMPT_1,
                "status": "complete",
                "transition_digest": transition["transition_digest"],
            },
        }) + "\n")

        with self.assertRaisesRegex(ValueError, "terminal"):
            review._adopt_completed_recovery(derived)

    def test_mutable_execution_drift_finds_same_attempt_and_fails_before_collection(self) -> None:
        """Runtime drift must not silently create fresh work under one attempt ID."""
        parent = self.make_parent()
        with mock.patch.object(collector, "collect_remote_sessions", return_value=self.healthy_peer()):
            first = recovery.recover(parent, SOURCE, ATTEMPT_1)
        with mock.patch.object(
            collector, "collector_runtime_identity",
            return_value={"collector_path": "/different/collector.py", "git_sha": "other", "dirty": True},
        ), mock.patch.object(collector, "collect_remote_sessions") as transport:
            with self.assertRaisesRegex(recovery.SourceDebtRecoveryError, "binding"):
                recovery.recover(parent, SOURCE, ATTEMPT_1)
        transport.assert_not_called()
        self.assertTrue(first.run_dir.is_dir())

    def test_same_attempt_converges_after_crash_before_transition_write(self) -> None:
        """A completed collector report must not be stranded by adapter metadata crash."""
        parent = self.make_parent()
        real_read = recovery._read_object
        interrupted = False

        def crash_after_collection(path: Path, *, label: str):
            nonlocal interrupted
            if label == "recovery run report" and not interrupted:
                interrupted = True
                raise RuntimeError("transition crash")
            return real_read(path, label=label)

        with mock.patch.object(collector, "collect_remote_sessions", return_value=self.healthy_peer()) as transport, \
             mock.patch.object(recovery, "_read_object", side_effect=crash_after_collection):
            with self.assertRaisesRegex(RuntimeError, "transition crash"):
                recovery.recover(parent, SOURCE, ATTEMPT_1)
            result = recovery.recover(parent, SOURCE, ATTEMPT_1)

        self.assertEqual(1, transport.call_count)
        recovery.verify_recovery_run(result.run_dir)

    def test_same_attempt_converges_after_crash_before_finalization_write(self) -> None:
        """A bundle-ready transition must recreate only its missing exact finalization."""
        parent = self.make_parent()
        real_write = collector._write_pending_slice_finalization
        interrupted = False

        def crash_before_finalization(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise RuntimeError("finalization crash")
            return real_write(*args, **kwargs)

        with mock.patch.object(collector, "collect_remote_sessions", return_value=self.healthy_peer()) as transport, \
             mock.patch.object(collector, "_write_pending_slice_finalization", side_effect=crash_before_finalization):
            with self.assertRaisesRegex(RuntimeError, "finalization crash"):
                recovery.recover(parent, SOURCE, ATTEMPT_1)
            result = recovery.recover(parent, SOURCE, ATTEMPT_1)

        self.assertEqual(1, transport.call_count)
        recovery.verify_recovery_run(result.run_dir)

    def test_public_terminal_verifier_binds_explicit_parent_source_and_attempt(self) -> None:
        """Task 3 must not accept a generic bundle under the wrong durable debt identity."""
        parent = self.make_parent()
        with mock.patch.object(collector, "collect_remote_sessions", return_value=self.failed_peer()):
            derived = recovery.recover(parent, SOURCE, ATTEMPT_1).run_dir
        review._snapshot_recovery_inputs(derived, parent)
        for name in (
            "semantic-analysis.json", "work-accounting-result.json",
            "quality_report.json", "review-snapshot.json",
        ):
            (derived / name).write_text("{}\n")
        bundle = review._finalize_recovery_completion(derived)
        transition = json.loads((derived / "run-report.json").read_text())["source_debt_recovery"]
        (derived / "autopilot-result.json").write_text(json.dumps({
            "quality_status": "pass",
            "completion_bundle_digest": bundle.bundle_digest,
            "source_debt_recovery": {
                "source": SOURCE, "attempt_id": ATTEMPT_1, "status": "incomplete",
                "transition_digest": transition["transition_digest"],
            },
        }) + "\n")
        recovery.seal_recovery_receipt(derived)
        before = tree_hashes(self.root)

        verified, status = review.verify_source_debt_recovery_completion(
            derived, parent_run_dir=parent, source=SOURCE, attempt_id=ATTEMPT_1
        )
        self.assertEqual(bundle.bundle_digest, verified.bundle_digest)
        self.assertEqual("incomplete", status)
        self.assertEqual(before, tree_hashes(self.root))
        with self.assertRaisesRegex(review.ReviewRunError, "attempt"):
            review.verify_source_debt_recovery_completion(
                derived, parent_run_dir=parent, source=SOURCE, attempt_id=ATTEMPT_2
            )

    def test_external_recovery_receipt_is_required_secure_and_idempotent(self) -> None:
        """Run-local terminal documents alone must never authorize another hop."""
        parent = self.make_parent()
        derived, _bundle = self.make_terminal_recovery(
            parent, SOURCE, ATTEMPT_1, peer=self.session_only_peer(), seal=False
        )
        with mock.patch.object(
            collector, "collect_remote_sessions", return_value=self.healthy_peer()
        ):
            with self.assertRaisesRegex(
                recovery.SourceDebtRecoveryError, "external recovery receipt"
            ):
                recovery.recover(derived, SOURCE, ATTEMPT_2)

        seal = getattr(recovery, "seal_recovery_receipt", None)
        self.assertIsNotNone(seal, "external receipt sealing is required")
        receipt = seal(derived)
        receipt_path = receipt.path
        root = receipt_path.parent
        self.assertNotEqual(derived, root)
        self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
        self.assertEqual(0o444, stat.S_IMODE(receipt_path.stat().st_mode))
        self.assertEqual(recovery.os.getuid(), receipt_path.stat().st_uid)
        self.assertNotIn("Existing work", receipt_path.read_text())
        before = (receipt_path.stat().st_ino, receipt_path.stat().st_mtime_ns, receipt_path.read_bytes())
        repeated = seal(derived)
        self.assertEqual(receipt.digest, repeated.digest)
        self.assertEqual(
            before,
            (receipt_path.stat().st_ino, receipt_path.stat().st_mtime_ns, receipt_path.read_bytes()),
        )

        with mock.patch.object(
            collector, "collect_remote_sessions", return_value=self.healthy_peer()
        ):
            second = recovery.recover(derived, SOURCE, ATTEMPT_2)
        self.assertEqual(derived, second.parent_run_dir)

        verify = getattr(recovery, "verify_recovery_receipt")
        original = receipt_path.read_bytes()
        cases = ("mode", "owner", "symlink", "tamper", "missing")
        for case in cases:
            with self.subTest(case=case):
                if case == "mode":
                    receipt_path.chmod(0o644)
                elif case == "owner":
                    pass
                elif case == "symlink":
                    receipt_path.unlink()
                    target = root / "receipt-target"
                    target.write_bytes(original)
                    target.chmod(0o444)
                    receipt_path.symlink_to(target)
                elif case == "tamper":
                    receipt_path.chmod(0o644)
                    receipt_path.write_text('{}\n')
                    receipt_path.chmod(0o444)
                else:
                    receipt_path.unlink()
                uid_patch = (
                    mock.patch.object(recovery.os, "getuid", return_value=recovery.os.getuid() + 1)
                    if case == "owner" else mock.patch.object(recovery.os, "getuid", wraps=recovery.os.getuid)
                )
                with uid_patch, self.assertRaisesRegex(
                    recovery.SourceDebtRecoveryError, "external recovery receipt"
                ):
                    verify(derived)
                if receipt_path.is_symlink() or receipt_path.exists():
                    receipt_path.unlink()
                receipt_path.write_bytes(original)
                receipt_path.chmod(0o444)
                target = root / "receipt-target"
                if target.exists():
                    target.chmod(0o600)
                    target.unlink()

    def test_same_attempt_recollects_unreceipted_terminal_run_after_crash(self) -> None:
        parent = self.make_parent()
        derived, _bundle = self.make_terminal_recovery(
            parent, SOURCE, ATTEMPT_1, peer=self.session_only_peer(), seal=False
        )

        with mock.patch.object(
            collector, "collect_remote_sessions", return_value=self.healthy_peer()
        ) as transport:
            restarted = recovery.recover(parent, SOURCE, ATTEMPT_1)

        self.assertFalse(restarted.collection_reused)
        self.assertEqual(1, transport.call_count)
        self.assertEqual(derived, restarted.run_dir)
        self.assertFalse((derived / "autopilot-result.json").exists())
        self.assertEqual(
            1,
            len(list(self.runs.glob(derived.name + "-incomplete-*"))),
        )

    def test_external_receipt_blocks_semantically_coherent_run_local_rewrite(self) -> None:
        parent = self.make_parent()
        derived, _bundle = self.make_terminal_recovery(
            parent, SOURCE, ATTEMPT_1, peer=self.session_only_peer()
        )
        rewritten = [
            *(derived / relative for relative in recovery._EVIDENCE_FILES.values()),
            derived / "evidence" / "evidence-ledger.json",
            derived / "ledger-recovery.json",
            derived / "run-report.json",
            derived / "autopilot-result.json",
            derived / "completion-bundle.json",
        ]
        for path in rewritten:
            document = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        with self.assertRaisesRegex(review.ReviewRunError, "external recovery receipt"):
            review.verify_source_debt_recovery_completion(
                derived,
                parent_run_dir=parent,
                source=SOURCE,
                attempt_id=ATTEMPT_1,
            )

    def test_parent_bundle_finalization_snapshot_and_receipt_tampering_block_collection(self) -> None:
        """Every immutable parent-chain artifact must be verified before transport."""
        parent = self.make_parent()
        finalization_path = parent / "slice-finalization.json"
        finalization = json.loads(finalization_path.read_text())
        identity = collector.BacklogIdentity(**finalization["backlog_identity"])
        slices = collector.plan_slices(SINCE, UNTIL, zone=collector.BUCHAREST)
        state = collector.BacklogStore(self.checkpoints).read_existing(identity, slices)
        cases = (
            (parent / "completion-bundle.json", b"{}\n"),
            (finalization_path, b"{}\n"),
            (state.directory / "backlog-manifest.json", b"{}\n"),
        )
        with mock.patch.object(collector, "collect_remote_sessions") as transport:
            for path, tampered in cases:
                original = path.read_bytes()
                try:
                    path.write_bytes(tampered)
                    with self.subTest(path=path.name), self.assertRaises(recovery.SourceDebtRecoveryError):
                        recovery.recover(parent, SOURCE, ATTEMPT_1)
                finally:
                    path.write_bytes(original)
            snapshot = parent / "period-manifest.json"
            original = snapshot.read_bytes()
            snapshot.unlink()
            try:
                with self.assertRaises(recovery.SourceDebtRecoveryError):
                    recovery.recover(parent, SOURCE, ATTEMPT_1)
            finally:
                snapshot.write_bytes(original)
        transport.assert_not_called()

    def test_verified_terminal_recovery_can_parent_newly_incomplete_peer(self) -> None:
        root = self.make_parent()
        first, _bundle = self.make_terminal_recovery(
            root, SOURCE, ATTEMPT_1, peer=self.session_only_peer()
        )

        with mock.patch.object(
            collector, "collect_remote_sessions", return_value=self.healthy_peer()
        ) as transport:
            second = recovery.recover(first, SOURCE, ATTEMPT_2)

        self.assertEqual(first, second.parent_run_dir)
        self.assertEqual(1, transport.call_count)

    def test_multi_hop_recovery_preserves_bound_non_peer_raw_evidence(self) -> None:
        """Derived parents must round-trip retained providers without recollection."""
        providers = {
            "clockify": {
                "status": "ok", "complete": True,
                "entries": [{
                    "id_suffix": "clock-1", "description": "Existing work",
                    "project_id_suffix": "project-1", "tag_id_suffixes": [],
                    "start": "2026-07-01T09:00:00+03:00",
                    "end": "2026-07-01T09:30:00+03:00", "running": False,
                    "running_snapshot": None, "duration": "PT30M", "billable": True,
                }],
            },
            "fathom": {
                "status": "ok", "complete": True,
                "meetings": [{
                    "recording_id": "meeting-1", "title": "Planning",
                    "start": "2026-07-01T10:00:00+03:00",
                    "end": "2026-07-01T10:30:00+03:00",
                    "summary": "Agreed the recovery boundary.",
                    "action_items": [{"text": "Verify the derived run"}],
                    "transcript": [{"speaker": "Owner", "text": "Proceed."}],
                    "semantic_evidence_status": "available",
                }],
            },
            "multica_issues": {
                "status": "ok", "complete": True,
                "issues": [{
                    "id": "SER-1", "key": "SER-1", "title": "Recovery",
                    "status": "in_progress",
                    "created_at": "2026-07-01T08:00:00+03:00",
                    "updated_at": "2026-07-01T08:30:00+03:00",
                }],
            },
        }
        with (
            mock.patch.object(
                collector, "fetch_clockify", return_value=providers["clockify"]
            ),
            mock.patch.object(
                collector, "fetch_fathom", return_value=providers["fathom"]
            ),
            mock.patch.object(
                collector, "fetch_multica_issues",
                return_value=providers["multica_issues"],
            ),
        ):
            root = self.make_parent()

        provider_files = {
            "clockify": "clockify-existing.json",
            "fathom": "fathom-meetings.json",
            "calendly": "calendly-recordings.json",
            "multica_issues": "multica-issues.json",
        }
        expected = {
            name: json.loads((root / "evidence" / filename).read_text())
            for name, filename in provider_files.items()
        }
        with (
            mock.patch.object(
                collector, "fetch_clockify",
                side_effect=AssertionError("must retain bound clockify evidence"),
            ),
            mock.patch.object(
                collector, "fetch_fathom",
                side_effect=AssertionError("must retain bound fathom evidence"),
            ),
            mock.patch.object(
                collector, "fetch_calendly",
                side_effect=AssertionError("must retain bound calendly evidence"),
            ),
            mock.patch.object(
                collector, "fetch_multica_issues",
                side_effect=AssertionError("must retain bound multica evidence"),
            ),
        ):
            first, _first_bundle = self.make_terminal_recovery(
                root, SOURCE, ATTEMPT_1, peer=self.session_only_peer()
            )
            for name, filename in provider_files.items():
                self.assertEqual(
                    expected[name],
                    json.loads((first / "evidence" / filename).read_text()),
                )
            second, _second_bundle = self.make_terminal_recovery(
                first, SOURCE, ATTEMPT_2, peer=self.healthy_peer()
            )

        raw, _digests = recovery._parent_evidence(
            type("RawParent", (), {"run_dir": second})()
        )
        ledger = recovery._verified_parent_ledger(second)
        reconstructed = recovery.evidence_ledger.EvidenceLedger(
            tuple(recovery.evidence_ledger.normalize_collector_snapshot(raw)),
            recovery.evidence_ledger.source_inventory_from_collector(raw),
            ledger.timezone,
            ledger.member_identities,
        )
        self.assertEqual(ledger.manifest.document(), reconstructed.manifest.document())

        for filename in provider_files.values():
            path = second / "evidence" / filename
            original = path.read_bytes()
            try:
                path.write_text('{"status":"ok","complete":true}\n')
                with self.subTest(filename=filename), self.assertRaisesRegex(
                    recovery.SourceDebtRecoveryError,
                    "external recovery receipt",
                ):
                    recovery._validate_parent(second, SOURCE)
            finally:
                path.write_bytes(original)

    def test_derived_parent_tampered_terminal_result_blocks_before_collection(self) -> None:
        root = self.make_parent()
        first, _bundle = self.make_terminal_recovery(
            root, SOURCE, ATTEMPT_1, peer=self.session_only_peer()
        )
        result_path = first / "autopilot-result.json"
        result = json.loads(result_path.read_text())
        result["source_debt_recovery"]["attempt_id"] = ATTEMPT_2
        result_path.write_text(json.dumps(result) + "\n")

        with mock.patch.object(collector, "collect_remote_sessions") as transport:
            with self.assertRaisesRegex(recovery.SourceDebtRecoveryError, "terminal"):
                recovery.recover(first, SOURCE, ATTEMPT_2)
        transport.assert_not_called()

    def test_derived_parent_cycle_blocks_before_collection(self) -> None:
        root = self.make_parent()
        first, _bundle = self.make_terminal_recovery(
            root, SOURCE, ATTEMPT_1, peer=self.session_only_peer()
        )
        report_path = first / "run-report.json"
        report = json.loads(report_path.read_text())
        report["source_debt_recovery"]["parent_run_id"] = first.name
        report_path.write_text(json.dumps(report) + "\n")

        with mock.patch.object(collector, "collect_remote_sessions") as transport:
            with self.assertRaises(recovery.SourceDebtRecoveryError) as caught:
                recovery.recover(first, SOURCE, ATTEMPT_2)
        transport.assert_not_called()
        causes: list[str] = []
        error: BaseException | None = caught.exception
        while error is not None:
            causes.append(str(error))
            error = error.__cause__
        self.assertTrue(any("cycle" in message for message in causes))

    def test_public_chain_verifier_rejects_tampered_ancestor_artifacts(self) -> None:
        root = self.make_parent()
        first, _first_bundle = self.make_terminal_recovery(
            root, SOURCE, ATTEMPT_1, peer=self.session_only_peer()
        )
        second, _second_bundle = self.make_terminal_recovery(
            first, SOURCE, ATTEMPT_2, peer=self.healthy_peer()
        )
        cases: list[tuple[str, Path, bytes]] = [
            ("bundle", first / "completion-bundle.json", b"{}\n"),
            ("snapshot", first / "period-manifest.json", b"{\"tampered\":true}\n"),
        ]
        report_path = first / "run-report.json"
        report = json.loads(report_path.read_text())
        report["source_debt_recovery"]["attempt_id"] = ATTEMPT_2
        cases.append(("transition", report_path, (json.dumps(report) + "\n").encode()))

        for label, path, tampered in cases:
            original = path.read_bytes()
            try:
                path.write_bytes(tampered)
                with self.subTest(label=label), self.assertRaises(review.ReviewRunError):
                    review.verify_source_debt_recovery_completion(
                        second,
                        parent_run_dir=first,
                        source=SOURCE,
                        attempt_id=ATTEMPT_2,
                    )
            finally:
                path.write_bytes(original)

    def test_direct_script_context_uses_shared_terminal_verifier_for_chain(self) -> None:
        root = self.make_parent()
        first, _bundle = self.make_terminal_recovery(
            root, SOURCE, ATTEMPT_1, peer=self.session_only_peer()
        )
        direct = runpy.run_path(str(Path(recovery.__file__)))
        direct_globals = direct["_validate_parent"].__globals__
        direct_globals["ROOT"] = self.root
        direct_globals["RUNS"] = self.runs

        with mock.patch.dict(sys.modules, {"clockify_review_run": review}):
            parent = direct["_validate_parent"](first, SOURCE)

        self.assertEqual(first, parent.run_dir)

    def test_recovery_accounting_publication_replay_is_exact_and_idempotent(self) -> None:
        """A real recovered slice must retain exact intervals through Sheet readback."""
        self.routing = json.loads(
            (PROJECT_ROOT / "routing.json").read_text(encoding="utf-8")
        )
        (self.root / "routing.json").write_text(
            json.dumps(self.routing) + "\n", encoding="utf-8"
        )
        clockify = {
            "status": "ok",
            "complete": True,
            "entries": [{
                "id_suffix": "overlap1",
                "description": "Different existing work",
                "project_id_suffix": "775f9f",
                "tag_id_suffixes": [],
                "start": "2026-07-01T09:05:00+03:00",
                "end": "2026-07-01T09:15:00+03:00",
                "running": False,
                "running_snapshot": None,
                "duration": "PT10M",
                "billable": True,
            }],
        }
        with mock.patch.object(collector, "fetch_clockify", return_value=clockify):
            parent = self.make_parent()
        peer = self.healthy_peer()
        peer["codex_sessions"] = [
            {
                "session_id": "recovered-first",
                "start": "2026-07-01T09:00:00+03:00",
                "end": "2026-07-01T09:20:00+03:00",
                "path": "/fixture/recovered-first.jsonl",
                "title": "Recovered first interval",
            },
            {
                "session_id": "recovered-second",
                "start": "2026-07-01T10:00:00+03:00",
                "end": "2026-07-01T10:10:00+03:00",
                "path": "/fixture/recovered-second.jsonl",
                "title": "Recovered second interval",
            },
        ]
        with mock.patch.object(
            collector, "collect_remote_sessions", return_value=peer
        ):
            recovered = recovery.recover(parent, SOURCE, ATTEMPT_1)
        snapshots = review._snapshot_recovery_inputs(
            recovered.run_dir, recovered.parent_run_dir
        )
        snapshot_bytes = {name: path.read_bytes() for name, path in snapshots.items()}
        ledger = json.loads(
            (recovered.run_dir / "evidence" / "evidence-ledger.json").read_text(
                encoding="utf-8"
            )
        )
        sessions = sorted(
            (
                event for event in ledger["events"]
                if event["source_type"] == "codex_sessions"
            ),
            key=lambda event: event["raw_source_span"]["start"],
        )
        self.assertEqual(2, len(sessions))

        def activity(event, action, object_, outcome, minutes):
            return {
                "lifecycle": "completed",
                "action": action,
                "object": object_,
                "outcome": outcome,
                "evidence_ids": [event["evidence_id"]],
                "evidence_spans": [{
                    "evidence_id": event["evidence_id"],
                    "start": event["raw_source_span"]["start"],
                    "end": event["raw_source_span"]["end"],
                }],
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
                "effort": {
                    "minimum_minutes": minutes,
                    "recommended_minutes": minutes,
                    "maximum_minutes": minutes,
                },
                "semantic_confidence": "high",
                "timing_confidence": "high",
                "split_rationale": "one bounded evidence interval",
                "merge_rationale": "no other activity shares this deliverable",
            }

        analysis = {
            "activities": [
                activity(
                    sessions[0], "Recovered", "the first exact interval",
                    "for complete accounting", 20,
                ),
                activity(
                    sessions[1], "Reviewed", "the second exact interval",
                    "for complete accounting", 10,
                ),
            ],
            "exceptions": [],
            "omissions": [],
        }
        fixture = self.root / "analysis.json"
        fixture.write_text(json.dumps(analysis) + "\n", encoding="utf-8")
        first = pipeline.run_accounting(
            recovered.run_dir,
            root=PROJECT_ROOT,
            analysis_fixture=fixture,
            routing_path=recovered.run_dir / "routing.json",
        )
        replay = pipeline.run_accounting(
            recovered.run_dir,
            root=PROJECT_ROOT,
            analysis_fixture=fixture,
            routing_path=recovered.run_dir / "routing.json",
        )

        self.assertEqual(first, replay)
        self.assertEqual(
            snapshot_bytes,
            {name: path.read_bytes() for name, path in snapshots.items()},
        )
        proposals = sorted(replay["proposals"], key=lambda row: row["start"])
        self.assertEqual(
            [
                ("2026-07-01T09:00:00+03:00", "2026-07-01T09:05:00+03:00"),
                ("2026-07-01T09:05:00+03:00", "2026-07-01T09:15:00+03:00"),
                ("2026-07-01T09:15:00+03:00", "2026-07-01T09:20:00+03:00"),
                ("2026-07-01T10:00:00+03:00", "2026-07-01T10:10:00+03:00"),
            ],
            [(row["start"], row["end"]) for row in proposals],
        )
        publisher.validate_recovery_proposal_groups(proposals)
        overlap = next(
            proposal for proposal in proposals
            if any(
                warning["type"] == "existing_clockify_overlap"
                for warning in proposal["review_warnings"]
            )
        )
        existing_event = next(
            event for event in ledger["events"] if event["source_type"] == "clockify"
        )
        self.assertEqual(
            {
                "type": "existing_clockify_overlap",
                "counterpart_id": existing_event["evidence_id"],
                "counterpart_project_suffix": "775f9f",
                "overlap_start": "2026-07-01T09:05:00+03:00",
                "overlap_end": "2026-07-01T09:15:00+03:00",
                "overlap_duration_seconds": 600,
            },
            next(
                warning for warning in overlap["review_warnings"]
                if warning["type"] == "existing_clockify_overlap"
            ),
        )
        self.assertEqual(
            {
                "type": "allocation_capacity_recovery",
                "requested_minutes": 20,
                "allocator_allocated_minutes": 10,
                "recovered_minutes": 10,
                "residual_minutes": 0,
            },
            next(
                warning for warning in overlap["review_warnings"]
                if warning["type"] == "allocation_capacity_recovery"
            ),
        )
        rows = [
            publisher.proposal_row(proposal, recovered.run_dir.name)
            for proposal in proposals
        ]
        gateway = RecoverySheetGateway()
        published = publisher.publish(
            gateway,
            spreadsheet_id="fixture-sheet",
            sheet_title="September 2026 review",
            template_title="Proposals",
            rows=rows,
        )
        repeated = publisher.publish(
            gateway,
            spreadsheet_id="fixture-sheet",
            sheet_title="September 2026 review",
            template_title="Proposals",
            rows=rows,
        )
        self.assertEqual(len(rows), published["appended"])
        self.assertEqual(len(rows), repeated["unchanged"])
        self.assertEqual(
            len(rows),
            len({row[0] for row in gateway.rows[1:]}),
        )


if __name__ == "__main__":
    unittest.main()
