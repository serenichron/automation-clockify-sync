from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from scripts import posting_receipts


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
OPERATION = "clockify_post:period-1:workspace-1:member-1"


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


class ApprovalReceiptStoreTests(unittest.TestCase):
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


class PostEventStoreTests(unittest.TestCase):
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
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "terminal"):
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


if __name__ == "__main__":
    unittest.main()
