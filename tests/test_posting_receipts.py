from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import posting_receipts


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
OPERATION = posting_receipts.derive_operation_identity(
    operation="clockify_post", period_id="period-1", workspace_id="workspace-1", member_id="member-1"
)


def approval_receipt(**changes: object) -> posting_receipts.ApprovalReceipt:
    values: dict[str, object] = {
        "approval_id": "approval-1",
        "approver": "board-member",
        "approved_at": "2026-08-18T11:00:00Z",
        "expires_at": "2026-08-18T13:00:00Z",
        "operation": "clockify_post",
        "operation_identity": OPERATION,
        "period_id": "period-1",
        "period_start": "2026-08-01T00:00:00Z",
        "period_end": "2026-09-01T00:00:00Z",
        "workspace_id": "workspace-1",
        "member_id": "member-1",
        "portfolio_digest": "sha256:portfolio",
        "quality_digest": "sha256:quality",
        "replay_digest": "sha256:replay",
        "routing_digest": "sha256:routing",
        "correction_log_digest": "sha256:corrections",
        "coverage_digest": "sha256:coverage",
        "residual_exception_digest": "sha256:exceptions",
        "single_use": True,
    }
    values.update(changes)
    return posting_receipts.ApprovalReceipt(**values)


def post_event(disposition: str, **changes: object) -> posting_receipts.PostEvent:
    values: dict[str, object] = {
        "disposition": disposition,
        "operation": "clockify_post",
        "operation_identity": OPERATION,
        "period_id": "period-1",
        "workspace_id": "workspace-1",
        "member_id": "member-1",
        "review_id": "review-1",
        "segment_index": 0,
        "recorded_at": "2026-08-18T12:00:00Z",
        "clockify_entry_id": None,
        "live_readback_digest": None,
    }
    values.update(changes)
    return posting_receipts.PostEvent(**values)


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ApprovalReceiptStoreTests(unittest.TestCase):
    def test_pending_journal_directory_is_fsynced_before_primary_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approvals.jsonl"
            store = posting_receipts.ApprovalReceiptStore(path)
            real_append = posting_receipts._append_jsonl
            with mock.patch.object(posting_receipts, "_fsync_directory", wraps=posting_receipts._fsync_directory) as sync_directory:
                def append_observing_durable_journal(destination: Path, record: dict) -> None:
                    if destination == path:
                        self.assertTrue(sync_directory.called)
                    real_append(destination, record)

                with mock.patch.object(posting_receipts, "_append_jsonl", side_effect=append_observing_durable_journal):
                    store.append(approval_receipt())

    def test_semantically_invalid_pending_approval_never_mutates_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approvals.jsonl"
            store = posting_receipts.ApprovalReceiptStore(path)
            real_append = posting_receipts._append_jsonl

            def interrupt_primary(destination: Path, record: dict) -> None:
                if destination == path:
                    raise InterruptedError("stop before primary")
                real_append(destination, record)

            with mock.patch.object(posting_receipts, "_append_jsonl", side_effect=interrupt_primary):
                with self.assertRaises(InterruptedError):
                    store.append(approval_receipt())

            pending_path = path.with_name(path.name + ".pending.json")
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            pending["record"]["receipt"]["workspace_id"] = ""
            event_payload = {key: value for key, value in pending["record"].items() if key != "event_digest"}
            pending["record"]["event_digest"] = canonical_digest(event_payload)
            pending["expected_head"] = pending["record"]["event_digest"]
            pending_payload = {key: value for key, value in pending.items() if key != "pending_digest"}
            pending["pending_digest"] = canonical_digest(pending_payload)
            pending_path.write_text(json.dumps(pending) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "workspace"):
                posting_receipts.ApprovalReceiptStore(path).verify()
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name(path.name + ".anchor.jsonl").exists())

    def test_approval_restart_completes_primary_only_pending_commit_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approvals.jsonl"
            store = posting_receipts.ApprovalReceiptStore(path)
            receipt = approval_receipt()
            with mock.patch.object(posting_receipts, "_append_anchor", side_effect=InterruptedError("stop")):
                with self.assertRaises(InterruptedError):
                    store.append(receipt)

            restarted = posting_receipts.ApprovalReceiptStore(path)
            self.assertEqual(
                receipt,
                restarted.require(receipt.approval_id, operation_identity=OPERATION, now=NOW),
            )
            self.assertEqual(1, len(path.read_text(encoding="utf-8").splitlines()))
            restarted.verify()
            with self.assertRaises(posting_receipts.PostingReceiptError):
                restarted.require(receipt.approval_id, operation_identity="different", now=NOW)

    def test_tampered_pending_commit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approvals.jsonl"
            store = posting_receipts.ApprovalReceiptStore(path)
            with mock.patch.object(posting_receipts, "_append_anchor", side_effect=InterruptedError("stop")):
                with self.assertRaises(InterruptedError):
                    store.append(approval_receipt())
            pending = path.with_name(path.name + ".pending.json")
            self.assertTrue(pending.is_file())
            record = json.loads(pending.read_text(encoding="utf-8"))
            record["expected_head"] = "sha256:tampered"
            pending.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "pending"):
                posting_receipts.ApprovalReceiptStore(path).verify()

    def test_approval_anchor_rejects_truncated_consumption_and_deleted_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approvals.jsonl"
            store = posting_receipts.ApprovalReceiptStore(path)
            receipt = approval_receipt()
            store.append(receipt)
            store.consume(
                receipt.approval_id,
                operation_identity=OPERATION,
                consumed_at="2026-08-18T12:01:00Z",
            )
            first_record = path.read_text(encoding="utf-8").splitlines()[0]
            path.write_text(first_record + "\n", encoding="utf-8")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "anchor"):
                store.require(receipt.approval_id, operation_identity=OPERATION, now=NOW)

            path.unlink()
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "anchor"):
                store.verify()

    def test_approval_is_bound_to_target_period_operation_and_artifact_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = posting_receipts.ApprovalReceiptStore(Path(directory) / "approvals.jsonl")
            receipt = approval_receipt()
            store.append(receipt)

            self.assertEqual(
                receipt,
                store.require(receipt.approval_id, operation_identity=OPERATION, now=NOW),
            )
            with self.assertRaises(posting_receipts.PostingReceiptError):
                store.require(receipt.approval_id, operation_identity="different", now=NOW)

            altered = approval_receipt(approval_id="approval-2", coverage_digest="sha256:other")
            store.append(altered)
            self.assertNotEqual(
                receipt.artifact_digests,
                store.require(altered.approval_id, operation_identity=OPERATION, now=NOW).artifact_digests,
            )

    def test_approval_rejects_expiry_consumption_and_invalid_target_or_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = posting_receipts.ApprovalReceiptStore(Path(directory) / "approvals.jsonl")
            expired = approval_receipt(expires_at="2026-08-18T11:59:59Z")
            store.append(expired)
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "expired"):
                store.require(expired.approval_id, operation_identity=OPERATION, now=NOW)

            expires_now = approval_receipt(approval_id="approval-expires-now", expires_at="2026-08-18T12:00:00Z")
            store.append(expires_now)
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "expired"):
                store.require(expires_now.approval_id, operation_identity=OPERATION, now=NOW)
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "expired"):
                store.consume(
                    expires_now.approval_id,
                    operation_identity=OPERATION,
                    consumed_at="2026-08-18T12:00:00Z",
                )

            valid = approval_receipt(approval_id="approval-offset")
            store.append(valid)
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "offset"):
                store.require(valid.approval_id, operation_identity=OPERATION, now=datetime(2026, 8, 18, 12, 0))

            available = approval_receipt(approval_id="approval-2")
            store.append(available)
            store.consume(available.approval_id, operation_identity=OPERATION, consumed_at="2026-08-18T12:01:00Z")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "consumed"):
                store.require(available.approval_id, operation_identity=OPERATION, now=NOW)

            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "workspace"):
                store.append(approval_receipt(approval_id="approval-3", workspace_id=""))
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "digest"):
                store.append(approval_receipt(approval_id="approval-4", routing_digest=""))

    def test_approval_chain_rejects_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approvals.jsonl"
            store = posting_receipts.ApprovalReceiptStore(path)
            store.append(approval_receipt())
            record = json.loads(path.read_text(encoding="utf-8"))
            record["receipt"]["workspace_id"] = "other-workspace"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "integrity"):
                store.verify()

    def test_approval_identity_canonically_binds_operation_and_target(self) -> None:
        canonical = posting_receipts.derive_operation_identity(
            operation="clockify_post",
            period_id="period-1",
            workspace_id="workspace-1",
            member_id="member-1",
        )
        self.assertNotEqual(canonical, posting_receipts.derive_operation_identity(
            operation="other_operation",
            period_id="period-1",
            workspace_id="workspace-1",
            member_id="member-1",
        ))
        with tempfile.TemporaryDirectory() as directory:
            store = posting_receipts.ApprovalReceiptStore(Path(directory) / "approvals.jsonl")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "operation identity"):
                store.append(approval_receipt(operation_identity=canonical, workspace_id="other-workspace"))


class PostEventStoreTests(unittest.TestCase):
    def test_semantically_invalid_pending_post_never_mutates_ledger(self) -> None:
        # Removing pending semantic validation from recovery would cause this
        # transaction to be appended and anchored before its missing plan is
        # detected by normal receipt verification.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "post-events.jsonl"
            store = posting_receipts.PostEventStore(path)
            real_append = posting_receipts._append_jsonl

            def interrupt_primary(destination: Path, record: dict) -> None:
                if destination == path:
                    raise InterruptedError("stop before primary")
                real_append(destination, record)

            with mock.patch.object(posting_receipts, "_append_jsonl", side_effect=interrupt_primary):
                with self.assertRaises(InterruptedError):
                    store.append(post_event("planned"))

            pending_path = path.with_name(path.name + ".pending.json")
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            pending["record"].update({
                "disposition": "created",
                "clockify_entry_id": "entry-1",
                "live_readback_digest": "sha256:live-1",
            })
            event_payload = {key: value for key, value in pending["record"].items() if key != "event_digest"}
            pending["record"]["event_digest"] = canonical_digest(event_payload)
            pending["expected_head"] = pending["record"]["event_digest"]
            pending_payload = {key: value for key, value in pending.items() if key != "pending_digest"}
            pending["pending_digest"] = canonical_digest(pending_payload)
            pending_path.write_text(json.dumps(pending) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "lacks planned"):
                posting_receipts.PostEventStore(path).verify()
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name(path.name + ".anchor.jsonl").exists())

    def test_post_restart_completes_primary_only_pending_commit_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "post-events.jsonl"
            store = posting_receipts.PostEventStore(path)
            planned = post_event("planned")
            with mock.patch.object(posting_receipts, "_append_anchor", side_effect=InterruptedError("stop")):
                with self.assertRaises(InterruptedError):
                    store.append(planned)

            restarted = posting_receipts.PostEventStore(path)
            receipt = restarted.derive_receipt(OPERATION)
            self.assertEqual(["interrupted"], [entry["disposition"] for entry in receipt["entries"]])
            self.assertEqual(1, len(path.read_text(encoding="utf-8").splitlines()))
            restarted.append(post_event("interrupted"))
            self.assertEqual(["interrupted"], [event.disposition for event in restarted.verify() if event.disposition != "planned"])

    def test_post_anchor_rejects_removal_of_completed_plan_and_terminal_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = posting_receipts.PostEventStore(Path(directory) / "post-events.jsonl")
            store.append(post_event("planned"))
            store.append(post_event(
                "created", clockify_entry_id="entry-1", live_readback_digest="sha256:live-1"
            ))
            store.path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "anchor"):
                store.verify()

    def test_post_history_derives_terminal_receipt_and_rejects_reorder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = posting_receipts.PostEventStore(Path(directory) / "post-events.jsonl")
            store.append(post_event("planned"))
            store.append(post_event(
                "created", clockify_entry_id="entry-1", live_readback_digest="sha256:live-1"
            ))

            receipt = store.derive_receipt(OPERATION)
            self.assertEqual("created", receipt["entries"][0]["disposition"])
            self.assertEqual("entry-1", receipt["entries"][0]["clockify_entry_id"])

            lines = store.path.read_text(encoding="utf-8").splitlines()
            store.path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "integrity"):
                store.verify()

    def test_post_history_rejects_truncated_terminal_and_duplicate_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = posting_receipts.PostEventStore(Path(directory) / "post-events.jsonl")
            store.append(post_event("planned"))
            store.append(post_event(
                "created", clockify_entry_id="entry-1", live_readback_digest="sha256:live-1"
            ))
            lines = store.path.read_text(encoding="utf-8").splitlines()
            store.path.write_text(lines[0] + "\n", encoding="utf-8")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "anchor"):
                store.verify()

            store = posting_receipts.PostEventStore(Path(directory) / "second-events.jsonl")
            store.append(post_event("planned"))
            store.append(post_event(
                "created", clockify_entry_id="entry-1", live_readback_digest="sha256:live-1"
            ))
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "terminal"):
                store.append(post_event(
                    "already_existing", clockify_entry_id="entry-2", review_id="review-1",
                    live_readback_digest="sha256:live-2",
                ))

    def test_post_history_requires_valid_terminal_dispositions_and_live_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = posting_receipts.PostEventStore(Path(directory) / "post-events.jsonl")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "disposition"):
                store.append(post_event("deleted"))
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "Clockify entry"):
                store.append(post_event("created"))
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "readback"):
                store.append(post_event("recovered_after_ambiguous_response", clockify_entry_id="entry-1"))

            store.append(post_event("planned"))
            store.append(post_event("interrupted"))
            self.assertEqual("interrupted", store.derive_receipt(OPERATION)["entries"][0]["disposition"])

    def test_post_history_rejects_operation_and_target_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = posting_receipts.PostEventStore(Path(directory) / "post-events.jsonl")
            store.append(post_event("planned"))
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "operation"):
                store.append(post_event("created", operation_identity="other", clockify_entry_id="entry-1", live_readback_digest="sha256:live"))
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "target"):
                store.append(post_event("created", workspace_id="other", clockify_entry_id="entry-1", live_readback_digest="sha256:live"))

    def test_post_event_rejects_identity_that_contradicts_its_target(self) -> None:
        identity = posting_receipts.derive_operation_identity(
            operation="clockify_post",
            period_id="period-1",
            workspace_id="workspace-1",
            member_id="member-1",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = posting_receipts.PostEventStore(Path(directory) / "post-events.jsonl")
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "operation identity"):
                store.append(post_event("planned", operation_identity=identity, member_id="other-member"))


if __name__ == "__main__":
    unittest.main()
