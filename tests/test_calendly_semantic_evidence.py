import contextlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from datetime import datetime

from scripts import calendly_collector as calendly


ROOT = Path(__file__).parents[1]


@contextlib.contextmanager
def _working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


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


def _assert_schema_contract(schema, value):
    def validate(declaration, candidate):
        if "$ref" in declaration:
            target = declaration["$ref"].split("/")[-1]
            return validate(schema["$defs"][target], candidate)
        kind = declaration.get("type")
        if kind == "object":
            if not isinstance(candidate, dict):
                raise AssertionError("expected object")
            for key in declaration.get("required", ()):
                if key not in candidate:
                    raise AssertionError("required field missing")
            if declaration.get("additionalProperties") is False:
                unknown = set(candidate) - set(declaration.get("properties", ()))
                if unknown:
                    raise AssertionError("additional property")
            for key, child in declaration.get("properties", {}).items():
                if key in candidate:
                    validate(child, candidate[key])
        elif kind == "array":
            if not isinstance(candidate, list):
                raise AssertionError("expected array")
            for item in candidate:
                validate(declaration["items"], item)
        elif kind == "string":
            if not isinstance(candidate, str):
                raise AssertionError("expected string")
            if len(candidate) < declaration.get("minLength", 0):
                raise AssertionError("string too short")
            if "pattern" in declaration and not re.search(declaration["pattern"], candidate):
                raise AssertionError("pattern mismatch")
            if declaration.get("format") == "date-time":
                try:
                    parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
                except ValueError as error:
                    raise AssertionError("invalid date-time") from error
                if parsed.tzinfo is None:
                    raise AssertionError("date-time must be aware")
        elif kind == "integer":
            if isinstance(candidate, bool) or not isinstance(candidate, int):
                raise AssertionError("expected integer")
            if candidate < declaration.get("minimum", candidate):
                raise AssertionError("integer below minimum")
        else:
            raise AssertionError("unsupported schema declaration")

    validate(schema, value)
    start = datetime.strptime(value["start"], "%Y-%m-%dT%H:%M:%SZ")
    end = datetime.strptime(value["end"], "%Y-%m-%dT%H:%M:%SZ")
    if end <= start:
        raise AssertionError("end must be after start")


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
        self.assertEqual(value["source_digest"], calendly.normalized_recording({
            "uri": "recordings/rec-1", "event_uri": "events/evt-1", "name": "Client review",
            "recording_start_time": "2026-08-04T10:00:00Z", "recording_end_time": "2026-08-04T10:37:00Z",
            "organizer": {"email": "vlad@example.test"}, "participants": [{"email": "client@example.test"}],
            "transcript": [{"offset_seconds": 0, "text": "Reviewed launch"}],
        })["source_digest"])

    def test_scheduled_event_without_recording_never_supplies_duration(self):
        value = calendly.scheduled_without_recording({
            "uri": "events/evt-2",
            "start_time": "2026-08-04T12:00:00Z",
            "end_time": "2026-08-04T13:00:00Z",
        })
        self.assertEqual("scheduled_without_recording", value["reason"])
        self.assertNotIn("duration_seconds", value)

    def test_recording_contract_schema_accepts_normalized_recording(self):
        schema = json.loads((ROOT / "schemas/calendly-recording-source-v1.json").read_text())
        _assert_schema_contract(schema, calendly.normalized_recording(recording()))

    def test_schema_contract_rejects_zero_duration(self):
        schema = json.loads((ROOT / "schemas/calendly-recording-source-v1.json").read_text())
        value = calendly.normalized_recording(recording())
        value["duration_seconds"] = 0
        with self.assertRaises(AssertionError):
            _assert_schema_contract(schema, value)

    def test_schema_contract_rejects_declared_type_and_shape_violations(self):
        schema = json.loads((ROOT / "schemas/calendly-recording-source-v1.json").read_text())
        value = calendly.normalized_recording(recording())
        invalid_values = (
            {**value, "extra": True},
            {**value, "participants": [{"email": 7}]},
            {**value, "transcript": [{"text": "ok", "extra": True}]},
            {**value, "duration_seconds": "2220"},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                _assert_schema_contract(schema, invalid)

    def test_schema_contract_rejects_missing_identity_naive_and_reversed_window(self):
        schema = json.loads((ROOT / "schemas/calendly-recording-source-v1.json").read_text())
        value = calendly.normalized_recording(recording())
        for invalid in (
            {**value, "recording_id": None},
            {**value, "start": "2026-08-04T10:00:00"},
            {**value, "end": "2026-08-04T09:59:59Z"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AssertionError):
                    _assert_schema_contract(schema, invalid)

    def test_recording_contract_rejects_missing_identity(self):
        with self.assertRaises(calendly.CalendlyCollectorError):
            calendly.normalized_recording({**recording(), "uri": ""})

    def test_recording_contract_rejects_naive_timestamp(self):
        with self.assertRaises(calendly.CalendlyCollectorError):
            calendly.normalized_recording({**recording(), "recording_start_time": "2026-08-04T10:00:00"})

    def test_recording_contract_rejects_fractional_or_offset_timestamp(self):
        for timestamp in ("2026-08-04T10:00:00.500Z", "2026-08-04T10:00:00+00:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(calendly.CalendlyCollectorError):
                calendly.normalized_recording({**recording(), "recording_start_time": timestamp})

    def test_recording_contract_rejects_malformed_people_and_transcript(self):
        for field, value in (("organizer", "bad"), ("participants", ["bad"]), ("transcript", ["bad"])):
            with self.subTest(field=field), self.assertRaises(calendly.CalendlyCollectorError):
                calendly.normalized_recording({**recording(), field: value})

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
        with tempfile.TemporaryDirectory() as directory, _working_directory(directory):
            output = Path(directory) / "result.json"
            args = calendly.parse_args([
                "preflight", "--since", "2026-08-04T00:00:00Z",
                "--until", "2026-08-05T00:00:00Z", "--output", str(output),
            ])
            self.assertEqual(output.resolve(), args.output)
            self.assertEqual(0, calendly.run(args))
            self.assertEqual("incomplete", json.loads(output.read_text())["status"])

    def test_collect_resolves_checkpoint_root_without_writing_outside_it(self):
        with tempfile.TemporaryDirectory() as directory, _working_directory(directory):
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
            self.assertEqual("incomplete", json.loads(output.read_text())["status"])
            self.assertFalse(root.exists())

    def test_cli_rejects_output_outside_invocation_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                calendly.parse_args([
                    "preflight", "--since", "2026-08-04T00:00:00Z", "--until", "2026-08-05T00:00:00Z",
                    "--output", str(Path(directory) / "outside.json"),
                ])

    def test_cli_rejects_output_traversal_outside_invocation_root(self):
        with tempfile.TemporaryDirectory() as directory, _working_directory(directory):
            with self.assertRaises(SystemExit):
                calendly.parse_args([
                    "preflight", "--since", "2026-08-04T00:00:00Z", "--until", "2026-08-05T00:00:00Z",
                    "--output", "nested/../../outside.json",
                ])

    def test_collect_rejects_checkpoint_traversal_outside_invocation_root(self):
        with tempfile.TemporaryDirectory() as directory, _working_directory(directory):
            with self.assertRaises(SystemExit):
                calendly.parse_args([
                    "collect", "--since", "2026-08-04T00:00:00Z", "--until", "2026-08-05T00:00:00Z",
                    "--output", "result.json", "--checkpoint-root", "nested/../../checkpoints",
                ])

    def test_cli_rejects_symlinked_output_component(self):
        with tempfile.TemporaryDirectory() as directory, _working_directory(directory):
            link = Path(directory) / "link"
            link.symlink_to(ROOT, target_is_directory=True)
            with self.assertRaises(SystemExit):
                calendly.parse_args([
                    "preflight", "--since", "2026-08-04T00:00:00Z", "--until", "2026-08-05T00:00:00Z",
                    "--output", str(link / "result.json"),
                ])

    def test_collect_rejects_symlinked_checkpoint_root(self):
        with tempfile.TemporaryDirectory() as directory, _working_directory(directory):
            link = Path(directory) / "link"
            link.symlink_to(ROOT, target_is_directory=True)
            with self.assertRaises(SystemExit):
                calendly.parse_args([
                    "collect", "--since", "2026-08-04T00:00:00Z", "--until", "2026-08-05T00:00:00Z",
                    "--output", str(Path(directory) / "result.json"), "--checkpoint-root", str(link),
                ])

    def test_cli_does_not_print_credentials_for_valid_run(self):
        secret = "calendly-secret-do-not-print"
        with tempfile.TemporaryDirectory() as directory, _working_directory(directory):
            args = calendly.parse_args([
                "preflight", "--since", "2026-08-04T00:00:00Z",
                "--until", "2026-08-05T00:00:00Z", "--output", str(Path(directory) / "result.json"),
            ])
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                calendly.run(args)
            self.assertNotIn(secret, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
