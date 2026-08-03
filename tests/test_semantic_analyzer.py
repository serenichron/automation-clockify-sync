from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from scripts import semantic_analyzer as semantic


def event(evidence_id: str, day: str = "2026-07-10", content: str = "work") -> dict:
    return {
        "evidence_id": evidence_id,
        "source_type": "codex",
        "observed_start": f"{day} 10:00",
        "observed_end": f"{day} 10:10",
        "content": content,
    }


def valid_response(evidence_id: str) -> dict:
    return {
        "activities": [
            {
                "lifecycle": "completed",
                "action": "Fixed",
                "object": "Clockify descriptions",
                "outcome": "removed transcript fragments",
                "evidence_ids": [evidence_id],
                "evidence_spans": [
                    {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}
                ],
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
                "effort": {
                    "minimum_minutes": 10,
                    "recommended_minutes": 20,
                    "maximum_minutes": 30,
                },
                "semantic_confidence": "high",
                "timing_confidence": "medium",
                "split_rationale": "one outcome",
                "merge_rationale": "",
            }
        ],
        "exceptions": [],
        "omissions": [],
    }


@mock.patch.dict(
    os.environ,
    {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"},
    clear=False,
)
class SemanticAnalyzerTests(unittest.TestCase):
    def test_validates_and_assigns_stable_ids(self):
        first = semantic.validate_result(
            valid_response("ev-1"),
            known_evidence_ids={"ev-1"},
            provider_model="model-a",
            analyzer_tier="primary",
        )
        second = semantic.validate_result(
            valid_response("ev-1"),
            known_evidence_ids={"ev-1"},
            provider_model="model-a",
            analyzer_tier="primary",
        )
        self.assertEqual(first["activities"][0]["activity_id"], second["activities"][0]["activity_id"])
        self.assertTrue(first["activities"][0]["activity_id"].startswith("act-"))
        self.assertTrue(first["activities"][0]["workstream_id"].startswith("ws-"))
        self.assertEqual("", first["activities"][0]["omit_rationale"])
        self.assertIsNone(first["activities"][0]["rendered_description"])

    def test_rejects_unknown_evidence(self):
        with self.assertRaisesRegex(semantic.AnalyzerError, "unknown evidence"):
            semantic.validate_result(
                valid_response("ev-missing"),
                known_evidence_ids={"ev-1"},
                provider_model="model-a",
                analyzer_tier="primary",
            )

    def test_rejects_missing_or_reassigned_known_evidence(self):
        with self.assertRaisesRegex(semantic.AnalyzerError, "omitted known evidence"):
            semantic.validate_result(
                valid_response("ev-1"),
                known_evidence_ids={"ev-1", "ev-2"},
                provider_model="model-a",
                analyzer_tier="primary",
            )
        duplicated = valid_response("ev-1")
        second = json.loads(json.dumps(duplicated["activities"][0]))
        second["object"] = "Clockify allocation rules"
        duplicated["activities"].append(second)
        with self.assertRaisesRegex(semantic.AnalyzerError, "reassigned evidence"):
            semantic.validate_result(
                duplicated,
                known_evidence_ids={"ev-1"},
                provider_model="model-a",
                analyzer_tier="primary",
            )

    def test_exception_and_omission_require_known_evidence(self):
        response = {
            "activities": [],
            "exceptions": [{
                "kind": "conflicting_evidence",
                "evidence_ids": [],
                "reason": "unclear",
            }],
            "omissions": [],
        }
        with self.assertRaisesRegex(semantic.AnalyzerError, "requires known evidence"):
            semantic.validate_result(
                response,
                known_evidence_ids={"ev-1"},
                provider_model="model-a",
                analyzer_tier="primary",
            )

    def test_rejects_invalid_effort_order(self):
        response = valid_response("ev-1")
        response["activities"][0]["effort"]["recommended_minutes"] = 40
        with self.assertRaisesRegex(semantic.AnalyzerError, "minimum <= recommended <= maximum"):
            semantic.validate_result(
                response,
                known_evidence_ids={"ev-1"},
                provider_model="model-a",
                analyzer_tier="primary",
            )

    def test_effort_is_normalized_to_five_minute_granularity(self):
        response = valid_response("ev-1")
        response["activities"][0]["effort"] = {
            "minimum_minutes": 8,
            "recommended_minutes": 33,
            "maximum_minutes": 37,
        }
        result = semantic.validate_result(
            response,
            known_evidence_ids={"ev-1"},
            provider_model="model-a",
            analyzer_tier="primary",
        )
        self.assertEqual(
            {"minimum_minutes": 20, "recommended_minutes": 35, "maximum_minutes": 50},
            result["activities"][0]["effort"],
        )
        response["activities"][0]["effort"] = {
            "minimum_minutes": 30,
            "recommended_minutes": 33,
            "maximum_minutes": 33,
        }
        second = semantic.validate_result(
            response,
            known_evidence_ids={"ev-1"},
            provider_model="model-a",
            analyzer_tier="primary",
        )
        self.assertEqual(result["activities"][0]["effort"], second["activities"][0]["effort"])

    def test_redundant_verification_action_is_canonicalized_but_other_compounds_fail(self):
        response = valid_response("ev-1")
        response["activities"][0]["action"] = "implemented and verified"
        result = semantic.validate_result(
            response,
            known_evidence_ids={"ev-1"},
            provider_model="model-a",
            analyzer_tier="primary",
        )
        self.assertEqual("Implemented", result["activities"][0]["action"])

        response["activities"][0]["action"] = "investigated and fixed"
        with self.assertRaisesRegex(semantic.AnalyzerError, "one atomic verb phrase"):
            semantic.validate_result(
                response,
                known_evidence_ids={"ev-1"},
                provider_model="model-a",
                analyzer_tier="primary",
            )

    def test_chunking_never_truncates_events_and_prefers_days(self):
        events = [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")]
        chunks = semantic.chunk_events(events, max_body_bytes=50_000)
        self.assertEqual([["ev-a"], ["ev-b"]], [[e["evidence_id"] for e in chunk] for chunk in chunks])
        self.assertEqual("work", chunks[0][0]["content"])

    def test_rejects_single_oversized_evidence_instead_of_clipping(self):
        projected = semantic.project_event(event("ev-a", content="x" * 10_000))
        self.assertEqual(10_000, len(projected["content"]))
        with self.assertRaisesRegex(semantic.AnalyzerError, "exceeds analyzer request ceiling"):
            semantic.chunk_events([event("ev-a", content="x" * 10_000)], max_body_bytes=2_000)

    def test_chunking_is_exact_linear_and_keeps_every_event_once(self):
        events = [
            event(f"ev-{index:03d}", content="x" * 1_024)
            for index in range(30)
        ]
        ordered = semantic.project_events(events)
        ceiling = len(
            semantic.canonical_json(
                semantic._body_for(ordered[:4], model="test", mode="extract")
            ).encode("utf-8")
        )

        with mock.patch.object(semantic, "_body_for", wraps=semantic._body_for) as body_for:
            chunks = semantic.chunk_events(
                reversed(events), model="test", max_body_bytes=ceiling
            )

        self.assertEqual(1, body_for.call_count)
        self.assertEqual([], body_for.call_args.args[0])
        self.assertEqual(
            [item["evidence_id"] for item in ordered],
            [item["evidence_id"] for chunk in chunks for item in chunk],
        )
        self.assertEqual(len(events), len({item["evidence_id"] for chunk in chunks for item in chunk}))
        self.assertTrue(all(
            len(semantic.canonical_json(
                semantic._body_for(chunk, model="test", mode="extract")
            ).encode("utf-8")) <= ceiling
            for chunk in chunks
        ))

    def test_chunking_caps_event_count_without_losing_evidence(self):
        events = [event(f"ev-{index:03d}") for index in range(11)]
        chunks = semantic.chunk_events(
            events,
            max_body_bytes=50_000,
            max_events_per_chunk=3,
        )
        self.assertEqual([3, 3, 3, 2], [len(chunk) for chunk in chunks])
        self.assertEqual(
            sorted(item["evidence_id"] for item in events),
            sorted(item["evidence_id"] for chunk in chunks for item in chunk),
        )

    def test_operational_target_does_not_reject_event_below_hard_ceiling(self):
        item = event("ev-large", content="x" * 8_000)
        chunks = semantic.chunk_events(
            [item],
            max_body_bytes=20_000,
            target_body_bytes=2_000,
        )
        self.assertEqual([["ev-large"]], [
            [value["evidence_id"] for value in chunk]
            for chunk in chunks
        ])

    def test_operational_target_must_be_positive(self):
        with self.assertRaisesRegex(
            semantic.AnalyzerError,
            "target_body_bytes must be positive",
        ):
            semantic.chunk_events(
                [event("ev-1")],
                max_body_bytes=20_000,
                target_body_bytes=0,
            )

    def test_projection_preserves_structured_fathom_semantics_without_identity_fields(self):
        meeting = {
            "evidence_id": "ev-meeting",
            "source_type": "fathom",
            "observed_at": "2026-07-10 10:00",
            "raw_source_span": {
                "start": "2026-07-10 10:00",
                "end": "2026-07-10 11:00",
            },
            "attributes": {
                "title": "Discovery call",
                "summary": {
                    "template_name": "general",
                    "markdown_formatted": "Defined onboarding scope and delivery constraints.",
                },
                "action_items": [
                    {
                        "description": "Prepare scoped implementation plan",
                        "assignee": {"email": "private@example.test"},
                        "recording_playback_url": "https://private.example.test/recording",
                    }
                ],
                "transcript": [
                    {
                        "speaker": {"display_name": "Private Person"},
                        "text": "We need staged rollout gates before launch.",
                    }
                ],
            },
        }
        projected = semantic.project_event(meeting)
        self.assertEqual("meeting", projected["source_category"])
        self.assertEqual(
            "Defined onboarding scope and delivery constraints.",
            projected["meeting_context"]["summary"],
        )
        self.assertEqual(
            ["Prepare scoped implementation plan"],
            projected["meeting_context"]["action_items"],
        )
        self.assertEqual(
            ["We need staged rollout gates before launch."],
            projected["meeting_context"]["transcript"],
        )
        payload = json.dumps(projected)
        self.assertNotIn("private@example.test", payload)
        self.assertNotIn("Private Person", payload)
        self.assertNotIn("recording_playback_url", payload)

    def test_private_semantic_text_requires_explicit_egress_approval(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            private_fields = [
                {"content": "Confidential acquisition pricing"},
                {"attributes": {"first_user_message": "Private session request"}},
                {"attributes": {"last_assistant_message": "Private session result"}},
                {"attributes": {"label": "Private work label"}},
                {"attributes": {"subject": "Private commit subject"}},
                {"meeting_context": {"title": "Private meeting title"}},
                {"source_type": "enriched_context", "attributes": {"description": "Private legacy context"}},
            ]
            for index, fields in enumerate(private_fields):
                private_event = event(f"ev-private-{index}", content="")
                private_event.update(fields)
                with self.subTest(fields=fields), self.assertRaisesRegex(
                    semantic.AnalyzerError, "private semantic text egress"
                ):
                    semantic._body_for(
                        [private_event],
                        model="cloud",
                        mode="extract",
                    )
            probe = semantic.probe_endpoint(
                semantic.AnalyzerEndpoint("probe", "http://example.test", "cloud"),
                transport=lambda endpoint, body: {"ok": True},
            )
            self.assertEqual("ok", probe["status"])

    def test_rejects_duplicate_evidence_ids_inside_each_classification(self):
        variants = []
        activity = valid_response("ev-1")
        activity["activities"][0]["evidence_ids"] = ["ev-1", "ev-1"]
        variants.append(("activity", activity))
        variants.append(("exception", {
            "activities": [],
            "exceptions": [{"kind": "uncertain", "reason": "conflict", "evidence_ids": ["ev-1", "ev-1"]}],
            "omissions": [],
        }))
        variants.append(("omission", {
            "activities": [],
            "exceptions": [],
            "omissions": [{"kind": "noise", "reason": "transport", "evidence_ids": ["ev-1", "ev-1"]}],
        }))
        for kind, response in variants:
            with self.subTest(kind=kind), self.assertRaisesRegex(
                semantic.AnalyzerError, f"{kind} evidence_ids must not repeat"
            ):
                semantic.validate_result(
                    response,
                    known_evidence_ids={"ev-1"},
                    provider_model="model",
                    analyzer_tier="primary",
                )

    def test_projection_preserves_commit_subject_and_only_artifact_basenames(self):
        commit = event("ev-commit", content="")
        commit.update({
            "source_type": "repository_events",
            "attributes": {
                "subject": "Fix stable review identity",
                "artifacts": ["scripts/review_state.py", "/private/project/tests/test_review.py"],
            },
        })
        projected = semantic.project_event(commit)
        self.assertIn("Fix stable review identity", projected["content"])
        self.assertIn("review_state.py", projected["content"])
        self.assertIn("test_review.py", projected["content"])
        self.assertNotIn("scripts/", projected["content"])
        self.assertNotIn("/private/project", projected["content"])

    def test_tiered_analysis_falls_back_on_contract_failure(self):
        calls = []

        def transport(endpoint, body):
            calls.append((endpoint.name, "probe" if "events" not in body["messages"][1]["content"] else "evidence"))
            if "events" not in body["messages"][1]["content"]:
                return {"probe": "ok"}
            if endpoint.name == "primary":
                return {"choices": [{"message": {"content": "not-json"}}]}
            payload = json.loads(body["messages"][1]["content"])
            return valid_response(payload["events"][0]["evidence_id"])

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )
        self.assertEqual(
            [("primary", "probe"), ("primary", "evidence"), ("fallback", "probe"), ("fallback", "evidence")],
            calls,
        )
        self.assertEqual("fallback", result["activities"][0]["analyzer_tier"])
        self.assertEqual("strong", result["activities"][0]["analyzer_model"])

    def test_tiered_analysis_falls_back_on_primary_call_failure(self):
        calls = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            calls.append((endpoint.name, "probe" if payload.get("probe") else "extract"))
            if payload.get("probe"):
                return {"probe": "ok"}
            if endpoint.name == "primary":
                raise semantic.AnalyzerError("primary call unavailable")
            return valid_response(payload["events"][0]["evidence_id"])

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )

        self.assertEqual(
            [
                ("primary", "probe"),
                ("primary", "extract"),
                ("fallback", "probe"),
                ("fallback", "extract"),
            ],
            calls,
        )
        self.assertEqual("fallback", result["activities"][0]["analyzer_tier"])
        self.assertEqual("used_after_primary_failure", result["analysis_chunks"][0]["fallback_status"])

    def test_low_confidence_primary_uses_stronger_fallback(self):
        calls = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                calls.append((endpoint.name, "probe"))
                return {"probe": "ok"}
            calls.append((endpoint.name, "extract"))
            response = valid_response(payload["events"][0]["evidence_id"])
            if endpoint.name == "primary":
                response["activities"][0]["semantic_confidence"] = "low"
            return response

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )
        self.assertEqual(
            [("primary", "probe"), ("primary", "extract"), ("fallback", "probe"), ("fallback", "extract")],
            calls,
        )
        self.assertEqual("fallback", result["activities"][0]["analyzer_tier"])
        self.assertEqual("high", result["activities"][0]["semantic_confidence"])

    def test_unresolved_low_confidence_becomes_exception_not_proposal(self):
        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            response = valid_response(payload["events"][0]["evidence_id"])
            response["activities"][0]["timing_confidence"] = "low"
            return response

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )
        self.assertEqual([], result["activities"])
        self.assertEqual("low_confidence", result["exceptions"][0]["kind"])
        self.assertEqual(["ev-1"], result["exceptions"][0]["evidence_ids"])

    def test_failed_fallback_defers_valid_low_confidence_primary_once(self):
        calls = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            calls.append((endpoint.name, payload.get("mode", "probe")))
            if payload.get("probe"):
                return {"probe": "ok"}
            response = valid_response(payload["events"][0]["evidence_id"])
            if endpoint.name == "primary":
                response["activities"][0]["semantic_confidence"] = "low"
                return response
            return {"activities": [], "exceptions": [], "omissions": []}

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )

        self.assertEqual(
            [
                ("primary", "probe"),
                ("primary", "extract"),
                ("fallback", "probe"),
                ("fallback", "extract"),
            ],
            calls,
        )
        self.assertEqual([], result["activities"])
        self.assertEqual("low_confidence", result["exceptions"][0]["kind"])
        self.assertEqual("primary", result["analysis_chunks"][0]["tier"])
        self.assertEqual("failed_deferred", result["analysis_chunks"][0]["fallback_status"])

    def test_primary_and_fallback_contract_failures_become_bounded_exception(self):
        calls = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            calls.append((endpoint.name, payload.get("mode", "probe")))
            if payload.get("probe"):
                return {"probe": "ok"}
            return {"activities": [], "exceptions": [], "omissions": []}

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )

        self.assertEqual(
            [
                ("primary", "probe"),
                ("primary", "extract"),
                ("fallback", "probe"),
                ("fallback", "extract"),
            ],
            calls,
        )
        self.assertEqual([], result["activities"])
        self.assertEqual("analyzer_failure", result["exceptions"][0]["kind"])
        self.assertEqual(["ev-1"], result["exceptions"][0]["evidence_ids"])
        self.assertEqual("exception", result["analysis_chunks"][0]["tier"])
        self.assertEqual("failed_exception", result["analysis_chunks"][0]["fallback_status"])
        self.assertRegex(result["analysis_chunks"][0]["failure_digest"], r"^aer-[0-9a-f]{24}$")
        self.assertEqual("fallback", result["analysis_chunks"][0]["fallback_endpoint"])
        self.assertEqual("strong", result["analysis_chunks"][0]["fallback_model"])

    def test_fallback_outage_blocks_instead_of_becoming_contract_exception(self):
        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if endpoint.name == "fallback" and payload.get("probe"):
                raise semantic.AnalyzerError("fallback route unavailable")
            if payload.get("probe"):
                return {"probe": "ok"}
            return {"activities": [], "exceptions": [], "omissions": []}

        with self.assertRaisesRegex(
            semantic.AnalyzerError,
            "failed without dual contract rejection",
        ):
            semantic.analyze_tiered(
                [event("ev-1")],
                primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
                fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
                transport=transport,
            )

    def test_tiered_analysis_is_deterministic_under_event_permutation(self):
        def transport(endpoint, body):
            if "events" not in body["messages"][1]["content"]:
                return {"probe": "ok"}
            payload = json.loads(body["messages"][1]["content"])
            ordered_events = sorted(payload["events"], key=lambda item: item["evidence_id"])
            response = valid_response(ordered_events[0]["evidence_id"])
            response["activities"][0].update({
                "evidence_ids": [item["evidence_id"] for item in ordered_events],
                "evidence_spans": [item["time_span"] for item in ordered_events],
            })
            return response

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        first = semantic.analyze_tiered(
            [event("ev-a"), event("ev-b")], primary=endpoint, transport=transport
        )
        second = semantic.analyze_tiered(
            [event("ev-b"), event("ev-a")], primary=endpoint, transport=transport
        )
        self.assertEqual(first, second)

    def test_tiered_analysis_probes_once_per_endpoint_before_evidence(self):
        calls = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            calls.append((endpoint.name, "probe" if "events" not in payload else "evidence"))
            if "events" not in payload:
                self.assertNotIn("ev-", json.dumps(body))
                return {"probe": "ok"}
            response = valid_response(payload["events"][0]["evidence_id"])
            if payload["events"][0]["time_span"]["start"].startswith("2026-07-11"):
                response["activities"][0]["object"] = "Clockify review state"
            response["activities"][0]["evidence_spans"] = [payload["events"][0]["time_span"]]
            return response

        semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
        )
        self.assertEqual(
            [("primary", "probe"), ("primary", "evidence"), ("primary", "evidence")],
            calls,
        )

    def test_concurrent_extraction_preserves_chunk_order_and_all_evidence(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def transport(_endpoint, body):
            nonlocal active, max_active
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            with lock:
                active += 1
                max_active = max(max_active, active)
            # Reverse completion order so the assertion cannot pass merely
            # because requests happened to finish in source order.
            evidence_id = payload["events"][0]["evidence_id"]
            day = int(payload["events"][0]["time_span"]["start"][8:10])
            time.sleep(0.01 * (20 - day))
            with lock:
                active -= 1
            response = valid_response(evidence_id)
            marker = payload["events"][0]["time_span"]["start"]
            response["activities"][0].update({
                "object": f"Clockify item {marker}",
                "workstream": f"Clockify stream {marker}",
                "evidence_spans": [payload["events"][0]["time_span"]],
            })
            return response

        events = [event(f"ev-{number}", f"2026-07-{10 + number:02d}") for number in range(4)]
        result = semantic.analyze_tiered(
            events,
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
            max_events_per_chunk=1,
            max_workers=4,
        )

        self.assertGreaterEqual(max_active, 2)
        self.assertEqual([1, 2, 3, 4], [item["chunk"] for item in result["analysis_chunks"]])
        classified = {
            evidence_id
            for collection in ("activities", "exceptions", "omissions")
            for item in result[collection]
            for evidence_id in item["evidence_ids"]
        }
        self.assertEqual({item["evidence_id"] for item in events}, classified)

    def test_concurrent_extraction_probes_each_route_once(self):
        calls: list[tuple[str, str]] = []
        lock = threading.Lock()

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            with lock:
                calls.append((endpoint.name, "probe" if payload.get("probe") else "extract"))
            if payload.get("probe"):
                return {"probe": "ok"}
            response = valid_response(payload["events"][0]["evidence_id"])
            marker = payload["events"][0]["time_span"]["start"]
            response["activities"][0].update({
                "object": f"Clockify item {marker}",
                "workstream": f"Clockify stream {marker}",
                "evidence_spans": [payload["events"][0]["time_span"]],
            })
            return response

        semantic.analyze_tiered(
            [event(f"ev-{number}", f"2026-07-{10 + number:02d}") for number in range(5)],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
            max_events_per_chunk=1,
            max_workers=4,
        )
        self.assertEqual(1, calls.count(("primary", "probe")))
        self.assertEqual(5, calls.count(("primary", "extract")))

    def test_concurrent_primary_probe_failure_uses_fallback_after_one_attempt(self):
        calls: list[str] = []
        lock = threading.Lock()

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                with lock:
                    calls.append(endpoint.name)
                if endpoint.name == "primary":
                    raise semantic.AnalyzerError("route unavailable")
                return {"probe": "ok"}
            response = valid_response(payload["events"][0]["evidence_id"])
            marker = payload["events"][0]["time_span"]["start"]
            response["activities"][0].update({
                "object": f"Clockify item {marker}",
                "workstream": f"Clockify stream {marker}",
                "evidence_spans": [payload["events"][0]["time_span"]],
            })
            return response

        result = semantic.analyze_tiered(
            [event(f"ev-{number}", f"2026-07-{10 + number:02d}") for number in range(4)],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
            max_events_per_chunk=1,
            max_workers=4,
        )
        self.assertEqual(["primary", "fallback"], calls)
        self.assertTrue(all(
            chunk["tier"] == "fallback"
            for chunk in result["analysis_chunks"]
        ))

    def test_fatal_concurrent_chunk_cancels_queued_chunks_before_transport_or_cache_write(self):
        started_second = threading.Event()
        extract_calls: list[tuple[str, int]] = []
        lock = threading.Lock()

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            event_payload = payload["events"][0]
            day = int(event_payload["time_span"]["start"][8:10])
            with lock:
                extract_calls.append((endpoint.name, day))
            if endpoint.name == "primary" and day == 10:
                self.assertTrue(started_second.wait(timeout=1))
                # A malformed primary answer is a contract rejection.  Its
                # fallback's route outage is the fatal error that must cancel
                # the chunks which are still queued behind these two workers.
                return {"choices": [{"message": {"content": "not-json"}}]}
            if endpoint.name == "fallback" and day == 10:
                raise semantic.AnalyzerError("fallback route unavailable")
            if endpoint.name == "primary" and day == 11:
                started_second.set()
                # Already in flight: it may complete, but cancellation must
                # discard it before cache persistence, fallback, or synthesis.
                time.sleep(0.1)
                response = valid_response(event_payload["evidence_id"])
                response["activities"][0]["evidence_spans"] = [event_payload["time_span"]]
                return response
            self.fail("a queued chunk must not reach the transport")

        primary = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        fallback = semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong")
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "analyzer-cache.jsonl"
            with self.assertRaisesRegex(semantic.AnalyzerError, "failed without dual contract rejection"):
                semantic.analyze_tiered(
                    [event(f"ev-{number}", f"2026-07-{10 + number:02d}") for number in range(5)],
                    primary=primary,
                    fallback=fallback,
                    transport=transport,
                    cache=semantic.AnalyzerResponseCache(cache_path),
                    max_events_per_chunk=1,
                    max_workers=2,
                )
            # Only the fatal primary contract rejection may be persisted.  The
            # in-flight peer and all queued chunks cannot add cache records.
            self.assertEqual(1, len(cache_path.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(
            [("fallback", 10), ("primary", 10), ("primary", 11)],
            sorted(extract_calls),
        )

    def test_cross_chunk_same_accomplishment_synthesizes_unioned_evidence(self):
        calls = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            calls.append(payload.get("mode", "probe"))
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                evidence = payload["events"][0]
                response = valid_response(evidence["evidence_id"])
                response["activities"][0]["evidence_spans"] = [evidence["time_span"]]
                return response
            provisional = payload["provisional_activities"]
            response = valid_response(provisional[0]["evidence_ids"][0])
            response["activities"][0].update({
                "evidence_ids": sorted(
                    evidence_id
                    for activity in provisional
                    for evidence_id in activity["evidence_ids"]
                ),
                "evidence_spans": [
                    span for activity in provisional for span in activity["evidence_spans"]
                ],
                "merge_rationale": "same cleanup outcome across two days",
            })
            return response

        result = semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
        )
        self.assertEqual(["probe", "extract", "extract", "synthesize"], calls)
        self.assertEqual(1, len(result["activities"]))
        self.assertEqual(["ev-a", "ev-b"], result["activities"][0]["evidence_ids"])
        self.assertEqual("primary", result["activities"][0]["analyzer_tier"])
        self.assertEqual("cheap", result["activities"][0]["analyzer_model"])

    def test_cross_chunk_distinct_outcomes_remain_split_with_specific_objects(self):
        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                evidence = payload["events"][0]
                response = valid_response(evidence["evidence_id"])
                response["activities"][0]["evidence_spans"] = [evidence["time_span"]]
                return response
            activities = []
            for index, provisional in enumerate(payload["provisional_activities"], start=1):
                response = valid_response(provisional["evidence_ids"][0])
                response["activities"][0].update({
                    "object": f"Clockify description work item {index}",
                    "outcome": f"preserved distinct evidenced outcome {index}",
                    "evidence_spans": provisional["evidence_spans"],
                    "split_rationale": "different atomic outcomes",
                })
                activities.extend(response["activities"])
            return {"activities": activities, "exceptions": [], "omissions": []}

        result = semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
        )
        self.assertEqual(2, len(result["activities"]))
        self.assertEqual(
            {"ev-a", "ev-b"},
            {evidence_id for activity in result["activities"] for evidence_id in activity["evidence_ids"]},
        )
        self.assertEqual(2, len({activity["workstream_id"] for activity in result["activities"]}))

    def test_synthesis_falls_back_and_probes_each_endpoint_once(self):
        calls = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            calls.append((endpoint.name, payload.get("mode", "probe")))
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                evidence = payload["events"][0]
                response = valid_response(evidence["evidence_id"])
                response["activities"][0]["evidence_spans"] = [evidence["time_span"]]
                return response
            provisional = payload["provisional_activities"]
            response = valid_response(provisional[0]["evidence_ids"][0])
            if endpoint.name == "primary":
                response["activities"][0]["evidence_spans"] = provisional[0]["evidence_spans"]
                return response
            response["activities"][0].update({
                "evidence_ids": sorted(
                    evidence_id
                    for activity in provisional
                    for evidence_id in activity["evidence_ids"]
                ),
                "evidence_spans": [
                    span for activity in provisional for span in activity["evidence_spans"]
                ],
            })
            return response

        result = semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )
        self.assertEqual(
            [
                ("primary", "probe"), ("primary", "extract"),
                ("primary", "extract"), ("primary", "synthesize"),
                ("fallback", "probe"), ("fallback", "synthesize"),
            ],
            calls,
        )
        self.assertEqual("fallback", result["activities"][0]["analyzer_tier"])
        self.assertEqual("strong", result["activities"][0]["analyzer_model"])

    def test_double_synthesis_failure_becomes_bounded_exception(self):
        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                evidence = payload["events"][0]
                response = valid_response(evidence["evidence_id"])
                response["activities"][0]["evidence_spans"] = [evidence["time_span"]]
                return response
            return {"activities": [], "exceptions": [], "omissions": []}

        result = semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )

        self.assertEqual([], result["activities"])
        self.assertEqual(1, len(result["exceptions"]))
        exception = result["exceptions"][0]
        self.assertEqual("analyzer_synthesis_failure", exception["kind"])
        self.assertEqual(["ev-a", "ev-b"], exception["evidence_ids"])
        self.assertRegex(exception["failure_digest"], r"^aer-[0-9a-f]{24}$")

    def test_double_contract_failure_digest_is_stable_on_cache_replay(self):
        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            return {"activities": [], "exceptions": [], "omissions": []}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "analyzer-cache.jsonl"
            endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
            fallback = semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong")
            first = semantic.analyze_tiered(
                [event("ev-1")],
                primary=endpoint,
                fallback=fallback,
                transport=transport,
                cache=semantic.AnalyzerResponseCache(path),
            )
            second = semantic.analyze_tiered(
                [event("ev-1")],
                primary=endpoint,
                fallback=fallback,
                transport=lambda *_: self.fail("sealed rejection must not retry transport"),
                cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertEqual(
            first["analysis_chunks"][0]["failure_digest"],
            second["analysis_chunks"][0]["failure_digest"],
        )

    def test_synthesis_request_contains_only_safe_provisional_fields(self):
        captured = {}
        secret = "TOP_SECRET_SYNTHESIS_VALUE"

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                evidence = payload["events"][0]
                response = valid_response(evidence["evidence_id"])
                response["activities"][0]["evidence_spans"] = [evidence["time_span"]]
                return response
            captured.update(payload)
            provisional = payload["provisional_activities"]
            response = valid_response(provisional[0]["evidence_ids"][0])
            response["activities"][0].update({
                "evidence_ids": sorted(
                    evidence_id
                    for activity in provisional
                    for evidence_id in activity["evidence_ids"]
                ),
                "evidence_spans": [
                    span for activity in provisional for span in activity["evidence_spans"]
                ],
            })
            return response

        semantic.analyze_tiered(
            [
                event("ev-a", "2026-07-10", f"completed work api_key={secret} /private/raw.json"),
                event("ev-b", "2026-07-11", f"completed work https://private.example.test/{secret}"),
            ],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
        )
        request = json.dumps(captured, sort_keys=True)
        self.assertEqual("synthesize", captured["mode"])
        self.assertNotIn("events", captured)
        self.assertNotIn("raw_source_span", request)
        self.assertNotIn("content", request)
        self.assertNotIn(secret, request)
        self.assertNotIn("https://private.example.test", request)
        self.assertNotIn("/private/raw.json", request)

    def test_synthesis_invalid_or_lost_citations_block(self):
        def run_synthesis(response_evidence_ids):
            def transport(endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                if payload.get("probe"):
                    return {"probe": "ok"}
                if payload["mode"] == "extract":
                    evidence = payload["events"][0]
                    response = valid_response(evidence["evidence_id"])
                    response["activities"][0]["evidence_spans"] = [evidence["time_span"]]
                    return response
                provisional = payload["provisional_activities"]
                response = valid_response(response_evidence_ids[0])
                response["activities"][0].update({
                    "evidence_ids": response_evidence_ids,
                    "evidence_spans": [
                        span for activity in provisional
                        if any(evidence_id in response_evidence_ids for evidence_id in activity["evidence_ids"])
                        for span in activity["evidence_spans"]
                    ],
                })
                return response

            return semantic.analyze_tiered(
                [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
                primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
                transport=transport,
            )

        with self.assertRaisesRegex(semantic.AnalyzerError, "unknown evidence reference"):
            run_synthesis(["ref-0001", "ref-unknown"])
        with self.assertRaisesRegex(semantic.AnalyzerError, "omitted known evidence IDs"):
            run_synthesis(["ref-0001"])

    def test_synthesis_is_deterministic_under_input_permutation(self):
        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                evidence = payload["events"][0]
                response = valid_response(evidence["evidence_id"])
                response["activities"][0]["evidence_spans"] = [evidence["time_span"]]
                return response
            provisional = payload["provisional_activities"]
            response = valid_response(provisional[0]["evidence_ids"][0])
            response["activities"][0].update({
                "evidence_ids": sorted(
                    evidence_id
                    for activity in provisional
                    for evidence_id in activity["evidence_ids"]
                ),
                "evidence_spans": [
                    span for activity in provisional for span in activity["evidence_spans"]
                ],
            })
            return response

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        first = semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=endpoint,
            transport=transport,
        )
        second = semantic.analyze_tiered(
            [event("ev-b", "2026-07-11"), event("ev-a", "2026-07-10")],
            primary=endpoint,
            transport=transport,
        )
        self.assertEqual(first, second)

    def test_projection_never_egresses_raw_tool_or_sensitive_content(self):
        sensitive = event(
            "ev-safe",
            content=(
                "Completed safe reconciliation and preserved cited evidence. "
                "Email jane@example.test, read https://private.example.test/run, and see "
                "/Users/alex/private/token.txt. api_key=SUPER_SECRET_TOKEN "
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
        )
        sensitive.update({
            "role": "assistant",
            "source_type": "codex_sessions_event",
            "attributes": {
                "role": "assistant",
                "kind": "message",
                "project": "Clockify Reconciliation",
                "content": "Validated safe semantic outcome.",
                "title": "Reconciliation review",
                "summary": "Reviewed cited coverage.",
                "action_items": ["Confirm the next evidence batch."],
            },
            "raw_source_span": {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10", "path": "/private/raw.jsonl"},
        })
        tool = event("ev-tool", content="curl https://private.example.test --header Authorization: Bearer TOP_SECRET")
        tool.update({
            "role": "tool",
            "kind": "tool_result",
            "arguments": {"password": "TOP_SECRET"},
            "attributes": {"kind": "tool_result", "content": "TOP_SECRET"},
        })
        body = semantic._body_for([sensitive, tool], model="test", mode="extract")
        request_json = json.dumps(body, sort_keys=True)
        for forbidden in (
            "jane@example.test", "https://private.example.test", "/Users/alex/private/token.txt",
            "SUPER_SECRET_TOKEN", "TOP_SECRET", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "curl ", "Authorization:",
        ):
            self.assertNotIn(forbidden, request_json)
        payload = json.loads(body["messages"][1]["content"])
        safe = next(item for item in payload["events"] if item["role"] == "assistant")
        tool_projection = next(item for item in payload["events"] if item["role"] == "tool")
        self.assertEqual({"ref-0001", "ref-0002"}, {item["evidence_id"] for item in payload["events"]})
        self.assertIn("Completed safe reconciliation", safe["content"])
        self.assertIn("Validated safe semantic outcome", safe["content"])
        self.assertEqual("agent_session", safe["source_category"])
        self.assertEqual("assistant", safe["role"])
        self.assertEqual({"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}, safe["time_span"])
        self.assertEqual("Clockify Reconciliation", safe["project_context"]["name"])
        self.assertEqual("Reconciliation review", safe["meeting_context"]["title"])
        self.assertEqual(["Confirm the next evidence batch."], safe["meeting_context"]["action_items"])
        self.assertEqual("", tool_projection["content"])
        self.assertNotIn("raw_source_span", json.dumps(payload))
        self.assertNotIn("arguments", json.dumps(payload))

    def test_projection_redacts_phone_address_and_contextual_person_name(self):
        private = event(
            "ev-private",
            content=(
                "Call Ana Popescu at +40 721 123 456 about rollout, then visit "
                "10 Strada Exemplu. Keep Clockify Reconciliation and version 1.2.3.4."
            ),
        )
        projected = semantic.project_event(private)
        payload = json.dumps(projected, ensure_ascii=False)
        self.assertNotIn("Ana Popescu", payload)
        self.assertNotIn("+40 721 123 456", payload)
        self.assertNotIn("10 Strada Exemplu", payload)
        self.assertIn("[person removed]", projected["content"])
        self.assertIn("[phone removed]", projected["content"])
        self.assertIn("[address removed]", projected["content"])
        self.assertIn("Clockify Reconciliation", projected["content"])
        self.assertIn("1.2.3.4", projected["content"])

    def test_privacy_redaction_does_not_consume_timestamps_or_product_wording(self):
        text = (
            "Call Clockify Support at 2026-07-10 10:00 during review. "
            "Processed 123456 records and release 2026-07-10."
        )
        safe = semantic._safe_text(text)
        self.assertIn("Clockify Support", safe)
        self.assertIn("2026-07-10 10:00", safe)
        self.assertIn("123456 records", safe)
        self.assertIn("release 2026-07-10", safe)

    def test_redaction_converges_when_one_match_exposes_an_adjacent_match(self):
        self.assertEqual(
            "a",
            semantic._sub_until_stable(re.compile("ab"), "a", "abb"),
        )

    def test_stable_identity_ignores_harmless_action_and_outcome_paraphrase(self):
        first = valid_response("ev-1")
        second = valid_response("ev-1")
        second["activities"][0].update({
            "action": "Corrected",
            "outcome": "cleared copied chat fragments",
        })
        left = semantic.validate_result(first, known_evidence_ids={"ev-1"}, provider_model="model", analyzer_tier="primary")
        right = semantic.validate_result(second, known_evidence_ids={"ev-1"}, provider_model="model", analyzer_tier="primary")
        self.assertEqual(left["activities"][0]["activity_id"], right["activities"][0]["activity_id"])

    def test_workstream_clusters_same_normalized_project_and_object_across_evidence(self):
        first = valid_response("ev-1")
        second = valid_response("ev-2")
        second["activities"][0]["object"] = "clockify descriptions!"
        second["activities"][0]["project_recommendation"]["name"] = "  serenichron level 2 "
        left = semantic.validate_result(first, known_evidence_ids={"ev-1"}, provider_model="model", analyzer_tier="primary")
        right = semantic.validate_result(second, known_evidence_ids={"ev-2"}, provider_model="model", analyzer_tier="primary")
        self.assertEqual(left["activities"][0]["workstream_id"], right["activities"][0]["workstream_id"])

    def test_rejects_reusing_one_evidence_item_for_two_activities(self):
        response = valid_response("ev-1")
        duplicate = dict(response["activities"][0])
        duplicate["effort"] = dict(duplicate["effort"])
        duplicate.update({"action": "Documented", "outcome": "captured rollout steps"})
        response["activities"].append(duplicate)
        with self.assertRaisesRegex(semantic.AnalyzerError, "reassigned evidence IDs more than once"):
            semantic.validate_result(
                response,
                known_evidence_ids={"ev-1"},
                provider_model="model",
                analyzer_tier="primary",
            )

    def test_rejects_obvious_compound_activity_and_missing_atomicity_rationale(self):
        compound = valid_response("ev-1")
        compound["activities"][0]["action"] = "Fixed and documented"
        with self.assertRaisesRegex(semantic.AnalyzerError, "one atomic verb phrase"):
            semantic.validate_result(
                compound,
                known_evidence_ids={"ev-1"},
                provider_model="model",
                analyzer_tier="primary",
            )

        missing = valid_response("ev-1")
        missing["activities"][0]["split_rationale"] = ""
        with self.assertRaisesRegex(semantic.AnalyzerError, "atomicity rationale"):
            semantic.validate_result(
                missing,
                known_evidence_ids={"ev-1"},
                provider_model="model",
                analyzer_tier="primary",
            )

    def test_rejects_missing_invalid_or_unsupported_reviewable_spans(self):
        response = valid_response("ev-1")
        response["activities"][0]["evidence_spans"] = []
        with self.assertRaisesRegex(semantic.AnalyzerError, "nonempty evidence_spans"):
            semantic.validate_result(response, known_evidence_ids={"ev-1"}, provider_model="model", analyzer_tier="primary")
        response = valid_response("ev-1")
        response["activities"][0]["evidence_spans"] = [{"start": "no", "end": "no"}]
        with self.assertRaisesRegex(semantic.AnalyzerError, "valid start and end"):
            semantic.validate_result(response, known_evidence_ids={"ev-1"}, provider_model="model", analyzer_tier="primary")
        with self.assertRaisesRegex(semantic.AnalyzerError, "not supported"):
            semantic.validate_result(
                valid_response("ev-1"),
                known_evidence_ids={"ev-1"},
                provider_model="model",
                analyzer_tier="primary",
                evidence_time_spans={"ev-1": {"start": "2026-07-11 10:00", "end": "2026-07-11 10:10"}},
            )

    def test_timestamp_validation_compares_instants_across_offsets(self):
        response = valid_response("ev-1")
        response["activities"][0]["evidence_spans"] = [
            {"start": "2026-07-11T07:05:00Z", "end": "2026-07-11T07:08:00Z"}
        ]

        result = semantic.validate_result(
            response,
            known_evidence_ids={"ev-1"},
            provider_model="model",
            analyzer_tier="primary",
            evidence_time_spans={
                "ev-1": {
                    "start": "2026-07-11T10:00:00+03:00",
                    "end": "2026-07-11T10:10:00+03:00",
                }
            },
        )

        self.assertEqual("2026-07-11T07:05:00Z", result["activities"][0]["evidence_spans"][0]["start"])
        self.assertTrue(
            semantic._ordered_timestamps(
                "2026-07-11T10:00:00+03:00",
                "2026-07-11T08:00:00Z",
            )
        )

    def test_probe_is_minimal_and_does_not_include_evidence(self):
        captured = {}

        def transport(endpoint, body):
            captured.update(body)
            return {"probe": "ok"}

        result = semantic.probe_endpoint(
            semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
        )
        self.assertEqual("ok", result["status"])
        self.assertNotIn("events", json.dumps(captured))

    def test_local_only_exact_correction_is_rejected_before_request_projection(self):
        with self.assertRaisesRegex(semantic.AnalyzerError, "local-only"):
            semantic._project_corrections([
                {
                    "local_only": True,
                    "decision": "modify",
                    "evidence_fingerprint": "evfp:sha256:" + "a" * 64,
                    "expected_field_patch": {
                        "description": {"op": "replace", "value": "private exact correction"}
                    },
                }
            ])

    def test_validated_response_cache_replays_without_recalling_provider(self):
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                calls.append("probe")
                return {"probe": "ok"}
            calls.append("evidence")
            evidence_id = payload["events"][0]["evidence_id"]
            return valid_response(evidence_id)

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first_cache = semantic.AnalyzerResponseCache(path)
            first = semantic.analyze_tiered(
                [event("ev-1")],
                primary=endpoint,
                transport=transport,
                private_text_approved=True,
                cache=first_cache,
            )
            second_cache = semantic.AnalyzerResponseCache(path)
            second = semantic.analyze_tiered(
                [event("ev-1")],
                primary=endpoint,
                transport=transport,
                private_text_approved=True,
                cache=second_cache,
            )

        self.assertEqual(first["activities"], second["activities"])
        self.assertEqual(["probe", "evidence"], calls)
        self.assertEqual(1, first_cache.misses)
        self.assertEqual(1, second_cache.hits)
        self.assertEqual(
            first["analyzer_cache"]["records"], second["analyzer_cache"]["records"]
        )

    def test_response_cache_is_safe_for_concurrent_distinct_writers(self):
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)
            bodies = [
                semantic._body_for(
                    [event(f"ev-{number}", content=f"work item {number}")],
                    model="cheap",
                    mode="extract",
                    private_text_approved=True,
                )
                for number in range(8)
            ]

            def store(number_and_body):
                number, body = number_and_body
                cache.store_accepted(endpoint, body, valid_response(f"ev-{number}"))

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(store, enumerate(bodies)))

            self.assertEqual(8, len(path.read_text(encoding="utf-8").splitlines()))
            self.assertEqual(8, len(cache.summary()["records"]))
            reloaded = semantic.AnalyzerResponseCache(path)
            self.assertEqual(8, len(reloaded._records))
            self.assertTrue(all(reloaded.lookup(endpoint, body) is not None for body in bodies))

    def test_cache_read_failure_fails_closed_without_fallback_transport(self):
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        fallback = semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong")
        calls: list[str] = []

        def transport(route, _body):
            calls.append(route.name)
            return {"probe": "ok"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)
            path.write_text("not-json\n", encoding="utf-8")
            with self.assertRaisesRegex(semantic.AnalyzerError, "invalid JSON"):
                semantic.analyze_tiered(
                    [event("ev-1")],
                    primary=endpoint,
                    fallback=fallback,
                    transport=transport,
                    private_text_approved=True,
                    cache=cache,
                )
        self.assertEqual([], calls)

    def test_response_cache_tampering_fails_closed(self):
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        body = semantic._body_for(
            [event("ev-1")], model="cheap", mode="extract", private_text_approved=True
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)
            cache.store_accepted(endpoint, body, valid_response("ev-1"))
            record = json.loads(path.read_text(encoding="utf-8"))
            record["response"]["activities"][0]["outcome"] = "tampered"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(semantic.AnalyzerError, "decision digest differs"):
                semantic.AnalyzerResponseCache(path)

    def test_response_cache_identity_tampering_fails_closed(self):
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        body = semantic._body_for(
            [event("ev-1")], model="cheap", mode="extract", private_text_approved=True
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)
            cache.store_accepted(endpoint, body, valid_response("ev-1"))
            record = json.loads(path.read_text(encoding="utf-8"))
            record["body_digest"] = "0" * 64
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(semantic.AnalyzerError, "identity digest differs"):
                semantic.AnalyzerResponseCache(path)

    def test_response_cache_retains_prior_prompt_records_without_reusing_them(self):
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        body = semantic._body_for(
            [event("ev-1")], model="cheap", mode="extract", private_text_approved=True
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)
            cache.store_accepted(endpoint, body, valid_response("ref-0001"))
            record = json.loads(path.read_text(encoding="utf-8"))
            record["prompt_version"] = "clockify-semantic-v2"
            record["cache_key"] = semantic.stable_digest(
                "arc-",
                {
                    "schema_version": semantic.ANALYZER_CACHE_SCHEMA_VERSION,
                    "prompt_version": record["prompt_version"],
                    "semantic_schema_version": record["semantic_schema_version"],
                    "route_digest": record["route_digest"],
                    "body_digest": record["body_digest"],
                },
                length=64,
            )
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            reloaded = semantic.AnalyzerResponseCache(path)
            self.assertIsNone(reloaded.lookup(endpoint, body))

    def test_cache_preserves_primary_rejection_and_fallback_choice(self):
        calls: list[tuple[str, str]] = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                calls.append((endpoint.name, "probe"))
                return {"probe": "ok"}
            calls.append((endpoint.name, "evidence"))
            evidence_id = payload["events"][0]["evidence_id"]
            if endpoint.name == "primary":
                return {"activities": [], "exceptions": [], "omissions": []}
            return valid_response(evidence_id)

        primary = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        fallback = semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                [event("ev-1")],
                primary=primary,
                fallback=fallback,
                transport=transport,
                private_text_approved=True,
                cache=semantic.AnalyzerResponseCache(path),
            )
            second = semantic.analyze_tiered(
                [event("ev-1")],
                primary=primary,
                fallback=fallback,
                transport=transport,
                private_text_approved=True,
                cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertEqual(first["activities"], second["activities"])
        self.assertEqual(
            [
                ("primary", "probe"),
                ("primary", "evidence"),
                ("fallback", "probe"),
                ("fallback", "evidence"),
            ],
            calls,
        )

    def test_stale_cache_writer_rejects_conflicting_decision(self):
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        body = semantic._body_for(
            [event("ev-1")], model="cheap", mode="extract", private_text_approved=True
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.AnalyzerResponseCache(path)
            stale = semantic.AnalyzerResponseCache(path)
            first.store_accepted(endpoint, body, valid_response("ev-1"))
            conflicting = valid_response("ev-1")
            conflicting["activities"][0]["outcome"] = "different accepted wording"
            with self.assertRaisesRegex(
                semantic.AnalyzerError, "cannot replace an existing decision"
            ):
                stale.store_accepted(endpoint, body, conflicting)


if __name__ == "__main__":
    unittest.main()
