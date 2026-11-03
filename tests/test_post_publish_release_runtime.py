"""A slice sealed under one release can finish publication under the next.

Source and replay stages keep the runtime identity of the release that
produced them. A publisher failure followed by a release upgrade must not
strand the slice: the post-publish revalidation binds each stage to its own
sealed runtime, exactly as the pre-publish stored-stage check already does.
"""

import datetime as dt
import json
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle
from tests import test_review_cycle_delivery as delivery

OLD = {"canonical_root": "/releases/" + "a" * 40, "collector_path": "/releases/" + "a" * 40 + "/scripts/clockify_sync_collect.py",
       "git_sha": "a" * 40, "git_dirty": False}
NEW = {**OLD, "canonical_root": "/releases/" + "b" * 40, "collector_path": "/releases/" + "b" * 40 + "/scripts/clockify_sync_collect.py",
       "git_sha": "b" * 40}


class PostPublishReleaseRuntimeTests(unittest.TestCase):
    setUp = delivery.ReviewCycleDeliveryTests.setUp
    child_for_runs = delivery.ReviewCycleDeliveryTests.child_for_runs
    publisher_child_result = delivery.ReviewCycleDeliveryTests.publisher_child_result

    def run_twice(self, first_runtime, second_runtime):
        commands: list[list[str]] = []
        child = self.child_for_runs(commands, source_options={"runtime_identity": first_runtime}, publish_codes=[9, 0])
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            failed = cycle.run_cycle({**self.config, "_runtime_identity": first_runtime},
                                     enable_sheet_write=True, today=dt.date(2026, 9, 10))
            retried = cycle.run_cycle({**self.config, "_runtime_identity": second_runtime},
                                      enable_sheet_write=True, today=dt.date(2026, 9, 10))
        return commands, failed, retried

    def test_publish_retry_under_newer_release_delivers_stages_sealed_by_older_release(self):
        commands, failed, retried = self.run_twice(OLD, NEW)

        self.assertEqual("failed", failed["status"])
        self.assertEqual("delivered", retried["status"])
        self.assertEqual(2, sum("clockify_sheet_publish.py" in item[1] for item in commands))
        self.assertEqual(1, sum("--replay-from" in item for item in commands))
        self.assertEqual(4, len(commands))
        record = json.loads((self.state_dir / "review-cycle-state.json").read_text())["slices"]["2026-09-07"]
        self.assertEqual(cycle._value_digest(OLD), record["source"]["runtime_identity_digest"])
        self.assertEqual(cycle._value_digest(OLD), record["replay"]["runtime_identity_digest"])

    def test_publish_retry_under_same_release_is_unchanged(self):
        commands, failed, retried = self.run_twice(OLD, OLD)

        self.assertEqual(("failed", "delivered"), (failed["status"], retried["status"]))
        self.assertEqual(4, len(commands))


if __name__ == "__main__":
    unittest.main()
