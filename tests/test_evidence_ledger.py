"""Contract tests for immutable evidence-ledger and regression-corpus v1."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import semantic_analyzer


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evidence_ledger.py"
SPEC = importlib.util.spec_from_file_location("evidence_ledger", SCRIPT)
assert SPEC and SPEC.loader
ledger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ledger
SPEC.loader.exec_module(ledger)

CORPUS_DIR = ROOT / "tests" / "fixtures" / "clockify-regression" / "v1"
EXPECTED_RECORD_COUNT = 86
EXPECTED_CORPUS_DIGEST = "43b010aa6bc9c18612e8a3a855244be7bd492364ce28fa40a65cbd341def8618"

CALENDLY_RECORDING = {
    "recording_id": "rec-1",
    "meeting_id": "event-1",
    "title": "Client review",
    "start": "2026-08-01T10:00:00Z",
    "end": "2026-08-01T10:37:00Z",
    "duration_seconds": 2220,
    "organizer": {"email": "organizer@example.test", "name": "Organizer"},
    "participants": [{"email": "participant@example.test", "name": "Participant"}],
    "join_url": "https://example.test/join/rec-1",
    "summary": "Reviewed launch readiness",
    "transcript": [{"offset_seconds": 0, "speaker": "Participant", "text": "Ready."}],
    "source_digest": "sha256:" + "a" * 64,
}


def complete_snapshot(**sources):
    snapshot = {
        "clockify": {"status": "ok", "complete": True, "entries": []},
        "fathom": {"status": "ok", "complete": True, "meetings": []},
        "multica_issues": {"status": "ok", "complete": True, "issues": []},
    }
    snapshot.update(sources)
    return snapshot


def event(*, source_id: str = "session-1", aliases: dict[str, str] | None = None, start: str = "2026-07-31T09:00:00Z"):
    return ledger.evidence_event(
        "codex_session",
        {"source_id": source_id, "machine": "precision"},
        observed_at=start,
        raw_source_span={"start": start, "end": "2026-07-31T10:00:00Z", "path": "/private/rollout.jsonl"},
        attributes={"label": "focused accounting analysis"},
        legacy_aliases=aliases or {},
    )


def _assert_schema_contract(schema: dict, declaration: dict, candidate: object) -> None:
    if "$ref" in declaration:
        _assert_schema_contract(schema, schema["$defs"][declaration["$ref"].split("/")[-1]], candidate)
        return
    if "const" in declaration:
        if candidate != declaration["const"]:
            raise AssertionError("const violation")
    kind = declaration.get("type")
    if kind == "object":
        if not isinstance(candidate, dict):
            raise AssertionError("expected object")
        for key in declaration.get("required", ()):
            if key not in candidate:
                raise AssertionError(f"required field missing: {key}")
        if declaration.get("additionalProperties") is False:
            unknown = set(candidate) - set(declaration.get("properties", ()))
            if unknown:
                raise AssertionError(f"additional properties: {unknown}")
        if len(candidate) < declaration.get("minProperties", 0):
            raise AssertionError("too few properties")
        for key, child in declaration.get("properties", {}).items():
            if key in candidate:
                _assert_schema_contract(schema, child, candidate[key])
    elif kind == "array":
        if not isinstance(candidate, list):
            raise AssertionError("expected array")
        for item in candidate:
            _assert_schema_contract(schema, declaration["items"], item)
    elif kind == "string":
        if not isinstance(candidate, str):
            raise AssertionError("expected string")
        if len(candidate) < declaration.get("minLength", 0):
            raise AssertionError("string too short")
        if "pattern" in declaration and not re.search(declaration["pattern"], candidate):
            raise AssertionError("pattern violation")
    elif kind == "integer":
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            raise AssertionError("expected integer")
        if candidate < declaration.get("minimum", candidate):
            raise AssertionError("minimum violation")
    elif kind is None and (
        "required" in declaration or "properties" in declaration or "minProperties" in declaration
    ):
        if not isinstance(candidate, dict):
            raise AssertionError("expected object")
        for key in declaration.get("required", ()):
            if key not in candidate:
                raise AssertionError(f"required field missing: {key}")
        if len(candidate) < declaration.get("minProperties", 0):
            raise AssertionError("too few properties")
        for key, child in declaration.get("properties", {}).items():
            if key in candidate:
                _assert_schema_contract(schema, child, candidate[key])
    for branch in declaration.get("allOf", ()):
        if "if" in branch:
            condition = branch["if"].get("properties", {}).get("source_type", {})
            if isinstance(candidate, dict) and candidate.get("source_type") != condition.get("const"):
                continue
            _assert_schema_contract(schema, branch.get("then", {}), candidate)
        else:
            _assert_schema_contract(schema, branch, candidate)


class EvidenceLedgerTests(unittest.TestCase):
    def test_calendly_recording_is_immutable_evidence_with_complete_inventory(self) -> None:
        snapshot = complete_snapshot(
            calendly={"status": "ok", "complete": True, "recordings": [CALENDLY_RECORDING]}
        )
        events = ledger.normalize_collector_snapshot(snapshot)
        meeting = next(event for event in events if event.source_type == "calendly")
        inventory = ledger.source_inventory_from_collector(snapshot)
        self.assertEqual("rec-1", meeting.attributes["recording_id"])
        self.assertEqual(
            {"status": "complete", "expected_count": 1, "observed_count": 1},
            inventory["calendly"],
        )

    def test_calendly_event_preserves_normalized_semantics_and_excludes_envelope_metadata(self) -> None:
        snapshot = complete_snapshot(
            calendly={
                "status": "ok",
                "complete": True,
                "recordings": [CALENDLY_RECORDING],
                "credentials": {"token": "must-not-leak"},
                "pagination": {"next_page_token": "must-not-leak"},
                "path": "/private/recordings.json",
                "cwd": "/private",
            }
        )
        meeting = next(
            event for event in ledger.normalize_collector_snapshot(snapshot)
            if event.source_type == "calendly"
        )
        self.assertEqual("2026-08-01T10:00:00Z", meeting.raw_source_span["start"])
        self.assertEqual("2026-08-01T10:37:00Z", meeting.raw_source_span["end"])
        self.assertEqual("event-1", meeting.attributes["meeting_id"])
        self.assertEqual(tuple(ledger.FrozenDict(CALENDLY_RECORDING["participants"][0]).items()), tuple(meeting.attributes["participants"][0].items()))
        self.assertEqual(CALENDLY_RECORDING["summary"], meeting.attributes["summary"])
        self.assertEqual(tuple(ledger.FrozenDict(CALENDLY_RECORDING["transcript"][0]).items()), tuple(meeting.attributes["transcript"][0].items()))
        self.assertEqual(CALENDLY_RECORDING["source_digest"], meeting.attributes["source_digest"])
        self.assertNotIn("credentials", meeting.document())
        self.assertNotIn("pagination", meeting.document())
        self.assertNotIn("/private", ledger.canonical_json(meeting.document()))

    def test_calendly_missing_or_partial_collection_is_incomplete(self) -> None:
        for calendly in (
            {"status": "missing_credentials", "complete": False, "recordings": []},
            {"status": "incomplete", "complete": False, "recordings": [CALENDLY_RECORDING]},
        ):
            with self.subTest(status=calendly["status"]):
                inventory = ledger.source_inventory_from_collector(complete_snapshot(calendly=calendly))
                if calendly["status"] == "missing_credentials":
                    self.assertEqual("unavailable", inventory["calendly"]["status"])
                self.assertEqual("incomplete", ledger.source_completeness(inventory)["status"])

    def test_calendly_malformed_expected_count_fails_closed(self) -> None:
        inventory = ledger.source_inventory_from_collector(
            complete_snapshot(
                calendly={
                    "status": "ok",
                    "complete": True,
                    "expected_count": "1",
                    "recordings": [CALENDLY_RECORDING],
                }
            )
        )
        self.assertEqual("partial", inventory["calendly"]["status"])
        self.assertEqual(1, inventory["calendly"]["observed_count"])
        self.assertEqual("incomplete", ledger.source_completeness(inventory)["status"])

    def test_calendly_explicit_recording_count_mismatch_fails_closed(self) -> None:
        inventory = ledger.source_inventory_from_collector(
            complete_snapshot(
                calendly={
                    "status": "ok",
                    "complete": True,
                    "recording_count": 2,
                    "recordings": [CALENDLY_RECORDING],
                }
            )
        )
        self.assertEqual("partial", inventory["calendly"]["status"])
        self.assertEqual(2, inventory["calendly"]["expected_count"])
        self.assertEqual(1, inventory["calendly"]["observed_count"])
        self.assertEqual("incomplete", ledger.source_completeness(inventory)["status"])

    def test_calendly_inventory_count_mismatch_with_events_blocks_completeness(self) -> None:
        snapshot = complete_snapshot(
            calendly={"status": "ok", "complete": True, "recordings": [CALENDLY_RECORDING]}
        )
        events = ledger.normalize_collector_snapshot(snapshot)
        evidence = ledger.EvidenceLedger(tuple(event for event in events if event.source_type != "calendly"), ledger.source_inventory_from_collector(snapshot))
        self.assertEqual("incomplete", evidence.manifest.document()["source_completeness"]["status"])

    def test_calendly_source_digest_keeps_event_digest_stable(self) -> None:
        recording = dict(CALENDLY_RECORDING)
        reordered = {key: recording[key] for key in reversed(tuple(recording))}
        first = ledger.normalize_collector_snapshot(complete_snapshot(calendly={"status": "ok", "complete": True, "recordings": [recording]}))
        second = ledger.normalize_collector_snapshot(complete_snapshot(calendly={"status": "ok", "complete": True, "recordings": [reordered]}))
        self.assertEqual("sha256:" + "a" * 64, first[0].attributes["source_digest"])
        self.assertEqual(ledger.event_digest(first), ledger.event_digest(second))

    def test_malformed_calendly_record_is_not_an_event_and_blocks_inventory(self) -> None:
        malformed_variants = (
            {key: value for key, value in CALENDLY_RECORDING.items() if key != "recording_id"},
            {**CALENDLY_RECORDING, "meeting_id": ""},
            {**CALENDLY_RECORDING, "start": "2026-08-01T10:00:00+00:00"},
            {**CALENDLY_RECORDING, "duration_seconds": 2221},
            {**CALENDLY_RECORDING, "source_digest": "sha256:not-a-full-digest"},
            {**CALENDLY_RECORDING, "cursor": "private-cursor"},
            {**CALENDLY_RECORDING, "participants": [{"email": 7}]},
            {**CALENDLY_RECORDING, "transcript": [{"text": "ok", "extra": True}]},
        )
        for recording in malformed_variants:
            with self.subTest(recording=recording):
                snapshot = complete_snapshot(
                    calendly={"status": "ok", "complete": True, "recordings": [recording]}
                )
                events = ledger.normalize_collector_snapshot(snapshot)
                inventory = ledger.source_inventory_from_collector(snapshot)
                self.assertFalse(any(event.source_type == "calendly" for event in events))
                self.assertEqual("partial", inventory["calendly"]["status"])
                self.assertEqual("incomplete", ledger.source_completeness(inventory)["status"])

    def test_calendly_event_constructor_rejects_fallback_or_malformed_identity(self) -> None:
        with self.assertRaises(ValueError):
            ledger.evidence_event(
                "calendly",
                {"source_type": "calendly", "source_id": "row-1"},
                observed_at=CALENDLY_RECORDING["start"],
                raw_source_span={"start": CALENDLY_RECORDING["start"], "end": CALENDLY_RECORDING["end"]},
                attributes={key: value for key, value in CALENDLY_RECORDING.items() if key not in {"start", "end", "meeting_id"}},
            )
        valid_ref = {"source_type": "calendly", "source_id": "rec-1", "meeting_id": "event-1"}
        valid_attrs = {key: value for key, value in CALENDLY_RECORDING.items() if key not in {"start", "end"}}
        for extra_key in ("start", "end"):
            with self.subTest(extra_key=extra_key), self.assertRaises(ValueError):
                ledger.evidence_event(
                    "calendly",
                    valid_ref,
                    observed_at=CALENDLY_RECORDING["start"],
                    raw_source_span={"start": CALENDLY_RECORDING["start"], "end": CALENDLY_RECORDING["end"]},
                    attributes={**valid_attrs, extra_key: CALENDLY_RECORDING[extra_key]},
                )

    def test_calendly_event_schema_is_strict_and_accepts_generated_event(self) -> None:
        schema = json.loads((ROOT / "schemas" / "evidence-ledger-v1.json").read_text())
        event_schema = schema["$defs"]["event"]
        attrs_schema = schema["$defs"]["calendlyAttributes"]
        source_ref_schema = schema["$defs"]["calendlySourceRef"]
        span_schema = schema["$defs"]["calendlySourceSpan"]
        self.assertFalse(attrs_schema["additionalProperties"])
        self.assertFalse(source_ref_schema["additionalProperties"])
        self.assertFalse(span_schema["additionalProperties"])
        self.assertEqual(
            set(attrs_schema["required"]),
            set(CALENDLY_RECORDING) - {"start", "end"},
        )
        event = next(
            item
            for item in ledger.normalize_collector_snapshot(
                complete_snapshot(
                    calendly={"status": "ok", "complete": True, "recordings": [CALENDLY_RECORDING]}
                )
            )
            if item.source_type == "calendly"
        )
        _assert_schema_contract(schema, schema["$defs"]["event"], event.document())
        for location, key, value in (
            ("attributes", "credentials", {"token": "secret"}),
            ("attributes", "pagination", {"cursor": "secret"}),
            ("source_ref", "path", "/private/recordings.json"),
            ("raw_source_span", "cwd", "/private"),
        ):
            with self.subTest(location=location, key=key):
                invalid = copy.deepcopy(event.document())
                invalid[location][key] = value
                with self.assertRaises(AssertionError):
                    _assert_schema_contract(schema, schema["$defs"]["event"], invalid)
        self.assertEqual(set(source_ref_schema["properties"]), set(event.source_ref))
        self.assertEqual(set(span_schema["properties"]), set(event.raw_source_span))
        self.assertEqual(set(attrs_schema["properties"]), set(event.attributes))
        self.assertRegex(event.attributes["source_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_content_address_is_stable_across_mapping_and_event_order(self) -> None:
        first = ledger.evidence_event(
            "codex_session",
            {"machine": "precision", "source_id": "one"},
            raw_source_span={"end": "2026-07-31T10:00:00Z", "start": "2026-07-31T09:00:00Z"},
            attributes={"b": 2, "a": 1},
        )
        same = ledger.evidence_event(
            "codex_session",
            {"source_id": "one", "machine": "precision"},
            raw_source_span={"start": "2026-07-31T09:00:00Z", "end": "2026-07-31T10:00:00Z"},
            attributes={"a": 1, "b": 2},
        )
        second = event(source_id="two")
        self.assertEqual(first.evidence_id, same.evidence_id)
        self.assertEqual(
            ledger.EvidenceLedger((first, second)).manifest.events_digest,
            ledger.EvidenceLedger((second, same)).manifest.events_digest,
        )

    def test_append_is_immutable_and_exact_duplicates_are_idempotent(self) -> None:
        first = event()
        initial = ledger.EvidenceLedger((first,))
        appended = initial.append((first, event(source_id="two")))
        self.assertEqual(1, len(initial.events))
        self.assertEqual(2, len(appended.events))
        self.assertEqual(2, appended.manifest.event_count)
        self.assertEqual(ledger.event_digest((first,)), ledger.event_digest((first, first)))

    def test_nested_event_values_are_immutable(self) -> None:
        item = event()
        with self.assertRaises(TypeError):
            item.attributes["label"] = "mutated"  # type: ignore[index]
        with self.assertRaises(TypeError):
            item.raw_source_span["start"] = "mutated"  # type: ignore[index]

    def test_overlapping_evidence_is_preserved(self) -> None:
        left = event(source_id="one", start="2026-07-31T09:00:00Z")
        right = event(source_id="two", start="2026-07-31T09:30:00Z")
        evidence = ledger.EvidenceLedger((left, right))
        self.assertEqual(2, len(evidence.events))
        self.assertNotEqual(left.evidence_id, right.evidence_id)

    def test_manifest_detects_tampering(self) -> None:
        evidence = ledger.EvidenceLedger((event(),))
        manifest = evidence.manifest
        evidence.validate(manifest)
        tampered = copy.deepcopy(manifest.document())
        tampered["event_count"] = 2
        with self.assertRaisesRegex(ValueError, "Manifest ID"):
            ledger.LedgerManifest.from_document(tampered)
        forged = ledger.LedgerManifest(
            events_digest=manifest.events_digest,
            event_count=2,
            source_inventory=manifest.source_inventory,
        )
        with self.assertRaisesRegex(ValueError, "validation failed"):
            evidence.validate(forged)

    def test_event_document_tampering_is_detected(self) -> None:
        original = event()
        document = original.document()
        document["attributes"]["label"] = "changed after hashing"
        with self.assertRaisesRegex(ValueError, "Evidence ID"):
            ledger.EvidenceEvent.from_document(document)

    def test_source_completeness_distinguishes_partial_and_count_mismatch(self) -> None:
        complete = ledger.source_completeness({"sessions": {"status": "complete", "expected_count": 2, "observed_count": 2}})
        partial = ledger.source_completeness({"sessions": {"status": "partial", "expected_count": 2, "observed_count": 1}})
        mismatch = ledger.source_completeness({"fathom": {"status": "complete", "expected_count": 2, "observed_count": 1}})
        self.assertEqual("complete", complete["status"])
        self.assertEqual(["sessions"], partial["incomplete_sources"])
        self.assertEqual(["fathom"], mismatch["incomplete_sources"])

    def test_legacy_candidate_and_review_aliases_resolve(self) -> None:
        item = event(aliases={"candidate_key": "ck-legacy", "review_item_id": "rvi-legacy"})
        evidence = ledger.EvidenceLedger((item,))
        self.assertEqual(item, evidence.resolve("candidate_key:ck-legacy"))
        self.assertEqual(item, evidence.resolve("review_item_id:rvi-legacy"))
        self.assertEqual(item, evidence.resolve(item.evidence_id))

    def test_ambiguous_alias_is_rejected(self) -> None:
        left = event(source_id="one", aliases={"candidate_key": "ck-same"})
        right = event(source_id="two", aliases={"candidate_key": "ck-same"})
        with self.assertRaisesRegex(ValueError, "Ambiguous legacy alias"):
            ledger.EvidenceLedger((left, right)).aliases()

    def test_snapshot_builder_retains_source_span_without_allocations(self) -> None:
        snapshot = {
            "clockify": {"entries": [{"id": "clock-1", "start": "2026-07-31T09:00:00Z", "end": "2026-07-31T10:00:00Z", "description": "Existing entry"}]},
            "fathom": {"meetings": [{"recording_id": "meeting-1", "start": "2026-07-31T10:00:00Z", "end": "2026-07-31T10:30:00Z", "title": "Planning"}]},
            "multica_issues": {"issues": [{"id": "SER-1", "status": "open", "title": "Track evidence"}]},
            "enriched_context": [{"id": "context-1", "timestamp": "2026-07-31T11:00:00Z", "label": "context"}],
            "sessions": [{"machine": "precision", "codex_sessions": [{"session_id": "session-1", "start": "2026-07-31T12:00:00Z", "end": "2026-07-31T12:30:00Z", "path": "/private/session.jsonl"}]}],
        }
        events = ledger.normalize_collector_snapshot(snapshot)
        self.assertEqual(5, len(events))
        session = next(item for item in events if item.source_type == "codex_sessions")
        self.assertEqual("/private/session.jsonl", session.raw_source_span["path"])
        self.assertTrue(all("allocation" not in item.document() for item in events))

    def test_internal_datetime_helpers_do_not_leak_into_immutable_evidence(self) -> None:
        parsed = __import__("datetime").datetime(2026, 8, 1, 10, 0)
        snapshot = {
            "enriched_context": {
                "claude_contexts": [{
                    "id": "context-one",
                    "timestamp": "2026-08-01T10:00:00",
                    "user_messages": [{
                        "user_message": "Fix evidence serialization",
                        "_parsed_ts": parsed,
                    }],
                }]
            }
        }
        events = ledger.normalize_collector_snapshot(snapshot)
        rendered = ledger.canonical_json(events[0].document())
        self.assertNotIn("_parsed_ts", rendered)
        self.assertIn("Fix evidence serialization", rendered)

    def test_session_messages_are_distinct_complete_events_with_explicit_missing_time(self) -> None:
        full_content = "First complete message, including the decisive implementation detail."
        snapshot = {
            "sessions": [{
                "machine": "precision",
                "status": "ok",
                "codex_sessions": [{
                    "session_id": "session-complete",
                    "start": "2026-08-01T09:00:00Z",
                    "end": "2026-08-01T10:00:00Z",
                    "path": "/private/session.jsonl",
                    "title": "Complete context",
                    "events": [
                        {"timestamp": "2026-08-01T09:05:00Z", "role": "user", "kind": "message", "content": full_content},
                        {"role": "assistant", "kind": "tool", "tool_name": "exec_command", "content": "Complete tool result"},
                    ],
                }],
            }],
        }
        events = ledger.normalize_collector_snapshot(snapshot)
        metadata = next(item for item in events if item.source_type == "codex_sessions")
        message_events = sorted((item for item in events if item.source_type == "codex_sessions_event"), key=lambda item: item.source_ref["ordinal"])
        self.assertEqual(3, len(events))
        self.assertEqual(2, metadata.attributes["event_count"])
        self.assertEqual(full_content, message_events[0].attributes["content"])
        self.assertEqual("session-complete", message_events[0].source_ref["session_id"])
        self.assertEqual("precision", message_events[0].source_ref["machine"])
        self.assertEqual(1, message_events[0].source_ref["ordinal"])
        self.assertEqual("2026-08-01T09:05:00Z", message_events[0].observed_at)
        self.assertIsNone(message_events[1].observed_at)
        self.assertEqual("missing", message_events[1].raw_source_span["timestamp_status"])
        self.assertNotIn("timestamp", message_events[1].raw_source_span)

    def test_fathom_and_multica_semantic_fields_are_preserved(self) -> None:
        summary = "Defined the handoff and accepted the remediation plan."
        action_items = [{"text": "Prepare the scope"}]
        transcript = [{"speaker": "Participant", "text": "Confirm the scope"}]
        snapshot = {
            "fathom": {"status": "ok", "complete": True, "meetings": [{
                "recording_id": "meeting-1", "title": "Planning", "start": "2026-08-01T10:00:00Z",
                "summary": summary, "action_items": action_items, "transcript": transcript,
                "semantic_evidence_status": "available",
            }]},
            "multica_issues": {"status": "ok", "issues": [{"id": "SER-1", "key": "SER-1", "title": "Evidence contract", "status": "in_progress"}]},
        }
        events = ledger.normalize_collector_snapshot(snapshot)
        fathom = next(item for item in events if item.source_type == "fathom")
        multica = next(item for item in events if item.source_type == "multica")
        self.assertEqual(summary, fathom.attributes["summary"])
        self.assertEqual(tuple(action_items), fathom.attributes["action_items"])
        self.assertEqual(tuple(ledger.FrozenDict(item) for item in transcript), fathom.attributes["transcript"])
        self.assertEqual("available", fathom.attributes["semantic_evidence_status"])
        self.assertEqual("Evidence contract", multica.attributes["title"])
        self.assertEqual("in_progress", multica.attributes["status"])

    def test_multica_observed_at_tracks_created_updated_and_completed_activity(self) -> None:
        snapshot = {
            "multica_issues": {
                "issues": [
                    {
                        "id": "SER-created",
                        "status": "open",
                        "created_at": "2026-08-01T09:00:00Z",
                    },
                    {
                        "id": "SER-updated",
                        "status": "in_progress",
                        "created_at": "2026-08-01T09:00:00Z",
                        "updated_at": "2026-08-02T10:00:00Z",
                    },
                    {
                        "id": "SER-completed",
                        "status": "completed",
                        "created_at": "2026-08-01T09:00:00Z",
                        "updated_at": "2026-08-02T10:00:00Z",
                        "completed_at": "2026-08-03T11:00:00Z",
                    },
                ]
            }
        }

        events = ledger.normalize_collector_snapshot(snapshot)
        observed = {item.source_ref["source_id"]: item.observed_at for item in events}

        self.assertEqual(
            {
                "SER-created": "2026-08-01T09:00:00Z",
                "SER-updated": "2026-08-02T10:00:00Z",
                "SER-completed": "2026-08-03T11:00:00Z",
            },
            observed,
        )

    def test_multica_activity_timestamp_controls_semantic_day_chunking(self) -> None:
        snapshot = {
            "multica_issues": {
                "issues": [
                    {
                        "id": "SER-updated",
                        "status": "in_progress",
                        "created_at": "2026-08-01T09:00:00Z",
                        "updated_at": "2026-08-02T10:00:00Z",
                    },
                    {
                        "id": "SER-completed",
                        "status": "completed",
                        "created_at": "2026-08-01T09:00:00Z",
                        "updated_at": "2026-08-02T10:00:00Z",
                        "completed_at": "2026-08-03T11:00:00Z",
                    },
                ]
            }
        }

        events = [item.document() for item in ledger.normalize_collector_snapshot(snapshot)]
        chunks = semantic_analyzer.chunk_events(events, max_body_bytes=50_000)

        self.assertEqual(2, len(chunks))
        self.assertEqual(
            ["2026-08-02", "2026-08-03"],
            [chunk[0]["time_span"]["start"][:10] for chunk in chunks],
        )

    def test_repository_commits_and_artifacts_are_normalized_without_allocation(self) -> None:
        snapshot = {
            "clockify": {"status": "ok", "entries": []},
            "fathom": {"status": "ok", "meetings": []},
            "multica_issues": {"status": "ok", "issues": []},
            "sessions": [{
                "machine": "macbook",
                "status": "ok",
                "repository_evidence_status": "complete",
                "repository_events": [{
                    "id": "repo:commit",
                    "source": "git_commit",
                    "machine": "macbook",
                    "commit_sha": "abc123",
                    "repository_root": "/work/repo",
                    "cwd": "/work/repo",
                    "start": "2026-08-01T10:00:00+03:00",
                    "end": "2026-08-01T10:00:00+03:00",
                    "subject": "Fix stable review identity",
                    "artifacts": ["scripts/review.py", "tests/test_review.py"],
                }],
            }],
        }
        events = ledger.normalize_collector_snapshot(snapshot)
        commit = next(item for item in events if item.source_type == "repository_events")
        self.assertEqual("abc123", commit.attributes["commit_sha"])
        self.assertEqual(
            ("scripts/review.py", "tests/test_review.py"), commit.attributes["artifacts"]
        )
        self.assertNotIn("allocation", commit.document())
        inventory = ledger.source_inventory_from_collector(snapshot)
        self.assertEqual(
            {"status": "complete", "observed_count": 1},
            inventory["repositories/macbook"],
        )

    def test_source_inventory_is_derived_from_partial_collector_metadata(self) -> None:
        snapshot = {
            "clockify": {"status": "ok", "entries": []},
            "fathom": {"status": "partial", "complete": False, "expected_count": 2, "meetings": [{"recording_id": "r1"}]},
            "multica_issues": {"status": "missing_profile", "issue_count": 0},
            "sessions": [{"machine": "macbook", "status": "unavailable", "errors": ["not reachable"], "codex_sessions": []}],
        }
        inventory = ledger.source_inventory_from_collector(snapshot)
        self.assertEqual({"status": "complete", "observed_count": 0}, inventory["clockify"])
        self.assertEqual({"status": "partial", "expected_count": 2, "observed_count": 1}, inventory["fathom"])
        self.assertEqual("unavailable", inventory["multica_issues"]["status"])
        self.assertEqual("unavailable", inventory["sessions/macbook"]["status"])
        self.assertEqual("incomplete", ledger.source_completeness(inventory)["status"])


class RegressionCorpusTests(unittest.TestCase):
    def _records(self) -> list[dict]:
        return [json.loads(line) for line in (CORPUS_DIR / "records.jsonl").read_text(encoding="utf-8").splitlines() if line]

    def test_versioned_corpus_has_exact_count_and_digest(self) -> None:
        records = self._records()
        manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(EXPECTED_RECORD_COUNT, len(records))
        self.assertEqual(EXPECTED_RECORD_COUNT, manifest["source"]["logical_record_count"])
        self.assertEqual(EXPECTED_CORPUS_DIGEST, ledger.corpus_digest(records))
        self.assertEqual(EXPECTED_CORPUS_DIGEST, manifest["records_digest"])

    def test_corpus_contains_only_sanitized_behavioral_contracts(self) -> None:
        records = self._records()
        sensitive = re.compile(r"(?:\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|https?://|www\.|(?:^|\s)/(?:Users|home)/|\b[a-fA-F0-9]{32,}\b)")
        for record in records:
            self.assertIn(
                record["expected_disposition"],
                {"omit", "exception", "render", "split"},
            )
            self.assertFalse(sensitive.search(canonical := ledger.canonical_json(record)), canonical)
            self.assertRegex(record["record_id"], r"^clockify-regression-v1-\d{3}$")
            self.assertTrue(
                all(
                    re.fullmatch(r"[a-z_]+", item)
                    for item in record["behavior_categories"]
                )
            )

    def test_redactor_never_emits_sensitive_input(self) -> None:
        raw = "[NEEDS REVIEW] inspect https://example.test/a for person@example.test at /Users/private/a 0123456789abcdef0123456789abcdef"
        sanitized, classes = ledger.redact_legacy_text(raw)
        self.assertNotIn("example.test", sanitized)
        self.assertNotIn("person@example", sanitized)
        self.assertNotIn("/Users/private", sanitized)
        self.assertIn("email", classes)
        self.assertIn("url", classes)
        self.assertIn("path", classes)
        self.assertIn("hash", classes)

    def test_source_features_are_non_identifying_and_deterministic(self) -> None:
        text = (
            "[NEEDS\u00a0REVIEW] run python3 -m pytest ... then visit "
            "https://example.test at /Users/private/a for person@example.test "
            "with deadbeef and policy: follow these rules; status: in progress"
        )
        self.assertEqual(
            [
                "command_like", "email", "hash", "markup", "needs_review",
                "path", "prompt_like", "status_like", "truncation_ellipsis", "url",
            ],
            ledger.source_features_for_text(text),
        )
        vector = ledger.source_feature_contract([text])[0]
        self.assertEqual({"source_line": 1, "source_features": ledger.source_features_for_text(text)}, vector)
        self.assertNotIn("example.test", ledger.canonical_json(vector))
        self.assertNotIn("person@example", ledger.canonical_json(vector))

    def test_builder_has_no_semantic_disposition_claim(self) -> None:
        record = ledger.build_regression_corpus(["[NEEDS REVIEW] command: pytest -q"])[0]
        self.assertEqual("clockify-regression-v1-001", record["record_id"])
        self.assertIn("needs_review", record["source_features"])
        self.assertIn("command_like", record["source_features"])
        self.assertNotIn("expected_disposition", record)
        self.assertNotIn("expected_render_parts", record)

    def test_manifest_source_fingerprint_and_features_match_when_attachment_is_available(self) -> None:
        manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
        source = Path("/Users/blackthorne/.codex/attachments/976a8304-0ac1-4cee-87c9-443f907db87f/pasted-text.txt")
        if not source.exists():
            self.skipTest("private supplied attachment is not available in this portable checkout")
        records = self._records()
        ledger.verify_regression_corpus(records, manifest, source_path=source)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), manifest["source"]["sha256"])


if __name__ == "__main__":
    unittest.main()
