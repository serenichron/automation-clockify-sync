"""The ordinary posting CLI must consume exact prepared batch inputs."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_native_sheet_post as native
from scripts import clockify_review_batches as batches
from scripts import clockify_sheet_publish as publisher
from test_clockify_native_sheet_post import capture, route
from test_review_batches import proposal, identity


class ReadOnlyGateway:
    timeout_seconds = 45

    def period_entries(self, start, end):
        if (start, end) != ("2026-09-29T07:00:07Z", "2026-09-29T07:11:23Z"):
            raise AssertionError("CLI requested a different period")
        return []


class NativeReviewBatchCliTests(unittest.TestCase):
    def setUp(self):
        self.sources = [proposal(1), proposal(2, "2026-09-29T10:10:00+03:00",
                                                   "2026-09-29T10:11:23+03:00", 83)]
        for source in self.sources:
            source.update(clockify_project_suffix="123456", tag_names=["Process"], tag_suffixes=["654321"])
        contexts = dict.fromkeys([identity(1), identity(2)], "explicit-work")
        package = batches.package_review_batches(self.sources,
            editorial_descriptions=dict.fromkeys(contexts, "SC — Prepared client deliverables"), work_contexts=contexts)
        self.preparation = {"package": package, "source_proposals": self.sources, "work_contexts": contexts}
        self.document = capture(publisher.portfolio_row(package["activities"][0], "source-run"))

    def request(self, path, key, *, timeout_seconds):
        if key != "synthetic-test-key" or timeout_seconds != 45:
            raise AssertionError("unexpected test gateway configuration")
        if path == "/workspaces/workspace-1/projects?archived=false":
            return [{"id": "project-123456", "name": "Example", "archived": False}]
        if path == "/workspaces/workspace-1/tags":
            return [{"id": "tag-654321", "name": "Process"}]
        raise AssertionError("unexpected provider request")

    def invoke(self, directory, preparation):
        root = Path(directory)
        capture_path, routing_path, prepared_path = root / "capture.json", root / "routing.json", root / "prepared.json"
        capture_path.write_text(json.dumps(self.document))
        routing_path.write_text(json.dumps(route()))
        prepared_path.write_text(json.dumps(preparation))
        args = ["plan", "--sheet-capture", str(capture_path), "--expected-capture-sha256",
                hashlib.sha256(capture_path.read_bytes()).hexdigest(), "--routing", str(routing_path),
                "--review-batches", str(prepared_path), "--output", str(root / "plan.json")]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                result = native.main(args)
            except SystemExit as error:
                result = error.code
        return result, root / "plan.json"

    def test_cli_plan_seals_prepared_children_and_never_posts(self):
        gateway = ReadOnlyGateway()  # Deliberately has no create method.
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(native, "_gateway", return_value=(gateway, "workspace-1", "user-1", "synthetic-test-key")), \
             mock.patch.object(native.legacy, "_paged", side_effect=self.request):
            result, output = self.invoke(directory, self.preparation)
            self.assertEqual(0, result)
            plan = json.loads(output.read_text())
        self.assertEqual(1, plan["row_count"])
        self.assertEqual(154 / 60, plan["total_minutes"])
        self.assertEqual([identity(1), identity(2)], [entry["review_id"] for entry in plan["entries"]])
        self.assertEqual(["2026-09-29T07:00:07Z", "2026-09-29T07:10:00Z"],
                         [entry["payload"]["start"] for entry in plan["entries"]])
        self.assertEqual(self.sources, plan["review_batches_binding"]["source_proposals"])

    def test_malformed_preparation_rejects_before_credentials_or_network(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("malformed preparation reached the provider")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(native, "_gateway", side_effect=forbidden):
            result, output = self.invoke(directory, {"package": self.preparation["package"]})
            self.assertEqual(2, result)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
