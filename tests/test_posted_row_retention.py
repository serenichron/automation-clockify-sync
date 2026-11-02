"""Approved/posted review rows keep their reviewed wording on later publications.

A later cycle may regenerate presentation for an activity a human already
approved or posted. Accounting drift must still stop publication; wording
drift must leave the reviewed row exactly as it is, without any write.
"""

import unittest

from scripts import clockify_sheet_publish as publisher
from tests.test_sheet_publish import StatefulGateway, proposal

TITLE = "August 2026 review"


def publish(gateway, rows):
    return publisher.publish(
        gateway, spreadsheet_id="sheet", sheet_title=TITLE,
        template_title="Proposals", rows=rows,
    )


def reviewed(row, *, disposition="Approved", status="posted"):
    prior = list(row)
    prior[7] = "medium"
    prior[8] = "SC — Wording the reviewer approved and posted"
    prior[10] = 1
    prior[11] = "20261019T104741Z-repair-older"
    prior[12] = ""
    prior[9], prior[13], prior[14] = disposition, status, "Human note"
    return prior


class PostedRowRetentionTests(unittest.TestCase):
    def test_posted_row_with_wording_drift_is_retained_without_writes(self):
        row = publisher.proposal_row(proposal(), "run-2")
        for disposition, status in (("Approved", "posted"), ("Approved", "unposted"), ("pending", "posted")):
            with self.subTest(disposition=disposition, status=status):
                prior = reviewed(row, disposition=disposition, status=status)
                gateway = StatefulGateway([publisher.HEADER, prior])

                result = publish(gateway, [row])

                self.assertEqual(1, result["unchanged"])
                self.assertEqual(0, result["updated"])
                self.assertEqual(0, result["appended"])
                self.assertEqual([row[0]], result["retained_reviewed"])
                self.assertEqual([], gateway.updated)
                self.assertEqual([], [rows for _range, rows in gateway.appended if rows])
                self.assertEqual(prior, gateway.rows[1])

    def test_accounting_drift_on_reviewed_row_still_aborts_before_writes(self):
        row = publisher.proposal_row(proposal(), "run-2")
        changes = {
            1: "2026-08-01 10:05:00", 2: "2026-08-01 10:20:00", 3: 12,
            4: "Other Client Level 2", 5: "Business development", 6: "act-999",
        }
        for column, value in changes.items():
            with self.subTest(column=publisher.HEADER[column]):
                prior = reviewed(row)
                prior[column] = value
                gateway = StatefulGateway([publisher.HEADER, prior])

                with self.assertRaisesRegex(publisher.PublicationError, "approved or posted"):
                    publish(gateway, [row])

                self.assertEqual([], gateway.updated)
                self.assertEqual([], gateway.appended)

    def test_retained_row_does_not_block_new_rows_and_readback_binds_sheet_wording(self):
        row = publisher.proposal_row(proposal(1), "run-2")
        new = publisher.proposal_row(proposal(2), "run-2")
        prior = reviewed(row)
        gateway = StatefulGateway([publisher.HEADER, prior])

        result = publish(gateway, [row, new])

        self.assertEqual(1, result["appended"])
        self.assertEqual([row[0]], result["retained_reviewed"])
        self.assertEqual(prior, gateway.rows[1])
        self.assertEqual(new[0], gateway.rows[2][0])

    def test_retained_reviewed_row_changed_during_publication_fails_readback(self):
        row = publisher.proposal_row(proposal(1), "run-2")
        new = publisher.proposal_row(proposal(2), "run-2")
        prior = reviewed(row)

        class DriftingGateway(StatefulGateway):
            def append_values(self, spreadsheet_id, range_name, rows):
                super().append_values(spreadsheet_id, range_name, rows)
                self.rows[1][8] = "changed by someone else mid-publication"

        with self.assertRaisesRegex(publisher.PublicationError, "readback"):
            publish(DriftingGateway([publisher.HEADER, prior]), [row, new])

    def test_unreviewed_row_wording_drift_still_updates(self):
        row = publisher.proposal_row(proposal(), "run-2")
        prior = reviewed(row, disposition="pending", status="unposted")

        result = publish(StatefulGateway([publisher.HEADER, prior]), [row])

        self.assertEqual(1, result["updated"])
        self.assertNotIn("retained_reviewed", result)


if __name__ == "__main__":
    unittest.main()
