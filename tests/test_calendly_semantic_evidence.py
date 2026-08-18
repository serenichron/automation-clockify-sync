import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import calendly_collector as calendly


ROOT = Path(__file__).parents[1]


def recording():
    return {
        "uri": "recordings/rec-1",
        "event_uri": "events/evt-1",
        "name": "Client review",
        "recording_start_time": "2026-08-04T10:00:00Z",
        "recording_end_time": "2026-08-04T10:37:00Z",
        "organizer": {"email": "vlad@example.test"},
        "participants": [{"email": "client@example.test"}],
        "join_url": "https://example.test/join",
        "transcript": [{"offset_seconds": 0, "text": "Reviewed launch"}],
        "summary": "Reviewed launch",
    }


class CalendlyRecordingContractTests(unittest.TestCase):
    def test_recording_preserves_exact_window_and_semantics(self):
        value = calendly.normalized_recording({
            "uri": "recordings/rec-1",
            "event_uri": "events/evt-1",
            "name": "Client review",
            "recording_start_time": "2026-08-04T10:00:00Z",
            "recording_end_time": "2026-08-04T10:37:00Z",
            "organizer": {"email": "vlad@example.test"},
            "participants": [{"email": "client@example.test"}],
            "transcript": [{"offset_seconds": 0, "text": "Reviewed launch"}],
        })
        self.assertEqual(2220, value["duration_seconds"])
        self.assertEqual("2026-08-04T10:00:00Z", value["start"])
        self.assertEqual("2026-08-04T10:37:00Z", value["end"])
        self.assertTrue(value["source_digest"].startswith("sha256:"))

    def test_scheduled_event_without_recording_never_supplies_duration(self):
        value = calendly.scheduled_without_recording({
            "uri": "events/evt-2",
            "start_time": "2026-08-04T12:00:00Z",
            "end_time": "2026-08-04T13:00:00Z",
        })
        self.assertEqual("scheduled_without_recording", value["reason"])
        self.assertNotIn("duration_seconds", value)

    def test_recording_contract_schema_accepts_normalized_recording(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema = json.loads((ROOT / "schemas/calendly-recording-source-v1.json").read_text())
        jsonschema.validate(calendly.normalized_recording(recording()), schema)

    def test_recording_contract_rejects_missing_identity(self):
        with self.assertRaises(calendly.CalendlyCollectorError):
            calendly.normalized_recording({**recording(), "uri": ""})

    def test_recording_contract_rejects_naive_timestamp(self):
        with self.assertRaises(calendly.CalendlyCollectorError):
            calendly.normalized_recording({**recording(), "recording_start_time": "2026-08-04T10:00:00"})

    def test_recording_contract_rejects_end_before_start(self):
        with self.assertRaises(calendly.CalendlyCollectorError):
            calendly.normalized_recording({**recording(), "recording_end_time": "2026-08-04T09:59:59Z"})


class CalendlyCliContractTests(unittest.TestCase):
    def test_preflight_requires_explicit_boundaries_and_output(self):
        with self.assertRaises(SystemExit):
            calendly.parse_args(["preflight"])

    def test_collect_requires_explicit_boundaries_and_output(self):
        with self.assertRaises(SystemExit):
            calendly.parse_args(["collect"])

    def test_preflight_resolves_output_inside_requested_root(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            args = calendly.parse_args([
                "preflight", "--since", "2026-08-04T00:00:00Z",
                "--until", "2026-08-05T00:00:00Z", "--output", str(output),
            ])
            self.assertEqual(output.resolve(), args.output)
            self.assertEqual(0, calendly.run(args))
            self.assertEqual("ready", json.loads(output.read_text())["status"])

    def test_collect_resolves_checkpoint_root_without_writing_outside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkpoints"
            output = Path(directory) / "result.json"
            args = calendly.parse_args([
                "collect", "--since", "2026-08-04T00:00:00Z",
                "--until", "2026-08-05T00:00:00Z", "--output", str(output),
                "--checkpoint-root", str(root),
            ])
            self.assertEqual(root.resolve(), args.checkpoint_root)
            self.assertEqual(0, calendly.run(args))
            self.assertEqual([], json.loads(output.read_text())["recordings"])
            self.assertFalse(root.exists())

    def test_cli_does_not_print_credentials_for_valid_run(self):
        secret = "calendly-secret-do-not-print"
        with tempfile.TemporaryDirectory() as directory:
            args = calendly.parse_args([
                "preflight", "--since", "2026-08-04T00:00:00Z",
                "--until", "2026-08-05T00:00:00Z", "--output", str(Path(directory) / "result.json"),
            ])
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                calendly.run(args)
            self.assertNotIn(secret, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
