"""Interrupted native-credit repairs resume only their sealed semantic fixture."""
import json
import unittest
from unittest import mock
from scripts import clockify_review_run as run
import test_cycle_native_credit_producer as fixtures


class NativeCreditRepairResumeTests(unittest.TestCase):
    def fixture(self):
        helper = fixtures.CycleNativeCreditProducerTests()
        transport, _config, _source, _manifest, _captured = helper.fixture()
        self.addCleanup(helper.doCleanups)
        with mock.patch.object(run, "RUNS", transport.runs):
            child = run._prepare_repair_run(transport.source)
        return transport, child

    def test_resume_passes_verified_fixture_to_accounting_instead_of_inference(self):
        transport, child = self.fixture()
        selected = []
        def process(args, directory, gate):
            fixture = getattr(args, "_repair_analysis_fixture", None)
            self.assertIsNotNone(fixture, "unfinished repair resume would perform new inference")
            self.assertEqual((transport.source / "semantic-analysis.json").read_bytes(), fixture.read_bytes())
            selected.append(directory)
            return 0, directory / "autopilot-result.json"
        with mock.patch.object(run, "_process_run", side_effect=process), mock.patch.object(run, "RUNS", transport.runs):
            self.assertEqual(0, run.main(["--runs-root", str(transport.runs), "--resume-from", str(child),
                "--state", str(transport.root / "review-state.json")]))
        self.assertEqual([child], selected)

    def test_foreign_repair_fixture_is_rejected_before_accounting(self):
        transport, child = self.fixture()
        lineage = json.loads((child / "repair-source.json").read_text())
        lineage["semantic_analysis_fixture"] = "../../foreign.json"
        (child / "repair-source.json").write_text(json.dumps(lineage))
        with mock.patch.object(run, "_process_run", side_effect=AssertionError("fixture bypass")), mock.patch.object(run, "RUNS", transport.runs):
            self.assertEqual(2, run.main(["--runs-root", str(transport.runs), "--resume-from", str(child),
                "--state", str(transport.root / "review-state.json")]))
