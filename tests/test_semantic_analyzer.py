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
import urllib.error

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


def provider_members(payload: dict) -> list[dict]:
    """Return only opaque, provider-visible evidence members in request order."""
    return [
        {"bundle_ref": bundle["bundle_ref"], **member}
        for bundle in payload["bundles"]
        for member in bundle["members"]
    ]


def provider_partitions(members: list[dict]) -> list[dict]:
    """Compact opaque members into the extraction partition contract."""
    grouped: dict[str, list[int]] = {}
    for member in members:
        grouped.setdefault(member["bundle_ref"], []).append(member["member"])
    partitions = []
    for bundle_ref, positions in grouped.items():
        ordered = sorted(positions)
        ranges = []
        start = end = ordered[0]
        for position in ordered[1:]:
            if position == end + 1:
                end = position
            else:
                ranges.append([start, end])
                start = end = position
        ranges.append([start, end])
        partitions.append({"bundle_ref": bundle_ref, "member_ranges": ranges})
    return partitions


def provider_response(payload: dict, members: list[dict] | None = None) -> dict:
    """Fixture provider response: opaque partitions, never local evidence IDs."""
    selected = members if members is not None else provider_members(payload)
    response = valid_response("unused-local-id")
    activity = response["activities"][0]
    activity.pop("evidence_ids")
    activity["evidence_partitions"] = provider_partitions(selected)
    activity["evidence_spans"] = [member["time_span"] for member in selected]
    return response


@mock.patch.dict(
    os.environ,
    {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"},
    clear=False,
)
class SemanticAnalyzerTests(unittest.TestCase):
    def test_primary_accepts_protected_openai_client_environment(self):
        environment = {
            "OPENAI_BASE_URL": "https://precision-llm.example/v1",
            "OPENAI_API_KEY": "gateway-bearer",
            "OPENAI_MODEL": "deepseek-v4-flash:cloud",
            "CF_ACCESS_CLIENT_ID": "access-id",
            "CF_ACCESS_CLIENT_SECRET": "access-secret",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            endpoint = semantic.AnalyzerEndpoint.from_env(
                "CLOCKIFY_ANALYZER_PRIMARY",
                default_model=semantic.DEFAULT_PRIMARY_MODEL,
            )

        self.assertIsNotNone(endpoint)
        self.assertEqual(
            "https://precision-llm.example/v1/chat/completions", endpoint.url
        )
        self.assertEqual("gateway-bearer", endpoint.api_key)
        self.assertEqual("access-id", endpoint.cf_access_client_id)
        self.assertEqual("access-secret", endpoint.cf_access_client_secret)

    def test_http_transport_sends_protected_gateway_headers(self):
        endpoint = semantic.AnalyzerEndpoint(
            "primary",
            "https://precision-llm.example/v1/chat/completions",
            semantic.DEFAULT_PRIMARY_MODEL,
            api_key="gateway-bearer",
            cf_access_client_id="access-id",
            cf_access_client_secret="access-secret",
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        with mock.patch(
            "scripts.semantic_analyzer.urllib.request.urlopen",
            return_value=response,
        ) as opened:
            self.assertEqual(
                {"ok": True}, semantic.http_transport(endpoint, {"messages": []})
            )

        request = opened.call_args.args[0]
        self.assertEqual("Bearer gateway-bearer", request.get_header("Authorization"))
        self.assertEqual("access-id", request.get_header("Cf-access-client-id"))
        self.assertEqual("access-secret", request.get_header("Cf-access-client-secret"))

    def test_http_transport_separates_retryable_and_hard_http_failures(self):
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "qualified")
        body = {"model": "qualified", "messages": []}
        retryable = urllib.error.HTTPError(
            endpoint.url, 503, "unavailable", None, None
        )
        with mock.patch(
            "scripts.semantic_analyzer.urllib.request.urlopen",
            side_effect=retryable,
        ):
            with self.assertRaises(semantic.AnalyzerRetryableHTTPError) as raised:
                semantic.http_transport(endpoint, body)
        self.assertEqual(503, raised.exception.status_code)
        retryable.close()

        hard = urllib.error.HTTPError(endpoint.url, 401, "unauthorized", None, None)
        with mock.patch(
            "scripts.semantic_analyzer.urllib.request.urlopen",
            side_effect=hard,
        ):
            with self.assertRaises(semantic.AnalyzerError) as raised:
                semantic.http_transport(endpoint, body)
        self.assertNotIsInstance(raised.exception, semantic.AnalyzerTransportError)
        hard.close()

    def test_prompt_hard_gates_title_only_meetings(self):
        system = semantic._request_messages(
            [event("ev-1")],
            mode="extract",
            private_text_approved=True,
        )[0]["content"]

        self.assertEqual("clockify-semantic-v17", semantic.PROMPT_VERSION)
        self.assertIn("MEETING SUFFICIENCY IS A HARD GATE", system)
        self.assertIn("MUST NOT produce an activity", system)
        self.assertIn("exactly one insufficient_evidence exception", system)
        self.assertIn("medium for timestamped paired", system)
        self.assertIn("Do not mark timing low merely", system)
        self.assertIn("Preserve domain qualifiers that distinguish", system)
        self.assertIn("putting the qualifier only in workstream", system)
        self.assertIn("Repository commits are corroboration only", system)

    def test_independent_flash_review_corrects_extraction_semantics(self):
        calls: list[str] = []
        taxonomy = [{
            "project_name": "Serenichron Level 2",
            "prefix": "SC",
            "tag_names": ["Processes"],
            "billable": True,
        }]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            mode = payload.get("mode")
            calls.append(mode)
            response = provider_response(payload)
            if mode == "extract":
                response["activities"][0]["action"] = "Fixed and published"
                return response
            self.assertEqual("review", mode)
            self.assertEqual(
                "Fixed and published",
                payload["candidate"]["activities"][0]["action"],
            )
            self.assertEqual(taxonomy, payload["clockify_taxonomy"])
            response["activities"][0].update({
                "action": "Published",
                "object": "Clockify review descriptions",
                "outcome": "removed transcript fragments",
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
            })
            return response

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint(
                "primary", "http://primary", "flash-review-test"
            ),
            transport=transport,
            review_taxonomy=taxonomy,
        )

        self.assertEqual(["extract", "review"], calls)
        activity = result["activities"][0]
        self.assertEqual("Published", activity["action"])
        self.assertEqual("flash-review-test", activity["semantic_reviewer_model"])
        self.assertEqual(
            semantic.REVIEW_PROMPT_VERSION,
            activity["review_prompt_version"],
        )

    def test_flash_review_prompt_distinguishes_interactive_from_autonomous_work(self):
        system = semantic._review_messages(
            [event("ev-1")],
            candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=[],
        )[0]["content"]

        self.assertEqual("clockify-semantic-review-v6", semantic.REVIEW_PROMPT_VERSION)
        self.assertIn("human instruction plus its resulting assistant/tool evidence", system)
        self.assertIn("Do not omit an accomplishment", system)
        self.assertIn("genuinely", system)
        self.assertIn("autonomous background execution", system)
        self.assertIn("Every omission requires lifecycle planned or noise", system)
        self.assertIn("repository commits only to corroborate", system)
        self.assertIn("plain Caveman wording", system)
        self.assertIn("route_hint", system)
        payload = json.loads(semantic._review_messages(
            [event("ev-1")],
            candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=[],
        )[1]["content"])
        self.assertEqual(
            [{"bundle_ref": "b-0001", "allowed_member_range": [1, 1]}],
            payload["coverage_contract"],
        )

        hinted = event("ev-1")
        hinted["semantic_route_hint"] = {
            "action": "route",
            "project_name": "Serenichron Level 2",
            "prefix": "SC",
            "tag_names": ["System development"],
            "confidence": "high",
        }
        hinted_payload = json.loads(semantic._review_messages(
            [hinted],
            candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=[],
        )[1]["content"])
        self.assertEqual(
            hinted["semantic_route_hint"],
            hinted_payload["bundles"][0]["members"][0]["route_hint"],
        )

    def test_portfolio_review_prompt_requires_invoice_worthy_consolidation(self):
        messages = semantic._review_messages(
            [event("ev-1")],
            candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=[],
            review_scope="portfolio",
            review_prompt_version=semantic.PORTFOLIO_REVIEW_PROMPT_VERSION,
        )

        self.assertIn("invoice-worthy accomplishments", messages[0]["content"])
        self.assertIn("one row per message", messages[0]["content"])
        self.assertIn("20–90 minute accomplishment", messages[0]["content"])
        self.assertIn("search→recovery or transfer", messages[0]["content"])
        self.assertIn("Never exceed 14 total words", messages[0]["content"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual("portfolio", payload["review_scope"])
        self.assertEqual(
            semantic.PORTFOLIO_REVIEW_PROMPT_VERSION,
            payload["review_prompt_version"],
        )

    def test_portfolio_validation_is_a_separate_semantic_contract(self):
        messages = semantic._review_messages(
            [event("ev-1")],
            candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=[],
            review_scope="portfolio_validation",
            review_prompt_version=semantic.PORTFOLIO_VALIDATION_PROMPT_VERSION,
        )

        self.assertIn("Independently compare the candidate", messages[0]["content"])
        self.assertIn("client/project level", messages[0]["content"])
        self.assertIn("Never exceed 14 total words", messages[0]["content"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual("portfolio_validation", payload["review_scope"])
        self.assertEqual(
            semantic.PORTFOLIO_VALIDATION_PROMPT_VERSION,
            payload["review_prompt_version"],
        )

    def test_single_activity_recovery_requires_exact_flash_citation_audit(self):
        messages = semantic._review_messages(
            [event("ev-1")],
            candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=[],
            review_scope="portfolio_single_activity_recovery",
            review_prompt_version="clockify-portfolio-single-activity-recovery-v2",
        )

        self.assertIn("one source candidate", messages[0]["content"])
        self.assertIn("entire allowed_member_range", messages[0]["content"])
        self.assertIn("every\ninteger member must occur exactly once", messages[0]["content"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual(
            "portfolio_single_activity_recovery", payload["review_scope"]
        )

    def test_flash_review_owns_wording_and_effort_without_python_rewrite(self):
        taxonomy = [{
            "project_name": "Serenichron Level 2",
            "prefix": "SC",
            "tag_names": ["Processes"],
            "billable": True,
        }]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            response = provider_response(payload)
            if payload.get("mode") == "review":
                response["activities"][0].update({
                    "action": "Fixed and verified",
                    "object": "Clockify review output",
                    "outcome": "scheduled Friday follow-up one-to-one",
                    "effort": {
                        "minimum_minutes": 60,
                        "recommended_minutes": 120,
                        "maximum_minutes": 180,
                    },
                    "project_recommendation": {
                        "name": "Serenichron Level 2",
                        "prefix": "SC",
                        "tag_names": ["Processes"],
                    },
                })
            return response

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint(
                "primary", "http://primary", "flash-review-test"
            ),
            transport=transport,
            review_taxonomy=taxonomy,
        )

        activity = result["activities"][0]
        self.assertEqual("Fixed and verified", activity["action"])
        self.assertEqual(
            "scheduled Friday follow-up one-to-one",
            activity["outcome"],
        )
        self.assertEqual(120, activity["effort"]["recommended_minutes"])

    def test_flash_review_structural_repair_exhaustion_does_not_retry_extractor(self):
        calls: list[str] = []
        taxonomy = [{
            "project_name": "Serenichron Level 2",
            "prefix": "SC",
            "tag_names": ["Processes"],
            "billable": True,
        }]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            calls.append(payload["mode"])
            if payload["mode"] == "extract":
                return provider_response(payload)
            return {"activities": [], "exceptions": [], "omissions": []}

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint(
                "primary", "http://primary", "flash-review-test"
            ),
            transport=transport,
            review_taxonomy=taxonomy,
        )

        self.assertEqual(["extract", "review", "review"], calls)
        self.assertEqual([], result["activities"])
        self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])
        self.assertIn("structural repair", result["exceptions"][0]["reason"])

    def test_flash_review_timeout_recovers_without_rerunning_extractor_and_replays(self):
        taxonomy = [{
            "project_name": "Serenichron Level 2",
            "prefix": "SC",
            "tag_names": ["Processes"],
            "billable": True,
        }]
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            calls.append(payload["mode"])
            if payload["mode"] == "extract":
                return provider_response(payload)
            raise semantic.AnalyzerTimeoutError("review timed out")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                [event("ev-1")],
                primary=semantic.AnalyzerEndpoint(
                    "primary", "http://primary", "flash-review-test"
                ),
                transport=transport,
                cache=semantic.AnalyzerResponseCache(path),
                review_taxonomy=taxonomy,
            )
            second = semantic.analyze_tiered(
                [event("ev-1")],
                primary=semantic.AnalyzerEndpoint(
                    "primary", "http://primary", "flash-review-test"
                ),
                transport=lambda *_: self.fail("sealed review replay called transport"),
                cache=semantic.AnalyzerResponseCache(path),
                review_taxonomy=taxonomy,
            )
            records = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(1, calls.count("extract"))
        self.assertEqual(4, calls.count("review"))
        self.assertEqual(first["exceptions"], second["exceptions"])
        self.assertEqual(1, sum(row["status"] == "accepted" for row in records))
        self.assertEqual(4, sum(row["status"] == "rejected" for row in records))

    def test_extractor_and_flash_reviewer_cache_identities_replay_independently(self):
        taxonomy = [{
            "project_name": "Serenichron Level 2",
            "prefix": "SC",
            "tag_names": ["Processes"],
            "billable": True,
        }]
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            calls.append(payload["mode"])
            response = provider_response(payload)
            if payload["mode"] == "review":
                response["activities"][0]["project_recommendation"] = {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                }
            return response

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first_cache = semantic.AnalyzerResponseCache(path)
            first = semantic.analyze_tiered(
                [event("ev-1")],
                primary=semantic.AnalyzerEndpoint(
                    "primary", "http://primary", "flash-review-test"
                ),
                transport=transport,
                cache=first_cache,
                review_taxonomy=taxonomy,
            )
            second_cache = semantic.AnalyzerResponseCache(path)
            second = semantic.analyze_tiered(
                [event("ev-1")],
                primary=semantic.AnalyzerEndpoint(
                    "primary", "http://primary", "flash-review-test"
                ),
                transport=lambda *_: self.fail("cache replay called transport"),
                cache=second_cache,
                review_taxonomy=taxonomy,
            )

        self.assertEqual(["extract", "review"], calls)
        self.assertEqual(first["activities"], second["activities"])
        self.assertEqual(2, first_cache.misses)
        self.assertEqual(2, second_cache.hits)

    def test_flash_review_rejects_project_and_task_outside_taxonomy(self):
        calls: list[str] = []
        taxonomy = [{
            "project_name": "Serenichron Level 2",
            "prefix": "SC",
            "tag_names": ["Processes"],
            "billable": True,
        }]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            calls.append(payload["mode"])
            response = provider_response(payload)
            if payload["mode"] == "review":
                response["activities"][0]["project_recommendation"] = {
                    "name": "Invented Client",
                    "prefix": "XX",
                    "tag_names": ["Invented Task"],
                }
            return response

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint(
                "primary", "http://primary", "flash-review-test"
            ),
            transport=transport,
            review_taxonomy=taxonomy,
        )

        self.assertEqual(["extract", "review", "review"], calls)
        self.assertEqual([], result["activities"])
        self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])

    def test_production_primary_requires_flash_and_rejects_pro(self):
        base = {
            "CLOCKIFY_ANALYZER_PRIMARY_URL": "http://analyzer.test/v1/chat",
            "CLOCKIFY_ANALYZER_PRIMARY_MODEL": "other-model",
        }
        with mock.patch.dict(os.environ, base, clear=False):
            with self.assertRaisesRegex(
                semantic.AnalyzerError, "approved DeepSeek V4 Flash cloud alias"
            ):
                semantic.AnalyzerEndpoint.from_env(
                    "CLOCKIFY_ANALYZER_PRIMARY",
                    default_model=semantic.DEFAULT_PRIMARY_MODEL,
                )
        preview_alias = {
            "CLOCKIFY_ANALYZER_PRIMARY_URL": "http://analyzer.test/v1/chat",
            "CLOCKIFY_ANALYZER_PRIMARY_MODEL": "deepseek-v4-flash:cloud",
        }
        with mock.patch.dict(os.environ, preview_alias, clear=False):
            endpoint = semantic.AnalyzerEndpoint.from_env(
                "CLOCKIFY_ANALYZER_PRIMARY",
                default_model=semantic.DEFAULT_PRIMARY_MODEL,
            )
        self.assertEqual("deepseek-v4-flash:cloud", endpoint.model)
        with self.assertRaisesRegex(semantic.AnalyzerError, "V4 Pro is not approved"):
            semantic.AnalyzerEndpoint(
                "fallback", "http://analyzer.test", "deepseek-v4-pro:cloud"
            )

    def test_explicit_0731_cloud_tag_requires_release_revision(self):
        endpoint = semantic.AnalyzerEndpoint(
            "primary",
            "http://analyzer.test/v1/chat",
            semantic.DEFAULT_PRIMARY_MODEL,
        )
        with self.assertRaisesRegex(
            semantic.AnalyzerError,
            "cloud model tags require an explicit 64-character release revision",
        ):
            semantic.probe_endpoint(endpoint, transport=lambda *_args: {"probe": "ok"})

    def test_provider_metadata_is_ignored_before_extraction_validation(self):
        events = [event("ev-1")]
        _bundles, manifest = semantic._semantic_evidence_bundles(events)
        response = provider_response(
            json.loads(
                semantic._body_for(
                    events,
                    model=semantic.DEFAULT_PRIMARY_MODEL,
                    mode="extract",
                    private_text_approved=True,
                )["messages"][1]["content"]
            )
        )
        response["provider_metadata"] = {"release": "current"}
        response["activities"][0]["provider_note"] = "structured output"

        restored = semantic._restore_extraction_partitions(response, events=events)

        self.assertEqual({"activities", "exceptions", "omissions"}, set(restored))
        self.assertNotIn("provider_note", restored["activities"][0])
        self.assertEqual(manifest[0]["evidence_ids"], restored["activities"][0]["evidence_ids"])

    def test_provider_normalization_requires_all_classification_lists(self):
        with self.assertRaisesRegex(
            semantic.AnalyzerError,
            "activities, exceptions, and omissions must be lists",
        ):
            semantic._normalize_provider_response(
                {"activities": [], "exceptions": []}, mode="extract"
            )
        with self.assertRaisesRegex(
            semantic.AnalyzerError,
            "activities, exceptions, and omissions must be lists",
        ):
            semantic._normalize_provider_response(
                {"activities": [], "exceptions": [], "omissions": {}},
                mode="extract",
            )

    def test_provider_normalization_does_not_repair_invalid_citations(self):
        response = {
            "activities": [{
                "lifecycle": "completed",
                "evidence_partitions": [{
                    "bundle_ref": "unknown-bundle",
                    "member_ranges": [[1, 1]],
                }],
            }],
            "exceptions": [],
            "omissions": [],
            "provider_metadata": True,
        }
        with self.assertRaisesRegex(semantic.AnalyzerError, "unknown or repeated bundle"):
            semantic._restore_extraction_partitions(response, events=[event("ev-1")])

    def test_synthesis_provider_metadata_is_pruned(self):
        response = valid_response("ref-0001")
        response["provider_metadata"] = {"release": "current"}
        response["activities"][0]["provider_note"] = "structured output"

        normalized = semantic._normalize_provider_response(
            response, mode="synthesize"
        )

        self.assertEqual({"activities", "exceptions", "omissions"}, set(normalized))
        self.assertNotIn("provider_note", normalized["activities"][0])
        self.assertEqual(["ref-0001"], normalized["activities"][0]["evidence_ids"])

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

    def test_exception_and_omission_require_structural_disposition_fields(self):
        invalid = [
            {
                "activities": [],
                "exceptions": [{
                    "kind": "insufficient_evidence",
                    "evidence_ids": ["ev-1"],
                    "reason": "",
                }],
                "omissions": [],
            },
            {
                "activities": [],
                "exceptions": [],
                "omissions": [{
                    "evidence_ids": ["ev-1"],
                    "reason": "autonomous execution",
                }],
            },
        ]
        for response in invalid:
            with self.subTest(response=response), self.assertRaisesRegex(
                semantic.AnalyzerError,
                "nonempty reason|omission lifecycle",
            ):
                semantic.validate_result(
                    response,
                    known_evidence_ids={"ev-1"},
                    provider_model="model-a",
                    analyzer_tier="primary",
                    semantic_validation=False,
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

    def test_rejects_activity_parts_that_cannot_render_as_caveman_text(self):
        response = valid_response("ev-1")
        response["activities"][0]["object"] = "Clockify descriptions and allocations"
        response["activities"][0]["outcome"] = (
            "removed transcript fragments while preserving every evidence reference without truncation"
        )

        with self.assertRaisesRegex(semantic.AnalyzerError, "Caveman render contract"):
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

    def test_bounded_source_spans_canonicalize_effort_and_timing_replays(self):
        first = valid_response("ev-1")
        first["activities"][0].update({
            "action": "Fixed",
            "outcome": "removed copied transcript fragments",
            "evidence_spans": [{
                "start": "2026-07-10T10:00:00+03:00",
                "end": "2026-07-10T10:28:00+03:00",
            }],
            "effort": {
                "minimum_minutes": 28,
                "recommended_minutes": 28,
                "maximum_minutes": 28,
            },
            "timing_confidence": "high",
        })
        second = valid_response("ev-1")
        second["activities"][0].update({
            "action": "Corrected",
            "outcome": "cleared copied transcript fragments",
            "evidence_spans": [{
                "start": "2026-07-10T10:00:00+03:00",
                "end": "2026-07-10T10:28:00+03:00",
            }],
            "effort": {
                "minimum_minutes": 10,
                "recommended_minutes": 20,
                "maximum_minutes": 28,
            },
            "timing_confidence": "low",
        })
        source_spans = {
            "ev-1": {
                "start": "2026-07-10T10:00:00+03:00",
                "end": "2026-07-10T10:28:00+03:00",
            }
        }

        left = semantic.validate_result(
            first,
            known_evidence_ids={"ev-1"},
            provider_model="model-a",
            analyzer_tier="primary",
            evidence_time_spans=source_spans,
        )["activities"][0]
        right = semantic.validate_result(
            second,
            known_evidence_ids={"ev-1"},
            provider_model="model-a",
            analyzer_tier="primary",
            evidence_time_spans=source_spans,
        )["activities"][0]

        review_fields = (
            "evidence_ids",
            "lifecycle",
            "effort",
            "semantic_confidence",
            "timing_confidence",
        )
        self.assertEqual(
            {field: left[field] for field in review_fields},
            {field: right[field] for field in review_fields},
        )
        self.assertEqual(
            {"minimum_minutes": 10, "recommended_minutes": 20, "maximum_minutes": 30},
            left["effort"],
        )
        self.assertEqual("medium", left["timing_confidence"])
        self.assertEqual(
            (10, 15, 20),
            semantic._effort_from_bounded_capacity(
                semantic._bounded_evidence_capacity_minutes(
                    ["ev-2"],
                    {
                        "ev-2": {
                            "start": "2026-07-10T10:30:00+03:00",
                            "end": "2026-07-10T10:48:00+03:00",
                        }
                    },
                )
            ),
        )

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

        # Packing may build a bounded number of bundle-aware sizing bodies,
        # but must remain linear and start with the empty-envelope baseline.
        self.assertLessEqual(body_for.call_count, len(events) + 1)
        self.assertEqual([], body_for.call_args_list[0].args[0])
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

    def test_chunking_keeps_interleaved_sessions_contiguous_before_packing(self):
        events = []
        for minute, session_id, evidence_id in (
            (0, "session-a", "ev-a1"),
            (1, "session-b", "ev-b1"),
            (2, "session-a", "ev-a2"),
            (3, "session-b", "ev-b2"),
        ):
            item = event(evidence_id)
            item["observed_start"] = f"2026-07-10 10:{minute:02d}"
            item["observed_end"] = f"2026-07-10 10:{minute + 1:02d}"
            item["source_ref"] = {
                "source_type": "codex_sessions",
                "machine": "test-machine",
                "session_id": session_id,
            }
            events.append(item)

        chunks = semantic.chunk_events(
            reversed(events),
            max_body_bytes=50_000,
            max_events_per_chunk=3,
        )

        self.assertEqual(
            [["ev-a1", "ev-a2"], ["ev-b1", "ev-b2"]],
            [[item["evidence_id"] for item in chunk] for chunk in chunks],
        )
        self.assertEqual(
            {item["evidence_id"] for item in events},
            {item["evidence_id"] for chunk in chunks for item in chunk},
        )

    def test_identified_session_crossing_midnight_remains_one_semantic_bundle(self):
        events = []
        for evidence_id, start, end in (
            ("ev-before-midnight", "2026-07-10 23:58", "2026-07-10 23:59"),
            ("ev-after-midnight", "2026-07-11 00:01", "2026-07-11 00:03"),
        ):
            item = event(evidence_id)
            item.update({
                "observed_start": start,
                "observed_end": end,
                "source_ref": {
                    "source_type": "codex_sessions",
                    "machine": "test-machine",
                    "session_id": "cross-midnight-session",
                },
            })
            events.append(item)

        chunks = semantic.chunk_events(events, max_body_bytes=50_000)
        self.assertEqual(1, len(chunks))
        body = semantic._body_for(chunks[0], model="test", mode="extract")
        payload = json.loads(body["messages"][1]["content"])
        self.assertEqual(1, len(payload["bundles"]))
        self.assertEqual(2, payload["bundles"][0]["member_count"])

        restored = semantic._restore_extraction_partitions(
            provider_response(payload), events=chunks[0]
        )
        self.assertEqual(
            ["ev-after-midnight", "ev-before-midnight"],
            sorted(restored["activities"][0]["evidence_ids"]),
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
            calls.append((endpoint.name, "probe" if '"bundles"' not in body["messages"][1]["content"] else "evidence"))
            if '"bundles"' not in body["messages"][1]["content"]:
                return {"probe": "ok"}
            if endpoint.name == "primary":
                return {"choices": [{"message": {"content": "not-json"}}]}
            payload = json.loads(body["messages"][1]["content"])
            return provider_response(payload)

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )
        self.assertEqual(
            [
                ("primary", "probe"), ("primary", "evidence"),
                ("primary", "evidence"), ("primary", "evidence"),
                ("fallback", "probe"), ("fallback", "evidence"),
            ],
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
            return provider_response(payload)

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

    def test_contract_rejection_uses_one_accepted_repair_request(self):
        calls: list[dict] = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            calls.append({
                "endpoint": endpoint.name,
                "payload": payload,
                "system": body["messages"][0]["content"],
            })
            if payload.get("probe"):
                return {"probe": "ok"}
            response = provider_response(payload)
            if "repair_feedback" not in payload:
                response["activities"][0]["action"] = "Fixed and published"
            return response

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
        )

        requests = [call["payload"] for call in calls if not call["payload"].get("probe")]
        self.assertEqual(2, len(requests))
        self.assertNotIn("repair_feedback", requests[0])
        self.assertEqual(
            "contract_rejected_compound_action",
            requests[1]["repair_feedback"]["failure_code"],
        )
        self.assertEqual(1, requests[1]["repair_feedback"]["attempt"])
        self.assertEqual(2, requests[1]["repair_feedback"]["maximum_attempts"])
        repair_call = [call for call in calls if "repair_feedback" in call["payload"]][0]
        self.assertIn("CORRECTIVE RETRY", repair_call["system"])
        self.assertIn("Return one atomic past-tense action", repair_call["system"])
        self.assertNotIn("Fixed and published", repair_call["system"])
        self.assertEqual("used", result["analysis_chunks"][0]["repair_status"])
        self.assertEqual("primary", result["analysis_chunks"][0]["tier"])

    def test_contract_rejection_can_use_second_bounded_repair_attempt(self):
        calls: list[tuple[dict, int]] = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            calls.append((payload, body.get("seed")))
            if payload.get("probe"):
                return {"probe": "ok"}
            response = provider_response(payload)
            attempt = (payload.get("repair_feedback") or {}).get("attempt", 0)
            if attempt < 2:
                response["activities"][0]["action"] = "Fixed and published"
            return response

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
        )

        evidence_calls = [(payload, seed) for payload, seed in calls if not payload.get("probe")]
        self.assertEqual([0, 1, 2], [seed for _, seed in evidence_calls])
        self.assertEqual(
            [None, 1, 2],
            [
                (payload.get("repair_feedback") or {}).get("attempt")
                for payload, _ in evidence_calls
            ],
        )
        self.assertEqual("used", result["analysis_chunks"][0]["repair_status"])
        self.assertEqual("primary", result["analysis_chunks"][0]["tier"])

    def test_rejected_repair_passes_category_to_fallback_then_stops(self):
        calls: list[tuple[str, dict]] = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            calls.append((endpoint.name, payload))
            if payload.get("probe"):
                return {"probe": "ok"}
            response = provider_response(payload)
            if endpoint.name == "primary":
                response["activities"][0]["action"] = "Fixed and published"
            return response

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )

        evidence_calls = [(name, payload) for name, payload in calls if not payload.get("probe")]
        self.assertEqual(
            ["primary", "primary", "primary", "fallback"],
            [name for name, _ in evidence_calls],
        )
        self.assertEqual(
            "contract_rejected_compound_action",
            evidence_calls[-1][1]["repair_feedback"]["failure_code"],
        )
        self.assertEqual("rejected", result["analysis_chunks"][0]["repair_status"])
        self.assertEqual("fallback", result["analysis_chunks"][0]["tier"])

    def test_endpoint_failure_does_not_attempt_repair(self):
        calls: list[tuple[str, dict]] = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            calls.append((endpoint.name, payload))
            if payload.get("probe"):
                return {"probe": "ok"}
            if endpoint.name == "primary":
                raise semantic.AnalyzerError("primary route unavailable")
            return provider_response(payload)

        result = semantic.analyze_tiered(
            [event("ev-1")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
        )

        evidence_calls = [(name, payload) for name, payload in calls if not payload.get("probe")]
        self.assertEqual(["primary", "fallback"], [name for name, _ in evidence_calls])
        self.assertNotIn("repair_feedback", evidence_calls[-1][1])
        self.assertEqual("not_attempted", result["analysis_chunks"][0]["repair_status"])

    def test_low_confidence_primary_uses_stronger_fallback(self):
        calls = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                calls.append((endpoint.name, "probe"))
                return {"probe": "ok"}
            calls.append((endpoint.name, "extract"))
            response = provider_response(payload)
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
            response = provider_response(payload)
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
            response = provider_response(payload)
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
                ("primary", "extract"),
                ("primary", "extract"),
                ("fallback", "probe"),
                ("fallback", "extract"),
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

    def test_dual_contract_failure_recovers_by_contiguous_bisection(self):
        extract_sizes: list[tuple[str, int]] = []
        child_successes = 0

        def transport(endpoint, body):
            nonlocal child_successes
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "synthesize":
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
                    "merge_rationale": "same recovered Clockify work",
                })
                return response
            members = provider_members(payload)
            extract_sizes.append((endpoint.name, len(members)))
            if len(members) > 1:
                return {"activities": [], "exceptions": [], "omissions": []}
            response = provider_response(payload)
            child_successes += 1
            response["activities"][0].update({
                "object": f"Clockify description {child_successes}",
                "workstream": f"Clockify stream {child_successes}",
            })
            return response

        events = [event("ev-a"), event("ev-b")]
        result = semantic.analyze_tiered(
            events,
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
            max_events_per_chunk=2,
        )

        self.assertEqual([], result["exceptions"])
        self.assertEqual(["ev-a", "ev-b"], sorted(
            evidence_id
            for activity in result["activities"]
            for evidence_id in activity["evidence_ids"]
        ))
        self.assertEqual(
            [
                ("fallback", 2), ("fallback", 2),
                ("primary", 1), ("primary", 1),
                ("primary", 2), ("primary", 2), ("primary", 2),
            ],
            sorted(extract_sizes),
        )
        chunk = result["analysis_chunks"][0]
        self.assertEqual("recovered_by_partition", chunk["recovery_status"])
        self.assertEqual("recovered", chunk["recovery"]["status"])
        self.assertEqual(["root.a", "root.b"], [
            child["partition_path"] for child in chunk["recovery"]["children"]
        ])

    def test_single_qualified_route_recovers_sealed_contract_partition(self):
        child_number = 0

        def transport(_endpoint, body):
            nonlocal child_number
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            members = provider_members(payload)
            if len(members) > 1:
                return {"activities": [], "exceptions": [], "omissions": []}
            child_number += 1
            response = provider_response(payload)
            response["activities"][0].update({
                "object": f"recovered outcome {child_number}",
                "workstream": f"recovered stream {child_number}",
            })
            return response

        result = semantic.analyze_tiered(
            [event("ev-a"), event("ev-b")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "qualified"),
            transport=transport,
            max_events_per_chunk=2,
        )
        self.assertEqual([], result["exceptions"])
        self.assertEqual(2, len(result["activities"]))
        chunk = result["analysis_chunks"][0]
        self.assertEqual("recovered_by_partition", chunk["recovery_status"])
        self.assertEqual("recovered", chunk["recovery"]["status"])

    def test_single_route_outage_blocks_without_partition_recovery(self):
        evidence_calls: list[int] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            evidence_calls.append(len(provider_members(payload)))
            raise semantic.AnalyzerError("route unavailable")

        with self.assertRaisesRegex(semantic.AnalyzerError, "primary analyzer failed"):
            semantic.analyze_tiered(
                [event("ev-a"), event("ev-b")],
                primary=semantic.AnalyzerEndpoint("primary", "http://primary", "qualified"),
                transport=transport,
                max_events_per_chunk=2,
            )
        self.assertEqual([2], evidence_calls)

    def test_single_route_timeout_partitions_and_replays_from_sealed_cache(self):
        calls: list[tuple[str, int]] = []
        child_number = 0

        def transport(_endpoint, body):
            nonlocal child_number
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                calls.append(("probe", 0))
                return {"probe": "ok"}
            if payload.get("mode") == "synthesize":
                provisional = payload["provisional_activities"]
                calls.append(("synthesize", len(provisional)))
                response = valid_response(provisional[0]["evidence_ids"][0])
                response["activities"][0].update({
                    "evidence_ids": sorted(
                        evidence_id
                        for activity in provisional
                        for evidence_id in activity["evidence_ids"]
                    ),
                    "evidence_spans": [
                        span
                        for activity in provisional
                        for span in activity["evidence_spans"]
                    ],
                    "merge_rationale": "same recovered timeout work",
                })
                return response
            members = provider_members(payload)
            calls.append(("extract", len(members)))
            if len(members) > 1:
                raise semantic.AnalyzerTimeoutError("bounded request timed out")
            child_number += 1
            response = provider_response(payload)
            response["activities"][0].update({
                "object": f"recovered timeout {child_number}",
                "workstream": f"timeout stream {child_number}",
            })
            return response

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "qualified")
        events = [event("ev-a"), event("ev-b")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=transport,
                max_events_per_chunk=2,
                cache=semantic.AnalyzerResponseCache(path),
            )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            second = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=lambda *_: self.fail(
                    "sealed timeout recovery replay must not call transport"
                ),
                max_events_per_chunk=2,
                cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertEqual(first["activities"], second["activities"])
        self.assertEqual(first["analysis_chunks"], second["analysis_chunks"])
        self.assertEqual(
            "transport_timeout",
            first["analysis_chunks"][0]["recovery"]["trigger"],
        )
        self.assertEqual("recovered", first["analysis_chunks"][0]["recovery"]["status"])
        self.assertEqual(
            1,
            sum(
                record.get("failure_code") == "transport_timeout"
                for record in records
            ),
        )
        self.assertEqual(
            [
                ("probe", 0),
                ("extract", 2),
                ("extract", 1),
                ("synthesize", 2),
            ],
            calls,
        )

    def test_single_route_indivisible_timeout_gets_one_sealed_recovery_attempt(self):
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                calls.append("probe")
                return {"probe": "ok"}
            if payload.get("timeout_recovery"):
                calls.append("timeout_recovery")
                return provider_response(payload)
            calls.append("extract")
            raise semantic.AnalyzerTimeoutError("bounded request timed out")

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "qualified")
        events = [event("ev-a")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=transport,
                cache=semantic.AnalyzerResponseCache(path),
            )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            second = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=lambda *_: self.fail(
                    "sealed indivisible timeout replay must not call transport"
                ),
                cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertEqual(first["activities"], second["activities"])
        self.assertEqual(first["analysis_chunks"], second["analysis_chunks"])
        self.assertEqual(
            "used", first["analysis_chunks"][0]["timeout_recovery_status"]
        )
        self.assertEqual(
            1,
            sum(
                record.get("failure_code") == "transport_timeout"
                for record in records
            ),
        )
        self.assertEqual(
            ["probe", "extract", "probe", "timeout_recovery"], calls
        )

    def test_single_route_indivisible_timeout_becomes_visible_exception_after_recovery(self):
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                calls.append("probe")
                return {"probe": "ok"}
            recovery = payload.get("timeout_recovery")
            calls.append(
                f"timeout_recovery_{recovery['attempt']}" if recovery else "extract"
            )
            raise semantic.AnalyzerTimeoutError("bounded request timed out")

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "qualified")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                [event("ev-a")],
                primary=endpoint,
                transport=transport,
                cache=semantic.AnalyzerResponseCache(path),
            )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            second = semantic.analyze_tiered(
                [event("ev-a")],
                primary=endpoint,
                transport=lambda *_: self.fail(
                    "sealed exhausted timeout replay must not call transport"
                ),
                cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertEqual(first["exceptions"], second["exceptions"])
        self.assertEqual([], first["activities"])
        self.assertEqual("analyzer_failure", first["exceptions"][0]["kind"])
        self.assertEqual(
            "exhausted_exception",
            first["analysis_chunks"][0]["timeout_recovery_status"],
        )
        self.assertEqual(
            [
                "probe",
                "extract",
                "probe",
                "timeout_recovery_1",
                "probe",
                "timeout_recovery_2",
                "probe",
                "timeout_recovery_3",
            ],
            calls,
        )
        self.assertEqual(
            1 + semantic.MAX_TIMEOUT_RECOVERY_ATTEMPTS,
            sum(
                record.get("failure_code") == "transport_timeout"
                for record in records
            ),
        )

    def test_single_route_connection_loss_reprobes_and_uses_sealed_recovery(self):
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                calls.append("probe")
                return {"probe": "ok"}
            if payload.get("connection_recovery"):
                calls.append("connection_recovery")
                return provider_response(payload)
            calls.append("extract")
            raise semantic.AnalyzerTransportError("connection lost")

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "qualified")
        events = [event("ev-a")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=transport,
                cache=semantic.AnalyzerResponseCache(path),
            )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            second = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=lambda *_: self.fail(
                    "sealed connection recovery replay must not call transport"
                ),
                cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertEqual(first["activities"], second["activities"])
        self.assertEqual(first["analysis_chunks"], second["analysis_chunks"])
        self.assertEqual(
            "used", first["analysis_chunks"][0]["connection_recovery_status"]
        )
        self.assertEqual(
            1,
            sum(
                record.get("failure_code") == "transport_error"
                for record in records
            ),
        )
        self.assertEqual(
            ["probe", "extract", "probe", "connection_recovery"], calls
        )

    def test_single_route_connection_loss_becomes_visible_exception_after_recovery(self):
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                calls.append("probe")
                return {"probe": "ok"}
            recovery = payload.get("connection_recovery")
            calls.append(
                f"connection_recovery_{recovery['attempt']}"
                if recovery
                else "extract"
            )
            raise semantic.AnalyzerTransportError("connection lost")

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "qualified")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                [event("ev-a")],
                primary=endpoint,
                transport=transport,
                cache=semantic.AnalyzerResponseCache(path),
            )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            second = semantic.analyze_tiered(
                [event("ev-a")],
                primary=endpoint,
                transport=lambda *_: self.fail(
                    "sealed exhausted connection replay must not call transport"
                ),
                cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertEqual(first["exceptions"], second["exceptions"])
        self.assertEqual([], first["activities"])
        self.assertEqual("analyzer_failure", first["exceptions"][0]["kind"])
        self.assertEqual(
            "exhausted_exception",
            first["analysis_chunks"][0]["connection_recovery_status"],
        )
        self.assertEqual(
            [
                "probe",
                "extract",
                "probe",
                "connection_recovery_1",
                "probe",
                "connection_recovery_2",
                "probe",
                "connection_recovery_3",
            ],
            calls,
        )
        self.assertEqual(
            1 + semantic.MAX_CONNECTION_RECOVERY_ATTEMPTS,
            sum(
                record.get("failure_code") == "transport_error"
                for record in records
            ),
        )

    def test_single_route_indivisible_rejection_is_visible_exception(self):
        events = []
        for ordinal, role in enumerate(("user", "assistant"), 1):
            source = event(f"ev-{ordinal}")
            source.update({
                "role": role,
                "source_ref": {"session_id": "session-one", "ordinal": ordinal},
            })
            events.append(source)

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            return {"activities": [], "exceptions": [], "omissions": []}

        result = semantic.analyze_tiered(
            events,
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "qualified"),
            transport=transport,
            max_events_per_chunk=2,
        )
        self.assertEqual("analyzer_failure", result["exceptions"][0]["kind"])
        chunk = result["analysis_chunks"][0]
        self.assertEqual("exception", chunk["tier"])
        self.assertEqual("primary_failed_exception", chunk["fallback_status"])
        self.assertNotIn("fallback_endpoint", chunk)
        self.assertNotIn("fallback_model", chunk)

    def test_partition_recovery_is_sealed_and_replays_without_transport(self):
        calls: list[str] = []
        child_successes = 0

        def transport(endpoint, body):
            nonlocal child_successes
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                calls.append("probe")
                return {"probe": "ok"}
            if payload["mode"] == "synthesize":
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
                    "merge_rationale": "same recovered Clockify work",
                })
                return response
            calls.append(endpoint.name)
            members = provider_members(payload)
            if len(members) > 1:
                return {"activities": [], "exceptions": [], "omissions": []}
            response = provider_response(payload)
            child_successes += 1
            response["activities"][0].update({
                "object": f"Clockify description {child_successes}",
                "workstream": f"Clockify stream {child_successes}",
            })
            return response

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        fallback = semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong")
        events = [event("ev-a"), event("ev-b")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                events, primary=endpoint, fallback=fallback, transport=transport,
                max_events_per_chunk=2, cache=semantic.AnalyzerResponseCache(path),
            )
            second = semantic.analyze_tiered(
                events, primary=endpoint, fallback=fallback,
                transport=lambda *_: self.fail("partition recovery replay must use sealed cache"),
                max_events_per_chunk=2, cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertTrue(calls)
        self.assertEqual(first["activities"], second["activities"])
        self.assertEqual(first["analysis_chunks"], second["analysis_chunks"])
        self.assertIn("recovery", first["analysis_chunks"][0])
        self.assertEqual("recovered", second["analysis_chunks"][0]["recovery"]["status"])

    def test_partition_recovery_stops_at_single_evidence_without_loss_or_duplication(self):
        extract_calls: list[tuple[str, int]] = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            extract_calls.append((endpoint.name, len(provider_members(payload))))
            return {"activities": [], "exceptions": [], "omissions": []}

        events = [event(f"ev-{number}") for number in range(4)]
        result = semantic.analyze_tiered(
            events,
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
            max_events_per_chunk=4,
        )

        self.assertEqual(35, len(extract_calls))  # 7 deterministic tree nodes × (3 primary + 2 fallback)
        self.assertTrue(all(size >= 1 for _endpoint, size in extract_calls))
        self.assertEqual(
            [item["evidence_id"] for item in events],
            sorted(
                evidence_id
                for exception in result["exceptions"]
                for evidence_id in exception["evidence_ids"]
            ),
        )
        recovery = result["analysis_chunks"][0]["recovery"]
        self.assertEqual("exhausted", recovery["status"])
        self.assertLessEqual(recovery["max_depth"], semantic.MAX_PARTITION_RECOVERY_DEPTH)

    def test_partition_recovery_rejects_colliding_child_activity_identities(self):
        def fake_call(_endpoint, chunk, **_kwargs):
            if len(chunk) > 1:
                raise semantic.AnalyzerContractError("sealed contract rejection")
            return {
                "activities": [{
                    "activity_id": "act-colliding",
                    "evidence_ids": [chunk[0]["evidence_id"]],
                }],
                "exceptions": [],
                "omissions": [],
            }

        with mock.patch.object(semantic, "_call_validated", side_effect=fake_call):
            with self.assertRaisesRegex(
                semantic.AnalyzerError,
                "partition recovery emitted colliding activity identities",
            ):
                semantic.analyze_tiered(
                    [event("ev-a"), event("ev-b")],
                    primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
                    fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
                    max_events_per_chunk=2,
                )

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
            if '"bundles"' not in body["messages"][1]["content"]:
                return {"probe": "ok"}
            payload = json.loads(body["messages"][1]["content"])
            return provider_response(payload)

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
            calls.append((endpoint.name, "probe" if "bundles" not in payload else "evidence"))
            if "bundles" not in payload:
                self.assertNotIn("ev-", json.dumps(body))
                return {"probe": "ok"}
            member = provider_members(payload)[0]
            response = provider_response(payload)
            if member["time_span"]["start"].startswith("2026-07-11"):
                response["activities"][0]["object"] = "Clockify review state"
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
            member = provider_members(payload)[0]
            day = int(member["time_span"]["start"][8:10])
            time.sleep(0.01 * (20 - day))
            with lock:
                active -= 1
            response = provider_response(payload)
            marker = member["time_span"]["start"]
            response["activities"][0].update({
                "object": f"Clockify item {marker}",
                "workstream": f"Clockify stream {marker}",
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

    def test_concurrent_extraction_refills_after_each_successful_chunk(self):
        slow_started = threading.Event()
        refilled_started = threading.Event()
        calls: list[int] = []
        lock = threading.Lock()

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            member = provider_members(payload)[0]
            day = int(member["time_span"]["start"][8:10])
            with lock:
                calls.append(day)
            if day == 10:
                self.assertTrue(slow_started.wait(timeout=1))
            elif day == 11:
                slow_started.set()
                self.assertTrue(
                    refilled_started.wait(timeout=1),
                    "a successful peer did not refill the available worker slot",
                )
            elif day == 12:
                refilled_started.set()
            else:
                self.fail(f"unexpected day {day}")
            response = provider_response(payload)
            response["activities"][0].update({
                "object": f"Clockify item {day}",
                "workstream": f"Clockify stream {day}",
            })
            return response

        result = semantic.analyze_tiered(
            [event(f"ev-{number}", f"2026-07-{10 + number:02d}") for number in range(3)],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
            max_events_per_chunk=1,
            max_workers=2,
        )

        self.assertTrue(refilled_started.is_set())
        self.assertEqual([10, 11, 12], sorted(calls))
        self.assertEqual([1, 2, 3], [item["chunk"] for item in result["analysis_chunks"]])

    def test_concurrent_extraction_never_exceeds_worker_limit_when_refilling(self):
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
            try:
                time.sleep(0.02)
            finally:
                with lock:
                    active -= 1
            member = provider_members(payload)[0]
            response = provider_response(payload)
            day = member["time_span"]["start"][8:10]
            response["activities"][0].update({
                "object": f"Clockify item {day}",
                "workstream": f"Clockify stream {day}",
            })
            return response

        semantic.analyze_tiered(
            [event(f"ev-{number}", f"2026-07-{10 + number:02d}") for number in range(6)],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
            max_events_per_chunk=1,
            max_workers=2,
        )

        self.assertLessEqual(max_active, 2)
        self.assertEqual(2, max_active)

    def test_concurrent_refill_keeps_ordered_cache_replay_identical(self):
        calls: list[int] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            member = provider_members(payload)[0]
            day = int(member["time_span"]["start"][8:10])
            calls.append(day)
            # Deliberately complete in reverse order. The result and replay
            # must stay in source chunk order rather than completion order.
            time.sleep(0.01 * (20 - day))
            response = provider_response(payload)
            response["activities"][0].update({
                "object": f"Clockify item {day}",
                "workstream": f"Clockify stream {day}",
            })
            return response

        events = [event(f"ev-{number}", f"2026-07-{10 + number:02d}") for number in range(4)]
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=transport,
                cache=semantic.AnalyzerResponseCache(path),
                max_events_per_chunk=1,
                max_workers=2,
            )
            second = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=lambda *_: self.fail("cache replay called transport"),
                cache=semantic.AnalyzerResponseCache(path),
                max_events_per_chunk=1,
                max_workers=2,
            )

        self.assertEqual([10, 11, 12, 13], sorted(calls))
        first_without_cache_summary = dict(first)
        second_without_cache_summary = dict(second)
        first_without_cache_summary.pop("analyzer_cache")
        second_without_cache_summary.pop("analyzer_cache")
        self.assertEqual(first_without_cache_summary, second_without_cache_summary)
        self.assertEqual(
            first["analyzer_cache"]["records"],
            second["analyzer_cache"]["records"],
        )
        self.assertEqual([1, 2, 3, 4], [item["chunk"] for item in first["analysis_chunks"]])

    def test_concurrent_extraction_probes_each_route_once(self):
        calls: list[tuple[str, str]] = []
        lock = threading.Lock()

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            with lock:
                calls.append((endpoint.name, "probe" if payload.get("probe") else "extract"))
            if payload.get("probe"):
                return {"probe": "ok"}
            member = provider_members(payload)[0]
            response = provider_response(payload)
            marker = member["time_span"]["start"]
            response["activities"][0].update({
                "object": f"Clockify item {marker}",
                "workstream": f"Clockify stream {marker}",
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
            member = provider_members(payload)[0]
            response = provider_response(payload)
            marker = member["time_span"]["start"]
            response["activities"][0].update({
                "object": f"Clockify item {marker}",
                "workstream": f"Clockify stream {marker}",
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
            event_payload = provider_members(payload)[0]
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
                return provider_response(payload)
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
            # The sealed primary rejection and its two bounded repair
            # rejections may be persisted. The in-flight peer and all queued
            # chunks cannot add cache records.
            self.assertEqual(3, len(cache_path.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(
            [
                ("fallback", 10),
                ("primary", 10),
                ("primary", 10),
                ("primary", 10),
                ("primary", 11),
            ],
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
                return provider_response(payload)
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

    def test_post_synthesis_semantics_receive_independent_flash_review(self):
        calls: list[str] = []
        taxonomy = [
            {
                "project_name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
                "billable": True,
            },
            {
                "project_name": "Internal Level 2",
                "prefix": "INT",
                "tag_names": ["Administration"],
                "billable": False,
            },
        ]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            mode = payload["mode"]
            calls.append(mode)
            if mode == "extract":
                return provider_response(payload)
            if mode == "synthesize":
                provisional = payload["provisional_activities"]
                response = valid_response(provisional[0]["evidence_ids"][0])
                response["activities"][0].update({
                    "action": "Copied",
                    "object": "raw transcript fragments",
                    "outcome": "into one timesheet line",
                    "evidence_ids": sorted(
                        evidence_id
                        for activity in provisional
                        for evidence_id in activity["evidence_ids"]
                    ),
                    "evidence_spans": [
                        span
                        for activity in provisional
                        for span in activity["evidence_spans"]
                    ],
                    "project_recommendation": {
                        "name": "Internal Level 2",
                        "prefix": "INT",
                        "tag_names": ["Administration"],
                    },
                    "merge_rationale": "same workstream across two days",
                })
                return response

            self.assertEqual("review", mode)
            members = provider_members(payload)
            response = provider_response(payload, members)
            if len(members) == 2:
                candidate_text = json.dumps(payload["candidate"], sort_keys=True)
                self.assertNotIn("ev-a", candidate_text)
                self.assertNotIn("ev-b", candidate_text)
                self.assertNotIn("activity_id", candidate_text)
                self.assertNotIn("evidence_ids", candidate_text)
                self.assertNotIn("evidence_partitions", candidate_text)
                self.assertEqual(
                    "Copied",
                    payload["candidate"]["activities"][0]["action"],
                )
                response["activities"][0].update({
                    "action": "Reconciled",
                    "object": "July work evidence",
                    "outcome": "produced one invoice-ready accomplishment",
                    "project_recommendation": {
                        "name": "Serenichron Level 2",
                        "prefix": "SC",
                        "tag_names": ["Processes"],
                    },
                    "merge_rationale": (
                        "same accomplishment supported across two days"
                    ),
                })
            else:
                response["activities"][0]["project_recommendation"] = {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                }
            return response

        result = semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=semantic.AnalyzerEndpoint(
                "primary", "http://primary", "flash-review-test"
            ),
            transport=transport,
            review_taxonomy=taxonomy,
            max_workers=1,
        )

        self.assertEqual(
            ["extract", "review", "extract", "review", "synthesize", "review"],
            calls,
        )
        self.assertEqual(1, len(result["activities"]))
        activity = result["activities"][0]
        self.assertEqual("Reconciled", activity["action"])
        self.assertEqual(
            "Serenichron Level 2",
            activity["project_recommendation"]["name"],
        )
        self.assertEqual(
            "primary_post_synthesis_flash_review",
            activity["analyzer_tier"],
        )
        self.assertEqual(
            semantic.REVIEW_PROMPT_VERSION,
            activity["review_prompt_version"],
        )

    def test_generic_workstream_with_different_objects_skips_synthesis(self):
        calls: list[str] = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            calls.append(payload.get("mode", "probe"))
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "synthesize":
                self.fail("different concrete objects must not become a synthesis candidate")
            evidence = provider_members(payload)[0]
            response = provider_response(payload)
            response["activities"][0].update({
                "workstream": "General maintenance",
                "object": (
                    "Clockify review identifiers"
                    if evidence["time_span"]["start"].startswith("2026-07-10")
                    else "Fathom meeting routing"
                ),
            })
            return response

        result = semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
            max_events_per_chunk=1,
        )

        self.assertNotIn("synthesize", calls)
        self.assertEqual(2, len(result["activities"]))
        # Allocation still sees one parent workstream; only synthesis uses the
        # internal concrete-object candidate key.
        self.assertEqual(1, len({activity["workstream_id"] for activity in result["activities"]}))

    def test_cross_chunk_distinct_outcomes_remain_split_with_specific_objects(self):
        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                return provider_response(payload)
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
                return provider_response(payload)
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
                ("primary", "synthesize"), ("primary", "synthesize"),
                ("fallback", "probe"), ("fallback", "synthesize"),
            ],
            calls,
        )
        self.assertEqual("fallback", result["activities"][0]["analyzer_tier"])
        self.assertEqual("strong", result["activities"][0]["analyzer_model"])

    def test_synthesis_contract_rejection_uses_one_accepted_repair(self):
        synthesis_requests: list[dict] = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                return provider_response(payload)
            synthesis_requests.append(payload)
            provisional = payload["provisional_activities"]
            response = valid_response(provisional[0]["evidence_ids"][0])
            if "repair_feedback" in payload:
                response["activities"][0].update({
                    "evidence_ids": sorted(
                        evidence_id
                        for activity in provisional
                        for evidence_id in activity["evidence_ids"]
                    ),
                    "evidence_spans": [
                        span for activity in provisional for span in activity["evidence_spans"]
                    ],
                    "merge_rationale": "same atomic description repair",
                })
            else:
                response["activities"][0]["evidence_spans"] = provisional[0]["evidence_spans"]
            return response

        result = semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            transport=transport,
        )

        self.assertEqual(2, len(synthesis_requests))
        self.assertEqual(
            "contract_rejected_omitted_evidence",
            synthesis_requests[1]["repair_feedback"]["failure_code"],
        )
        self.assertEqual(1, len(result["activities"]))
        self.assertEqual("primary", result["activities"][0]["analyzer_tier"])

    def test_double_synthesis_failure_becomes_bounded_exception(self):
        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                return provider_response(payload)
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

    def test_single_route_synthesis_rejection_becomes_bounded_exception(self):
        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                return provider_response(payload)
            return {"choices": [{"message": {"content": "[]"}}]}

        result = semantic.analyze_tiered(
            [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "qualified"),
            transport=transport,
        )

        self.assertEqual([], result["activities"])
        self.assertEqual(1, len(result["exceptions"]))
        exception = result["exceptions"][0]
        self.assertEqual("analyzer_synthesis_failure", exception["kind"])
        self.assertEqual(["ev-a", "ev-b"], exception["evidence_ids"])
        self.assertEqual("qualified", exception["primary_model"])
        self.assertNotIn("fallback_model", exception)
        self.assertRegex(exception["failure_digest"], r"^aer-[0-9a-f]{24}$")

    def test_single_route_synthesis_timeout_becomes_replayable_exception(self):
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                calls.append("probe")
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                calls.append("extract")
                return provider_response(payload)
            recovery = payload.get("transport_recovery")
            calls.append(
                f"synthesis_recovery_{recovery['attempt']}"
                if recovery
                else "synthesize"
            )
            raise semantic.AnalyzerTimeoutError("synthesis timed out")

        endpoint = semantic.AnalyzerEndpoint(
            "primary", "http://primary", "qualified"
        )
        events = [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=transport,
                cache=semantic.AnalyzerResponseCache(path),
            )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            second = semantic.analyze_tiered(
                events,
                primary=endpoint,
                transport=lambda *_: self.fail(
                    "sealed synthesis timeout replay must not call transport"
                ),
                cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertEqual([], first["activities"])
        self.assertEqual(first["exceptions"], second["exceptions"])
        exception = first["exceptions"][0]
        self.assertEqual(
            "analyzer_synthesis_failure", exception["kind"]
        )
        self.assertEqual(
            1 + semantic.MAX_CONNECTION_RECOVERY_ATTEMPTS,
            sum(
                record.get("failure_code") == "transport_timeout"
                for record in records
            ),
        )
        self.assertEqual(
            [
                "probe",
                "extract",
                "extract",
                "synthesize",
                "probe",
                "synthesis_recovery_1",
                "probe",
                "synthesis_recovery_2",
                "probe",
                "synthesis_recovery_3",
            ],
            calls,
        )
        self.assertEqual("qualified", exception["primary_model"])
        self.assertNotIn("fallback_model", exception)
        self.assertRegex(exception["failure_digest"], r"^aer-[0-9a-f]{24}$")

    def test_single_route_synthesis_outage_still_blocks(self):
        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "extract":
                return provider_response(payload)
            raise semantic.AnalyzerError("route unavailable")

        with self.assertRaisesRegex(
            semantic.AnalyzerError, "primary analyzer failed for synthesis"
        ):
            semantic.analyze_tiered(
                [event("ev-a", "2026-07-10"), event("ev-b", "2026-07-11")],
                primary=semantic.AnalyzerEndpoint(
                    "primary", "http://primary", "qualified"
                ),
                transport=transport,
            )

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
                return provider_response(payload)
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
                    return provider_response(payload)
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
                return provider_response(payload)
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
        self.assertIn("bundles", payload)
        self.assertNotIn("events", payload)
        members = provider_members(payload)
        safe = next(item for item in members if item["role"] == "assistant")
        tool_projection = next(item for item in members if item["role"] == "tool")
        self.assertEqual(2, len(members))
        self.assertTrue(all(item["member"] == 1 for item in members))
        self.assertEqual(2, len({item["bundle_ref"] for item in members}))
        self.assertNotIn("ev-safe", json.dumps(payload))
        self.assertNotIn("ev-tool", json.dumps(payload))
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

    def test_rejects_secondary_action_hidden_in_outcome(self):
        response = valid_response("ev-1")
        response["activities"][0]["outcome"] = (
            "verified SER work and created coordination agent"
        )
        with self.assertRaisesRegex(semantic.AnalyzerError, "multiple accomplishment clauses"):
            semantic.validate_result(
                response,
                known_evidence_ids={"ev-1"},
                provider_model="model",
                analyzer_tier="primary",
            )

    def test_rejects_list_like_and_rationalized_compound_accomplishments(self):
        variants = [
            (
                "comma results",
                {"outcome": "endpoint disabled, credentials sanitized, pipeline hardened"},
                "multiple accomplishment clauses",
            ),
            (
                "parallel passive results",
                {"outcome": "root cause identified and report written"},
                "multiple accomplishment clauses",
            ),
            (
                "second outcome action",
                {"outcome": "scheduled client follow-up for Friday"},
                "second accomplishment action",
            ),
            (
                "same-message excuse",
                {
                    "split_rationale": (
                        "The assistant delivered both transcripts and analysis in one response."
                    )
                },
                "multiple accomplishment clauses",
            ),
        ]
        for label, changes, message in variants:
            response = valid_response("ev-1")
            response["activities"][0].update(changes)
            with self.subTest(label=label), self.assertRaisesRegex(
                semantic.AnalyzerError, message
            ):
                semantic.validate_result(
                    response,
                    known_evidence_ids={"ev-1"},
                    provider_model="model",
                    analyzer_tier="primary",
                )

    def test_rejects_user_request_or_assistant_status_as_human_accomplishment(self):
        for role in ("user", "assistant"):
            source = event(f"ev-{role}")
            source.update({
                "role": role,
                "source_ref": {"session_id": "session-one", "machine": "precision"},
            })
            response = valid_response(f"ev-{role}")
            with self.subTest(role=role), self.assertRaisesRegex(
                semantic.AnalyzerError, "human accomplishment support"
            ):
                semantic.validate_result(
                    response,
                    known_evidence_ids={f"ev-{role}"},
                    provider_model="model",
                    analyzer_tier="primary",
                    evidence_support=semantic._evidence_support([source]),
                )

    def test_accepts_paired_user_intent_and_assistant_result_from_same_session(self):
        user = event("ev-user")
        assistant = event("ev-assistant")
        for source, role in ((user, "user"), (assistant, "assistant")):
            source.update({
                "role": role,
                "source_ref": {"session_id": "session-one", "machine": "precision"},
            })
        response = valid_response("ev-user")
        response["activities"][0]["evidence_ids"] = ["ev-user", "ev-assistant"]
        result = semantic.validate_result(
            response,
            known_evidence_ids={"ev-user", "ev-assistant"},
            provider_model="model",
            analyzer_tier="primary",
            evidence_support=semantic._evidence_support([user, assistant]),
        )
        self.assertEqual(
            ["ev-assistant", "ev-user"], result["activities"][0]["evidence_ids"]
        )

    def test_turn_aware_recovery_keeps_each_user_with_following_result(self):
        events = []
        for ordinal, role in enumerate(("user", "assistant", "user", "assistant"), 1):
            source = event(f"ev-{ordinal}")
            source.update({
                "role": role,
                "source_ref": {
                    "session_id": "session-one",
                    "machine": "precision",
                    "ordinal": ordinal,
                },
            })
            events.append(source)
        split = semantic._turn_aware_recovery_split(events)
        self.assertIsNotNone(split)
        assert split is not None
        left, right, split_at = split
        self.assertEqual(2, split_at)
        self.assertEqual(["ev-1", "ev-2"], [item["evidence_id"] for item in left])
        self.assertEqual(["ev-3", "ev-4"], [item["evidence_id"] for item in right])

    def test_operational_chunking_never_splits_conversation_turns(self):
        events = []
        for ordinal, role in enumerate(("user", "assistant", "user", "assistant"), 1):
            source = event(f"ev-{ordinal}")
            source.update({
                "role": role,
                "source_ref": {
                    "session_id": "session-one",
                    "machine": "precision",
                    "ordinal": ordinal,
                },
            })
            events.append(source)
        chunks = semantic.chunk_events(events, max_events_per_chunk=1)
        self.assertEqual(
            [["ev-1", "ev-2"], ["ev-3", "ev-4"]],
            [[item["evidence_id"] for item in chunk] for chunk in chunks],
        )

    def test_turn_aware_recovery_will_not_split_one_indivisible_turn(self):
        events = []
        for ordinal, role in enumerate(("user", "assistant", "tool"), 1):
            source = event(f"ev-{ordinal}")
            source.update({
                "role": role,
                "source_ref": {"session_id": "session-one", "ordinal": ordinal},
            })
            events.append(source)
        self.assertIsNone(semantic._turn_aware_recovery_split(events))

    def test_dual_rejection_recovery_never_orphans_results_from_user_turns(self):
        child_roles: list[list[str]] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "synthesize":
                provisional = payload["provisional_activities"]
                response = valid_response(provisional[0]["evidence_ids"][0])
                response["activities"][0].update({
                    "evidence_ids": sorted(
                        evidence_id
                        for activity in provisional
                        for evidence_id in activity["evidence_ids"]
                    ),
                    "evidence_spans": [
                        span
                        for activity in provisional
                        for span in activity["evidence_spans"]
                    ],
                    "merge_rationale": "same accomplishment across recovered turns",
                })
                return response
            members = provider_members(payload)
            if len(members) > 2:
                return {"activities": [], "exceptions": [], "omissions": []}
            child_roles.append([member["role"] for member in members])
            return provider_response(payload)

        events = []
        for ordinal, role in enumerate(("user", "assistant", "user", "assistant"), 1):
            source = event(f"ev-{ordinal}")
            source.update({
                "role": role,
                "source_ref": {
                    "session_id": "session-one",
                    "machine": "precision",
                    "ordinal": ordinal,
                },
            })
            events.append(source)
        result = semantic.analyze_tiered(
            events,
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
            max_events_per_chunk=4,
        )
        self.assertEqual([], result["exceptions"])
        self.assertEqual([["user", "assistant"], ["user", "assistant"]], child_roles)
        self.assertEqual("recovered_by_partition", result["analysis_chunks"][0]["recovery_status"])

    def test_dual_rejection_of_indivisible_turn_becomes_safe_exception(self):
        extract_sizes: list[int] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            extract_sizes.append(len(provider_members(payload)))
            return {"activities": [], "exceptions": [], "omissions": []}

        events = []
        for ordinal, role in enumerate(("user", "assistant", "tool"), 1):
            source = event(f"ev-{ordinal}")
            source.update({
                "role": role,
                "source_ref": {"session_id": "session-one", "ordinal": ordinal},
            })
            events.append(source)
        result = semantic.analyze_tiered(
            events,
            primary=semantic.AnalyzerEndpoint("primary", "http://primary", "cheap"),
            fallback=semantic.AnalyzerEndpoint("fallback", "http://fallback", "strong"),
            transport=transport,
            max_events_per_chunk=3,
        )
        self.assertTrue(extract_sizes)
        self.assertEqual({3}, set(extract_sizes))
        self.assertEqual("analyzer_failure", result["exceptions"][0]["kind"])
        self.assertEqual(
            ["ev-1", "ev-2", "ev-3"], result["exceptions"][0]["evidence_ids"]
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
            return provider_response(payload)

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

    def test_repair_cache_identity_is_distinct_and_replays_without_transport(self):
        calls: list[dict] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            calls.append(payload)
            if payload.get("probe"):
                return {"probe": "ok"}
            response = provider_response(payload)
            if "repair_feedback" not in payload:
                response["activities"][0]["action"] = "Fixed and published"
            return response

        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analyzer-cache.jsonl"
            first = semantic.analyze_tiered(
                [event("ev-1")], primary=endpoint, transport=transport,
                cache=semantic.AnalyzerResponseCache(path),
            )
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            second = semantic.analyze_tiered(
                [event("ev-1")], primary=endpoint,
                transport=lambda *_: self.fail("repair cache replay must not call transport"),
                cache=semantic.AnalyzerResponseCache(path),
            )

        self.assertEqual(first["activities"], second["activities"])
        self.assertEqual("used", second["analysis_chunks"][0]["repair_status"])
        self.assertEqual(2, len(records))
        self.assertEqual(2, len({record["body_digest"] for record in records}))
        self.assertEqual("rejected", records[0]["status"])
        self.assertEqual("accepted", records[1]["status"])

    def test_contract_failure_codes_cover_common_validation_errors(self):
        self.assertEqual(
            "contract_rejected_compound_action",
            semantic._contract_failure_code(
                semantic.AnalyzerError("activity action must express one atomic verb phrase")
            ),
        )
        self.assertEqual(
            "contract_rejected_omitted_evidence",
            semantic._contract_failure_code(
                semantic.AnalyzerError("semantic result omitted known evidence IDs")
            ),
        )
        self.assertEqual(
            "contract_rejected_invalid_evidence_ids",
            semantic._contract_failure_code(
                semantic.AnalyzerError("activity contains missing or unknown evidence IDs")
            ),
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

    def test_response_cache_identity_binds_model_revision(self):
        body = {"model": semantic.DEFAULT_PRIMARY_MODEL, "messages": []}
        first = semantic.AnalyzerEndpoint(
            "primary", "http://primary", semantic.DEFAULT_PRIMARY_MODEL,
            revision="a" * 64,
        )
        second = semantic.AnalyzerEndpoint(
            "primary", "http://primary", semantic.DEFAULT_PRIMARY_MODEL,
            revision="b" * 64,
        )

        self.assertNotEqual(
            semantic.AnalyzerResponseCache._request_identity(first, body)["cache_key"],
            semantic.AnalyzerResponseCache._request_identity(second, body)["cache_key"],
        )

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
            if endpoint.name == "primary":
                return {"activities": [], "exceptions": [], "omissions": []}
            return provider_response(payload)

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
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
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
                ("primary", "evidence"),
                ("primary", "evidence"),
                ("fallback", "probe"),
                ("fallback", "evidence"),
            ],
            calls,
        )

        rejection = next(record for record in records if record["status"] == "rejected")
        self.assertEqual("contract_rejected_omitted_evidence", rejection["failure_code"])

    def test_cache_rejects_unsafe_failure_code(self):
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "cheap")
        body = semantic._body_for(
            [event("ev-1")], model="cheap", mode="extract", private_text_approved=True
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl")
            with self.assertRaisesRegex(semantic.AnalyzerError, "rejection code is invalid"):
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code="private model text",
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

    def test_provider_body_contains_only_opaque_bundles(self):
        events = [event("ev-local-a"), event("ev-local-b")]
        for item in events:
            item["source_ref"] = {
                "source_type": "codex_sessions",
                "machine": "private-machine-id",
                "session_id": "private-session-id",
            }
        body = semantic._body_for(
            events, model="fixture", mode="extract", private_text_approved=True
        )
        payload = json.loads(body["messages"][-1]["content"])
        outbound = semantic.canonical_json(payload)
        self.assertEqual(semantic.EVIDENCE_BUNDLE_SCHEMA_VERSION, payload["evidence_bundle_schema_version"])
        self.assertIn("bundles", payload)
        self.assertNotIn("events", payload)
        for forbidden in ("ev-local-a", "ev-local-b", "private-machine-id", "private-session-id"):
            self.assertNotIn(forbidden, outbound)
        self.assertEqual({"b-0001"}, {item["bundle_ref"] for item in payload["bundles"]})

    def test_bundle_manifest_is_content_addressed_and_stable_under_input_permutation(self):
        events = [event("ev-z", content="z work"), event("ev-a", content="a work")]
        for item in events:
            item["source_ref"] = {
                "source_type": "codex_sessions", "machine": "host", "session_id": "same"
            }
        _, first = semantic._semantic_evidence_bundles(events)
        _, second = semantic._semantic_evidence_bundles(reversed(events))
        self.assertEqual(first, second)
        self.assertTrue(all(item["bundle_id"].startswith("seb-") for item in first))

    def test_partition_split_and_whole_range_expand_to_original_evidence_ids(self):
        events = [event("ev-a"), event("ev-b"), event("ev-c")]
        for item in events:
            item["source_ref"] = {
                "source_type": "codex_sessions", "machine": "host", "session_id": "same"
            }
        payload = json.loads(semantic._body_for(
            events, model="fixture", mode="extract", private_text_approved=True
        )["messages"][-1]["content"])
        members = provider_members(payload)
        split_left = provider_response(payload, members[:1])
        split_right = provider_response(payload, members[1:])
        restored_split = semantic._restore_extraction_partitions(
            {
                "activities": split_left["activities"] + split_right["activities"],
                "exceptions": [],
                "omissions": [],
            },
            events=events,
        )
        self.assertEqual([["ev-a"], ["ev-b", "ev-c"]], [
            item["evidence_ids"] for item in restored_split["activities"]
        ])
        restored_whole = semantic._restore_extraction_partitions(
            provider_response(payload), events=events
        )
        self.assertEqual(["ev-a", "ev-b", "ev-c"], restored_whole["activities"][0]["evidence_ids"])

    def test_invalid_provider_partitions_fail_closed(self):
        events = [event("ev-a"), event("ev-b")]
        for item in events:
            item["source_ref"] = {
                "source_type": "codex_sessions", "machine": "host", "session_id": "same"
            }
        payload = json.loads(semantic._body_for(
            events, model="fixture", mode="extract", private_text_approved=True
        )["messages"][-1]["content"])
        base = provider_response(payload)
        activity = base["activities"][0]
        variants = [
            ("unknown", [{"bundle_ref": "b-9999", "member_ranges": [[1, 1]]}]),
            ("repeated bundle ref", [
                {"bundle_ref": "b-0001", "member_ranges": [[1, 1]]},
                {"bundle_ref": "b-0001", "member_ranges": [[2, 2]]},
            ]),
            ("ranges overlap", [{"bundle_ref": "b-0001", "member_ranges": [[1, 2], [2, 2]]}]),
            ("reversed or out of bounds", [{"bundle_ref": "b-0001", "member_ranges": [[2, 1]]}]),
            ("reversed or out of bounds", [{"bundle_ref": "b-0001", "member_ranges": [[1, 3]]}]),
        ]
        for message, partitions in variants:
            candidate = json.loads(json.dumps(base))
            candidate["activities"][0]["evidence_partitions"] = partitions
            with self.subTest(message=message), self.assertRaisesRegex(semantic.AnalyzerError, message):
                semantic._restore_extraction_partitions(candidate, events=events)

        missing = provider_response(payload, provider_members(payload)[:1])
        restored = semantic._restore_extraction_partitions(missing, events=events)
        with self.assertRaisesRegex(semantic.AnalyzerError, "omitted known evidence"):
            semantic.validate_result(
                restored, known_evidence_ids={"ev-a", "ev-b"},
                provider_model="fixture", analyzer_tier="primary",
            )

    def test_accepted_extraction_cache_never_contains_local_ids_or_private_prose(self):
        local_id = "ev-private-local"
        private = "PRIVATE_EXACT_TEXT"
        item = event(local_id, content=private)
        endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "fixture")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            semantic.analyze_tiered(
                [item], primary=endpoint, private_text_approved=True,
                cache=semantic.AnalyzerResponseCache(path),
                transport=lambda _endpoint, body: (
                    {"probe": "ok"}
                    if json.loads(body["messages"][-1]["content"]).get("probe")
                    else provider_response(json.loads(body["messages"][-1]["content"]))
                ),
            )
            cached = path.read_text(encoding="utf-8")
        self.assertNotIn(local_id, cached)
        self.assertNotIn(private, cached)
        self.assertNotIn("evidence_ids", cached)
        self.assertIn("evidence_partitions", cached)

    def test_oversized_stable_context_splits_without_evidence_loss(self):
        events = [event(f"ev-{index:02d}") for index in range(7)]
        for item in events:
            item["source_ref"] = {
                "source_type": "codex_sessions", "machine": "host", "session_id": "same"
            }
        chunks = semantic.chunk_events(
            events, max_body_bytes=50_000, max_events_per_chunk=2
        )
        self.assertEqual([2, 2, 2, 1], [len(chunk) for chunk in chunks])
        self.assertEqual(
            sorted(item["evidence_id"] for item in events),
            sorted(item["evidence_id"] for chunk in chunks for item in chunk),
        )

    def test_unchanged_bundle_request_and_manifest_are_deterministic(self):
        events = [event("ev-a", content="same"), event("ev-b", content="same")]
        for item in events:
            item["source_ref"] = {
                "source_type": "codex_sessions", "machine": "host", "session_id": "same"
            }
        first_body = semantic._body_for(
            events, model="fixture", mode="extract", private_text_approved=True
        )
        second_body = semantic._body_for(
            events, model="fixture", mode="extract", private_text_approved=True
        )
        _, first_manifest = semantic._semantic_evidence_bundles(events)
        _, second_manifest = semantic._semantic_evidence_bundles(events)
        self.assertEqual(semantic.canonical_json(first_body), semantic.canonical_json(second_body))
        self.assertEqual(first_manifest, second_manifest)


if __name__ == "__main__":
    unittest.main()
