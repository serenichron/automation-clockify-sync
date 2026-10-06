import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_sync_collect as collector
from scripts import evidence_ledger as ledger
from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline


SINCE = dt.datetime(2026, 7, 21, tzinfo=dt.timezone.utc)
UNTIL = SINCE + dt.timedelta(days=1)
REASON = "native_semantic_projection_omits_tool_content"


class CodexToolTransportTests(unittest.TestCase):
    def fixture(self, directory):
        tool = "tool result é🙂\n" * 100000
        human = "Repair the billing export é🙂\n" * 500
        assistant = "Verified full client totals é🙂\n" * 500
        call = '{"cmd":"inspect totals é🙂"}'
        rows = [{"type": "session_meta", "payload": {"id": "synthetic", "cwd": "/work/client"}}]
        for timestamp, payload in (
            ("2026-07-21T05:00:13Z", {"type": "message", "role": "user", "content": human}),
            ("2026-07-21T05:01:17Z", {"type": "function_call", "name": "inspect", "arguments": call}),
            ("2026-07-21T05:02:19Z", {"type": "function_call_output", "call_id": "inspect-1", "output": tool}),
            ("2026-07-21T05:03:21Z", {"type": "message", "role": "assistant", "content": assistant}),
            ("2026-07-21T05:05:27Z", {"type": "message", "role": "user", "content": "Confirm the repaired totals."}),
        ):
            rows.append({"timestamp": timestamp, "type": "response_item", "payload": payload})
        path = directory / "rollout.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
        return path, tool, human, assistant, call

    def normalized(self, rows):
        return [event.document() for event in ledger.normalize_collector_snapshot(
            {"sessions": [{"machine": "synthetic", "codex_sessions": rows}]})]

    def test_local_tool_transport_retains_receipt_not_body_and_native_meaning(self):
        # Transporting tool bodies must not expand with tool output size, nor
        # change the provider-visible meaning or any human/assistant evidence.
        with tempfile.TemporaryDirectory() as tmp:
            path, tool, human, assistant, call = self.fixture(Path(tmp))
            original_file = path.read_bytes()
            rows = collector.parse_codex_rollout_file(path, "synthetic", SINCE, UNTIL)
            self.assertEqual(original_file, path.read_bytes())
        self.assertEqual(1, len(rows))
        events = rows[0]["events"]
        self.assertEqual(["user", "assistant", "tool", "assistant", "user"],
                         [event["role"] for event in events])
        self.assertEqual([human, call, "", assistant, "Confirm the repaired totals."],
                         [event["content"] for event in events])
        self.assertEqual("2026-07-21T08:02:19+03:00", events[2]["timestamp"])
        self.assertEqual("tool_result", events[2]["kind"])
        metadata = {"sha256": hashlib.sha256(tool.encode()).hexdigest(),
                    "byte_count": len(tool.encode()), "reason": REASON}
        self.assertEqual(metadata, events[2]["transport_omitted_tool_content"])
        self.assertLess(len(json.dumps(rows, ensure_ascii=False).encode()), 150000)
        old = copy.deepcopy(rows)
        old[0]["events"][2].pop("transport_omitted_tool_content")
        old[0]["events"][2]["content"] = tool
        before, after = self.normalized(old), self.normalized(rows)
        by_source = lambda values: {event["source_ref"]["source_id"]: event for event in values}
        before_map, after_map = by_source(before), by_source(after)
        changed = []
        for source_id, event in before_map.items():
            current = after_map[source_id]
            if event["evidence_id"] != current["evidence_id"]:
                changed.append(source_id)
                self.assertEqual("tool", event["attributes"]["role"])
                self.assertEqual(metadata, current["attributes"]["transport_omitted_tool_content"])
            else:
                self.assertEqual(event, current)
            first, second = semantic.project_event(event), semantic.project_event(current)
            first.pop("evidence_id")
            second.pop("evidence_id")
            self.assertEqual(first, second)
        self.assertEqual(["synthetic:event:3"], changed)
        # Native request bytes are unchanged for retained human/assistant members.
        retained = lambda values: [event for event in values
                                  if event.get("attributes", {}).get("role") in {"user", "assistant"}]
        self.assertEqual(json.dumps(semantic._body_for(retained(before), model="test", mode="extract",
                                                      private_text_approved=True), sort_keys=True),
                         json.dumps(semantic._body_for(retained(after), model="test", mode="extract",
                                                      private_text_approved=True), sort_keys=True))
        self.assertEqual(json.dumps(semantic._body_for(before, model="test", mode="extract",
                                                      private_text_approved=True), sort_keys=True),
                         json.dumps(semantic._body_for(after, model="test", mode="extract",
                                                      private_text_approved=True), sort_keys=True))

    def test_canonical_remote_export_uses_minimized_local_parser_and_attestation(self):
        # Exercise the actual export-local command with synthetic local paths;
        # a digest mismatch must return attestation before reading evidence.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path, tool, _, _, _ = self.fixture(home)
            path.rename(home / "rollout-test.jsonl")
            sessions = home / "sessions"
            sessions.mkdir()
            (home / "rollout-test.jsonl").rename(sessions / "rollout-test.jsonl")
            arguments = ["collect", "export-local", "--machine-json", json.dumps({
                "name": "synthetic", "codex_home": str(home),
            }), "--since", SINCE.isoformat(), "--until", UNTIL.isoformat(),
                "--expected-collector-sha256", collector.collector_script_sha256(), "--encoded-output"]
            output = io.StringIO()
            with mock.patch.object(collector.sys, "argv", arguments), contextlib.redirect_stdout(output):
                self.assertEqual(0, collector.main())
            result = collector.canonical_export_payload(output.getvalue(), "synthetic")
            event = result["codex_sessions"][0]["events"][2]
            self.assertEqual("", event["content"])
            self.assertEqual(len(tool.encode()), event["transport_omitted_tool_content"]["byte_count"])
            self.assertEqual(collector.collector_script_sha256(),
                             result["canonical_export_attestation"]["collector_script_sha256"])
            arguments[-2] = "wrong-digest"
            output = io.StringIO()
            with mock.patch.object(collector.sys, "argv", arguments), \
                 mock.patch.object(collector, "collect_local_sessions", side_effect=AssertionError("unattested read")), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(0, collector.main())
            result = collector.canonical_export_payload(output.getvalue(), "synthetic")
            self.assertEqual("unavailable", result["status"])
            self.assertNotIn("codex_sessions", result)

    def test_timestamp_collisions_preserve_actual_filtered_native_request_bytes(self):
        # The real boundary filters tool events BEFORE native chunking. Raw
        # content-bound tool-ID sort ties must not reorder retained members.
        with tempfile.TemporaryDirectory() as tmp:
            path, tool, _, _, _ = self.fixture(Path(tmp))
            rows = collector.parse_codex_rollout_file(path, "synthetic", SINCE, UNTIL)
        for member in (0, 1, 3, 4):
            with self.subTest(colliding_member=member):
                rows[0]["events"][2]["timestamp"] = rows[0]["events"][member]["timestamp"]
                old = copy.deepcopy(rows)
                old[0]["events"][2].pop("transport_omitted_tool_content")
                old[0]["events"][2]["content"] = tool
                bodies = []
                retained_inputs = []
                for values in (self.normalized(old), self.normalized(rows)):
                    retained, noise = pipeline._analysis_events(values)
                    self.assertCountEqual(["user", "assistant", "user"],
                                          [event["attributes"]["role"] for event in retained])
                    self.assertEqual(3, len(noise))  # summary, call, and result
                    hinted = pipeline._with_semantic_route_hints(retained, {})
                    retained_inputs.append(sorted(hinted, key=lambda event: event["evidence_id"]))
                    chunks = semantic.chunk_events(hinted, model="test", private_text_approved=True)
                    bodies.append([json.dumps(semantic._body_for(chunk, model="test", mode="extract",
                                                                 private_text_approved=True), sort_keys=True)
                                   for chunk in chunks])
                self.assertEqual(retained_inputs[0], retained_inputs[1])
                self.assertEqual(bodies[0], bodies[1])

    def test_omission_metadata_is_preserved_and_malformed_receipts_rejected(self):
        valid = {"role": "tool", "kind": "tool_result", "content": "", "timestamp": SINCE.isoformat(),
                 "transport_omitted_tool_content": {"sha256": "a" * 64, "byte_count": 128, "reason": REASON}}
        snapshot = lambda event: [{"session_id": "synthetic", "events": [event]}]
        normalized = self.normalized(snapshot(valid))
        message = next(event for event in normalized if event["source_type"] == "codex_sessions_event")
        self.assertEqual(valid["transport_omitted_tool_content"],
                         message["attributes"].get("transport_omitted_tool_content"))
        for field, value in (("sha256", "bad"), ("sha256", "g" * 64),
                             ("byte_count", -1), ("byte_count", True),
                             ("byte_count", 1.5), ("reason", "unknown"), ("extra", "unexpected")):
            with self.subTest(field=field, value=value):
                event = copy.deepcopy(valid)
                event["transport_omitted_tool_content"][field] = value
                with self.assertRaises(ValueError):
                    self.normalized(snapshot(event))
        for update in ({"role": "user"}, {"content": "not omitted"},
                       {"transport_omitted_tool_content": None}, {"transport_omitted_tool_content": {}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.normalized(snapshot({**valid, **update}))

    def test_historical_full_tool_payloads_still_normalize_without_receipts(self):
        old = [{"session_id": "synthetic", "events": [{
            "role": "tool", "kind": "tool_result", "content": "historical full output é🙂",
            "timestamp": SINCE.isoformat(),
        }]}]
        event = next(event for event in self.normalized(old) if event["source_type"] == "codex_sessions_event")
        self.assertEqual("historical full output é🙂", event["attributes"]["content"])
        self.assertNotIn("transport_omitted_tool_content", event["attributes"])


if __name__ == "__main__":
    unittest.main()
