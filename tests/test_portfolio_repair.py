import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from scripts import semantic_analyzer as semantic


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_portfolio_repair.py"
SPEC = importlib.util.spec_from_file_location("clockify_portfolio_repair", SCRIPT)
repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(repair)


def source_document(description: str) -> dict:
    return {
        "schema_version": 1,
        "model": semantic.DEFAULT_PRIMARY_MODEL,
        "revision": repair.APPROVED_FLASH_REVISION,
        "activities": [{
            "review_id": "pvi-one",
            "activity_id": "act-one",
            "source_activity_ids": ["act-source"],
            "evidence_ids": ["ev-one"],
            "allocation_segments": [{
                "start": "2026-07-10T09:00+03:00",
                "end": "2026-07-10T09:30+03:00",
                "duration_minutes": 30,
            }],
            "start": "2026-07-10T09:00+03:00",
            "end": "2026-07-10T09:30+03:00",
            "duration_minutes": 30,
            "client_project": "Serenichron Level 2",
            "tag_names": ["Processes"],
            "confidence": "high",
            "description": description,
            "disposition": "pending",
            "review_prompt_version": semantic.PORTFOLIO_REVIEW_PROMPT_VERSION,
            "semantic_reviewer_model": semantic.DEFAULT_PRIMARY_MODEL,
            "semantic_reviewer_revision": repair.APPROVED_FLASH_REVISION,
            "validation_status": "flash_validated",
        }],
        "exceptions": [],
        "omissions": [],
    }


def ledger() -> dict:
    return {"events": [{
        "evidence_id": "ev-one",
        "source_type": "codex",
        "observed_start": "2026-07-10T09:00+03:00",
        "observed_end": "2026-07-10T09:30+03:00",
        "content": "Reviewed July portfolio entries and prepared invoice-ready wording.",
    }]}


def partition_response(payload: dict) -> dict:
    members = [
        {"bundle_ref": bundle["bundle_ref"], **member}
        for bundle in payload["bundles"]
        for member in bundle["members"]
    ]
    grouped: dict[str, list[int]] = {}
    for member in members:
        grouped.setdefault(member["bundle_ref"], []).append(member["member"])
    route = payload["candidate"]["activities"][0]["project_recommendation"]
    return {
        "activities": [{
            "lifecycle": "completed",
            "action": "Rebuilt",
            "object": "July portfolio descriptions",
            "outcome": "for invoice-ready client review",
            "workstream": "portfolio wording repair",
            "evidence_partitions": [
                {"bundle_ref": name, "member_ranges": [[min(values), max(values)]]}
                for name, values in grouped.items()
            ],
            "evidence_spans": [member["time_span"] for member in members],
            "project_recommendation": {
                "name": route["name"],
                "prefix": route["prefix"],
                "tag_names": route["tag_names"],
            },
            "effort": {
                "minimum_minutes": 30,
                "recommended_minutes": 30,
                "maximum_minutes": 30,
            },
            "semantic_confidence": "high",
            "timing_confidence": "medium",
            "split_rationale": "one bounded outcome",
            "merge_rationale": "",
            "omit_rationale": "",
        }],
        "exceptions": [],
        "omissions": [],
    }


def routing() -> dict:
    return {"session_routes": [
        {
            "pattern": "clockify",
            "project_name": "Serenichron Level 2",
            "prefix": "SC",
            "tag_names": ["Processes"],
            "billable": True,
        },
        {
            "pattern": "tstprep",
            "project_name": "TST Prep Level 2",
            "prefix": "TSTP",
            "tag_names": ["Technical development"],
            "billable": True,
        },
    ], "meeting_routes": []}


def proposals(*routes: tuple[str, list[str]]) -> list[dict]:
    return [
        {
            "activity_id": f"act-source{suffix}",
            "client_project": project,
            "tag_names": tags,
        }
        for suffix, (project, tags) in enumerate(routes, 1)
    ]


@mock.patch.dict(
    "os.environ", {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}, clear=False
)
class PortfolioRepairTests(unittest.TestCase):
    def endpoint(self) -> semantic.AnalyzerEndpoint:
        return semantic.AnalyzerEndpoint(
            "primary", "http://flash", semantic.DEFAULT_PRIMARY_MODEL,
            revision=repair.APPROVED_FLASH_REVISION,
        )

    def test_repair_reasons_detect_forbidden_content_and_target_length(self):
        reasons = repair.repair_reasons(
            "SC — NEEDS REVIEW https://example.com /Users/private with many copied unnecessary status details for no useful reason across several unrelated client workstreams and meetings"
        )

        self.assertTrue(any("8-24" in reason for reason in reasons))
        self.assertTrue(any("forbidden" in reason for reason in reasons))

    def test_clean_no_repair_result_declares_no_unresolved_wording(self):
        source = source_document(
            "SC — Rebuilt Clockify review into invoice-ready July entries"
        )

        with tempfile.TemporaryDirectory() as directory:
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=None,
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
            )

        self.assertEqual("pass", result["repair"]["status"])
        self.assertEqual([], result["repair"]["unresolved_wording"])

    def test_carried_row_is_revalidated_even_when_wording_is_already_clean(self):
        source = source_document(
            "SC — Rebuilt Clockify review into invoice-ready July entries"
        )
        source["activities"][0]["validation_status"] = (
            "source_semantic_review_carried_after_flash_contract_failure"
        )
        stages: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            stages.append(payload["review_scope"])
            return partition_response(payload)

        with tempfile.TemporaryDirectory() as directory:
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=transport,
            )

        self.assertEqual(["portfolio", "portfolio_validation"], stages)
        self.assertEqual(
            "flash_validated", result["activities"][0]["validation_status"]
        )
        self.assertEqual(["pvi-one"], result["repair"]["repaired_review_ids"])

    def test_flash_repair_runs_structured_and_separate_validation_and_preserves_rows(self):
        source = source_document(
            "SC — Rebuilt Clockify review into a complete invoice-ready July package with too many secondary details across every client workstream and meeting across all three machines"
        )
        before = copy.deepcopy(source["activities"][0])
        stages: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            stages.append(payload["review_scope"])
            return partition_response(payload)

        with tempfile.TemporaryDirectory() as directory:
            cache = semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl")
            result = repair.repair_document(
                source, ledger(), endpoint=self.endpoint(), cache=cache, transport=transport
            )

        after = result["activities"][0]
        self.assertEqual(["portfolio", "portfolio_validation"], stages)
        self.assertEqual("SC — Rebuilt July portfolio descriptions for invoice-ready client review", after["description"])
        self.assertGreaterEqual(len(after["description"].split()), 8)
        self.assertLessEqual(len(after["description"].split()), 14)
        self.assertEqual(
            {key: value for key, value in before.items() if key != "description"},
            {key: value for key, value in after.items() if key != "description"},
        )

    def test_contract_failed_review_switches_to_bounded_wording_decision(self):
        source = source_document("SC — Reviewed NEEDS REVIEW status for July package")
        analyzer_failure = {
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": ["ev-one"],
                "reason": "Flash reviewer exhausted one structural repair",
            }],
            "omissions": [],
        }
        stages: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            self.assertEqual("portfolio_wording_decision", payload["mode"])
            stages.append(payload["decision_stage"])
            decision = {
                "disposition": "activity",
                "action": "Prepared",
                "object": "July portfolio review package",
                "outcome": "for invoice-ready client approval",
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
                "exception_kind": "",
                "reason": "",
            }
            return {"choices": [{"message": {"content": json.dumps(decision)}}]}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic, "_call_semantic_review", return_value=analyzer_failure
        ) as full_review:
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=transport,
            )

        self.assertEqual(["draft", "validation"], stages)
        self.assertEqual(1, full_review.call_count)
        self.assertEqual(
            "SC — Prepared July portfolio review package for invoice-ready client approval",
            result["activities"][0]["description"],
        )
        self.assertEqual(["ev-one"], result["activities"][0]["evidence_ids"])
        self.assertEqual(["pvi-one"], result["repair"]["repaired_review_ids"])

    def test_wording_decision_repairs_one_malformed_provider_envelope(self):
        source = source_document("SC — Reviewed NEEDS REVIEW status for July package")
        analyzer_failure = {
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": ["ev-one"],
                "reason": "Flash reviewer exhausted one structural repair",
            }],
            "omissions": [],
        }
        stages: list[str] = []
        valid = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "July portfolio review package",
            "outcome": "for invoice-ready client approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "",
        }

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            stages.append(payload["decision_stage"])
            if len(stages) == 1:
                return {"choices": []}
            return {"choices": [{"message": {"content": json.dumps(valid)}}]}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic, "_call_semantic_review", return_value=analyzer_failure
        ):
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=transport,
            )

        self.assertEqual(["draft", "draft", "validation"], stages)
        self.assertEqual(
            "SC — Prepared July portfolio review package for invoice-ready client approval",
            result["activities"][0]["description"],
        )

    def test_wording_decision_retry_receives_specific_contract_feedback(self):
        source = source_document("SC — Reviewed NEEDS REVIEW status for July package")
        analyzer_failure = {
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": ["ev-one"],
                "reason": "Flash reviewer exhausted one structural repair",
            }],
            "omissions": [],
        }
        valid = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "July portfolio review package",
            "outcome": "for invoice-ready client approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "",
        }
        repair_feedback: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            feedback = payload.get("repair_feedback")
            if feedback is None:
                malformed = (
                    {**valid, "confidence": "high"}
                    if payload["decision_stage"] == "draft"
                    else {**valid, "disposition": "completed"}
                )
                return {"choices": [{"message": {"content": json.dumps(malformed)}}]}
            repair_feedback.append(feedback["failure_code"])
            return {"choices": [{"message": {"content": json.dumps(valid)}}]}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic, "_call_semantic_review", return_value=analyzer_failure
        ):
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=transport,
            )

        self.assertEqual(
            ["unsupported_fields", "contract_rejected_invalid_lifecycle"],
            repair_feedback,
        )
        self.assertEqual(
            "SC — Prepared July portfolio review package for invoice-ready client approval",
            result["activities"][0]["description"],
        )

    def test_cached_generic_rejection_uses_distinct_structural_repair_identity(self):
        valid = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "July portfolio review package",
            "outcome": "for invoice-ready client approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "",
        }
        feedback: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            feedback.append(payload["repair_feedback"]["failure_code"])
            return {"choices": [{"message": {"content": json.dumps(valid)}}]}

        with tempfile.TemporaryDirectory() as directory:
            cache = semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl")
            original = repair._wording_decision_body(
                ledger()["events"],
                candidate=source_document("ignored")["activities"][0],
                taxonomy=routing()["session_routes"],
                endpoint=self.endpoint(),
                stage="draft",
            )
            cache.store_rejected(
                self.endpoint(), original, failure_code="contract_rejected_other"
            )

            decision = repair._call_wording_decision(
                self.endpoint(),
                ledger()["events"],
                candidate=source_document("ignored")["activities"][0],
                taxonomy=routing()["session_routes"],
                stage="draft",
                cache=cache,
                transport=transport,
                probe_once=lambda _endpoint: None,
            )

        self.assertEqual(["cached_unknown_contract_shape"], feedback)
        self.assertEqual(valid, decision)

    def test_two_cached_generic_rejections_do_not_consume_live_repair_budget(self):
        valid = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "July portfolio review package",
            "outcome": "for invoice-ready client approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "",
        }
        feedback: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            feedback.append(payload["repair_feedback"]["failure_code"])
            return {"choices": [{"message": {"content": json.dumps(valid)}}]}

        with tempfile.TemporaryDirectory() as directory:
            cache = semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl")
            common = {
                "events": ledger()["events"],
                "candidate": source_document("ignored")["activities"][0],
                "taxonomy": routing()["session_routes"],
                "endpoint": self.endpoint(),
                "stage": "draft",
            }
            original = repair._wording_decision_body(**common)
            first_repair = repair._wording_decision_body(
                **common, repair_failure_code="cached_unknown_contract_shape"
            )
            cache.store_rejected(
                self.endpoint(), original, failure_code="contract_rejected_other"
            )
            cache.store_rejected(
                self.endpoint(), first_repair, failure_code="contract_rejected_other"
            )

            decision = repair._call_wording_decision(
                self.endpoint(),
                ledger()["events"],
                candidate=source_document("ignored")["activities"][0],
                taxonomy=routing()["session_routes"],
                stage="draft",
                cache=cache,
                transport=transport,
                probe_once=lambda _endpoint: None,
            )

        self.assertEqual(["cached_unknown_contract_shape_2"], feedback)
        self.assertEqual(valid, decision)

    def test_current_wording_contract_does_not_replay_legacy_rejections(self):
        valid = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "July portfolio review package",
            "outcome": "for invoice-ready client approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "",
        }
        for legacy_version in (
            "clockify-portfolio-wording-decision-v1",
            "clockify-portfolio-wording-decision-v2",
        ):
            with self.subTest(legacy_version=legacy_version):
                feedback_present: list[bool] = []

                def transport(_endpoint, body):
                    payload = json.loads(body["messages"][-1]["content"])
                    feedback_present.append("repair_feedback" in payload)
                    return {"choices": [{"message": {"content": json.dumps(valid)}}]}

                with tempfile.TemporaryDirectory() as directory:
                    cache = semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl")
                    with mock.patch.object(
                        repair, "WORDING_DECISION_PROMPT_VERSION", legacy_version
                    ):
                        legacy = repair._wording_decision_body(
                            ledger()["events"],
                            candidate=source_document("ignored")["activities"][0],
                            taxonomy=routing()["session_routes"],
                            endpoint=self.endpoint(),
                            stage="draft",
                        )
                    cache.store_rejected(
                        self.endpoint(), legacy, failure_code="contract_rejected_other"
                    )

                    decision = repair._call_wording_decision(
                        self.endpoint(),
                        ledger()["events"],
                        candidate=source_document("ignored")["activities"][0],
                        taxonomy=routing()["session_routes"],
                        stage="draft",
                        cache=cache,
                        transport=transport,
                        probe_once=lambda _endpoint: None,
                    )

                self.assertEqual([False], feedback_present)
                self.assertEqual(valid, decision)

    def test_current_wording_contract_reuses_valid_v1_decision(self):
        valid = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "July portfolio review package",
            "outcome": "for invoice-ready client approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "",
        }

        with tempfile.TemporaryDirectory() as directory:
            cache = semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl")
            with mock.patch.object(
                repair,
                "WORDING_DECISION_PROMPT_VERSION",
                "clockify-portfolio-wording-decision-v1",
            ):
                legacy = repair._wording_decision_body(
                    ledger()["events"],
                    candidate=source_document("ignored")["activities"][0],
                    taxonomy=routing()["session_routes"],
                    endpoint=self.endpoint(),
                    stage="draft",
                )
            cache.store_accepted(self.endpoint(), legacy, valid)

            decision = repair._call_wording_decision(
                self.endpoint(),
                ledger()["events"],
                candidate=source_document("ignored")["activities"][0],
                taxonomy=routing()["session_routes"],
                stage="draft",
                cache=cache,
                transport=lambda *_: self.fail("valid v1 decision must be reused"),
                probe_once=lambda _endpoint: None,
            )

        self.assertEqual(valid, decision)

    def test_activity_wording_decision_discards_unused_reason_prose(self):
        decision = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "July portfolio review package",
            "outcome": "for invoice-ready client approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "The evidence supports this completed activity.",
        }

        normalized = repair._validate_wording_decision(
            decision, taxonomy=routing()["session_routes"]
        )

        self.assertEqual("", normalized["reason"])

    def test_nonactivity_wording_decision_discards_unused_activity_fields(self):
        decision = {
            "disposition": "noise",
            "action": "Observed",
            "object": "agent status",
            "outcome": "while awaiting approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "Status-only evidence is not a completed accomplishment.",
        }

        normalized = repair._validate_wording_decision(
            decision, taxonomy=routing()["session_routes"]
        )

        self.assertEqual("", normalized["action"])
        self.assertEqual("", normalized["object"])
        self.assertEqual("", normalized["outcome"])
        self.assertEqual(
            {"name": "", "prefix": "", "tag_names": []},
            normalized["project_recommendation"],
        )

    def test_wording_decision_retries_caveman_unsafe_activity_fields(self):
        unsafe = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "portfolio commit 0123456789abcdef0123456789abcdef",
            "outcome": "for client approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "",
        }
        valid = {
            **unsafe,
            "object": "July portfolio review package",
            "outcome": "for invoice-ready client approval",
        }
        feedback: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            repair_feedback = payload.get("repair_feedback")
            if repair_feedback is None:
                response = unsafe
            else:
                feedback.append(repair_feedback["failure_code"])
                response = (
                    {key: valid[key] for key in ("action", "object", "outcome")}
                    if payload["mode"] == "portfolio_wording_fields_repair"
                    else valid
                )
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        with tempfile.TemporaryDirectory() as directory:
            decision = repair._call_wording_decision(
                self.endpoint(),
                ledger()["events"],
                candidate=source_document("ignored")["activities"][0],
                taxonomy=routing()["session_routes"],
                stage="draft",
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=transport,
                probe_once=lambda _endpoint: None,
            )

        self.assertEqual(["forbidden_hash"], feedback)
        self.assertEqual(valid, decision)

    def test_caveman_retry_feedback_names_the_rejected_wording_class(self):
        error = semantic.AnalyzerContractError(
            "wording decision violates Caveman contract: description contains forbidden hash"
        )

        failure_code = repair._wording_failure_code(error)
        request = repair._wording_decision_body(
            ledger()["events"],
            candidate=source_document("ignored")["activities"][0],
            taxonomy=routing()["session_routes"],
            endpoint=self.endpoint(),
            stage="draft",
            repair_failure_code=failure_code,
        )
        payload = json.loads(request["messages"][-1]["content"])

        self.assertEqual("forbidden_hash", failure_code)
        self.assertIn("commit hashes", payload["repair_feedback"]["instruction"])

    def test_caveman_rejection_uses_locked_wording_fields_repair(self):
        unsafe = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "portfolio commit 0123456789abcdef0123456789abcdef",
            "outcome": "for client approval",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
            "exception_kind": "",
            "reason": "",
        }
        valid_full = {
            **unsafe,
            "object": "July portfolio review package",
            "outcome": "for invoice-ready client approval",
        }
        modes: list[str] = []
        micro_payloads: list[dict] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            modes.append(payload["mode"])
            if len(modes) == 1:
                response = unsafe
            elif payload["mode"] == "portfolio_wording_fields_repair":
                micro_payloads.append(payload)
                response = {
                    "action": "prepared",
                    "object": "July portfolio review package",
                    "outcome": "for invoice-ready client approval",
                }
            else:
                response = valid_full
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        with tempfile.TemporaryDirectory() as directory:
            decision = repair._call_wording_decision(
                self.endpoint(),
                ledger()["events"],
                candidate=source_document("ignored")["activities"][0],
                taxonomy=routing()["session_routes"],
                stage="draft",
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=transport,
                probe_once=lambda _endpoint: None,
            )

        self.assertEqual(
            ["portfolio_wording_decision", "portfolio_wording_fields_repair"], modes
        )
        self.assertEqual(unsafe["disposition"], decision["disposition"])
        self.assertEqual(
            unsafe["project_recommendation"], decision["project_recommendation"]
        )
        self.assertEqual("Prepared", decision["action"])
        self.assertEqual("July portfolio review package", decision["object"])
        self.assertNotIn("candidate", micro_payloads[0])
        self.assertEqual(
            {"disposition", "project_recommendation"},
            set(micro_payloads[0]["locked_decision"]),
        )

    def test_wording_decision_applies_flash_validated_taxonomy_route(self):
        source = source_document("SC — Reviewed NEEDS REVIEW status for July package")
        source["activities"][0].update({
            "client_project": "",
            "tag_names": [],
            "source_activity_ids": ["act-source1"],
        })
        analyzer_failure = {
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": ["ev-one"],
                "reason": "Flash reviewer exhausted one structural repair",
            }],
            "omissions": [],
        }
        decision = {
            "disposition": "activity",
            "action": "Prepared",
            "object": "July portfolio review package",
            "outcome": "for approved test-prep operations",
            "project_recommendation": {
                "name": "TST Prep Level 2",
                "prefix": "TSTP",
                "tag_names": ["Technical development"],
            },
            "exception_kind": "",
            "reason": "",
        }

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            return {"choices": [{"message": {"content": json.dumps(decision)}}]}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic, "_call_semantic_review", return_value=analyzer_failure
        ):
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=transport,
                routing=routing(),
                source_proposals=proposals(("Serenichron Level 2", ["Processes"])),
            )

        row = result["activities"][0]
        self.assertEqual("TST Prep Level 2", row["client_project"])
        self.assertEqual(["Technical development"], row["tag_names"])
        self.assertEqual(
            "TSTP — Prepared July portfolio review package for approved test-prep operations",
            row["description"],
        )

    def test_cache_resume_does_not_call_transport_again(self):
        source = source_document(
            "SC — Rebuilt Clockify review into a complete invoice-ready July package with too many secondary details across every client workstream and meeting across all three machines"
        )

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            return {"probe": "ok"} if payload.get("probe") else partition_response(payload)

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.jsonl"
            first = repair.repair_document(
                source, ledger(), endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(cache_path), transport=transport,
            )
            second = repair.repair_document(
                source, ledger(), endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(cache_path),
                transport=lambda *_: self.fail("cached repair must not call transport"),
            )

        self.assertEqual(first["activities"], second["activities"])

    def test_validation_failure_uses_single_activity_flash_recovery(self):
        source = source_document(
            "SC — Rebuilt Clockify review into a complete invoice-ready July package with too many secondary details across every client workstream and meeting across all three machines"
        )
        failure = {
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": ["ev-one"],
                "reason": "structural repair exhausted",
            }],
            "omissions": [],
        }
        replacement = {
            "activities": [{
                "activity_id": "act-recovered",
                "evidence_ids": ["ev-one"],
                "action": "Rebuilt",
                "object": "July portfolio descriptions",
                "outcome": "for invoice-ready client review",
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
            }],
            "exceptions": [],
            "omissions": [],
        }
        scopes = []

        def semantic_call(*_args, **kwargs):
            scopes.append(kwargs["review_scope"])
            if kwargs["review_scope"] in {
                "portfolio",
                "portfolio_single_activity_recovery",
            }:
                return replacement
            return failure

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic, "_call_semantic_review", side_effect=semantic_call
        ):
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=lambda *_: {"probe": "ok"},
            )

        self.assertEqual(
            ["portfolio", "portfolio_validation", "portfolio_single_activity_recovery"],
            scopes,
        )
        self.assertEqual(
            "SC — Rebuilt July portfolio descriptions for invoice-ready client review",
            result["activities"][0]["description"],
        )

    def test_flash_noise_reclassification_conserves_minutes(self):
        source = source_document(
            "SC — Rebuilt Clockify review into a complete invoice-ready July package with too many secondary details across every client workstream and meeting across all three machines"
        )
        source["activities"][0]["duration_seconds"] = 1800
        source["activities"][0]["allocation_segments"][0]["duration_seconds"] = 1800
        source.update({
            "source_minutes": 30,
            "review_minutes": 30,
            "excluded_minutes": 0,
            "source_seconds": 1800,
            "review_seconds": 1800,
            "excluded_seconds": 0,
            "review_activity_count": 1,
            "groups": [{
                "review_ids": ["pvi-one"],
                "source_minutes": 30,
                "review_minutes": 30,
                "excluded_minutes": 0,
                "source_seconds": 1800,
                "review_seconds": 1800,
                "excluded_seconds": 0,
                "reviewed_activities": 1,
                "exceptions": 0,
                "omissions": 0,
                "exclusion_reasons": [],
            }],
        })
        first = {
            "activities": [{
                "activity_id": "act-first",
                "evidence_ids": ["ev-one"],
            }],
            "exceptions": [],
            "omissions": [],
        }
        excluded = {
            "activities": [],
            "exceptions": [],
            "omissions": [{
                "lifecycle": "noise",
                "evidence_ids": ["ev-one"],
                "reason": "Autonomous background work without paired human instruction",
            }],
        }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic, "_call_semantic_review", side_effect=[first, excluded]
        ):
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=lambda *_: {"probe": "ok"},
            )

        self.assertEqual([], result["activities"])
        self.assertEqual(0, result["review_minutes"])
        self.assertEqual(30, result["excluded_minutes"])
        self.assertEqual(0, result["review_activity_count"])
        self.assertEqual(0, result["review_seconds"])
        self.assertEqual(1800, result["excluded_seconds"])
        self.assertEqual(["pvi-one"], result["repair"]["excluded_review_ids"])
        self.assertEqual(1, len(result["omissions"]))
        self.assertEqual(0, result["groups"][0]["review_minutes"])
        self.assertEqual(30, result["groups"][0]["excluded_minutes"])

        self.assertEqual(0, result["groups"][0]["review_seconds"])
        self.assertEqual(1800, result["groups"][0]["excluded_seconds"])
    def test_flash_trims_noise_evidence_without_changing_row_minutes(self):
        source = source_document(
            "SC — Rebuilt Clockify review into a complete invoice-ready July package with too many secondary details across every client workstream and meeting across all three machines"
        )
        source["activities"][0]["evidence_ids"] = ["ev-one", "ev-two"]
        source["groups"] = [{
            "review_ids": ["pvi-one"],
            "exceptions": 0,
            "omissions": 0,
        }]
        evidence = ledger()
        extra = copy.deepcopy(evidence["events"][0])
        extra["evidence_id"] = "ev-two"
        extra["content"] = ""
        evidence["events"].append(extra)
        first = {
            "activities": [{"activity_id": "act-first", "evidence_ids": ["ev-one"]}],
            "exceptions": [],
            "omissions": [{
                "lifecycle": "noise",
                "evidence_ids": ["ev-two"],
                "reason": "Empty assistant message",
            }],
        }
        validated = {
            "activities": [{
                "activity_id": "act-validated",
                "evidence_ids": ["ev-one"],
                "action": "Rebuilt",
                "object": "July portfolio descriptions",
                "outcome": "for invoice-ready client review",
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
            }],
            "exceptions": [],
            "omissions": [{
                "lifecycle": "noise",
                "evidence_ids": ["ev-two"],
                "reason": "Empty assistant message",
            }],
        }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic, "_call_semantic_review", side_effect=[first, validated]
        ):
            result = repair.repair_document(
                source,
                evidence,
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=lambda *_: {"probe": "ok"},
            )

        row = result["activities"][0]
        self.assertEqual("pvi-one", row["review_id"])
        self.assertEqual(30, row["duration_minutes"])
        self.assertEqual(["ev-one"], row["evidence_ids"])
        self.assertEqual(1, len(result["omissions"]))
        self.assertEqual(1, result["groups"][0]["omissions"])
        self.assertEqual(
            ["pvi-one"], result["repair"]["evidence_trimmed_review_ids"]
        )

    def test_wording_recovery_preserves_activity_route_and_noise_partition(self):
        source = source_document(
            "SC — Rebuilt Clockify review into a complete invoice-ready July package with too many secondary details across every client workstream and meeting across all three machines"
        )
        source["activities"][0]["evidence_ids"] = ["ev-one", "ev-two"]
        source["groups"] = [{
            "review_ids": ["pvi-one"],
            "exceptions": 0,
            "omissions": 0,
        }]
        evidence = ledger()
        noise_event = copy.deepcopy(evidence["events"][0])
        noise_event.update({"evidence_id": "ev-two", "content": ""})
        evidence["events"].append(noise_event)
        retained = {
            "activity_id": "act-retained",
            "evidence_ids": ["ev-one"],
            "action": "Rebuilt",
            "object": "sale issue workflow",
            "outcome": "TSTP — prepared invoice-ready review",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            },
        }
        noise = {
            "lifecycle": "noise",
            "evidence_ids": ["ev-two"],
            "reason": "Empty assistant message",
        }
        malformed = {
            "activities": [retained],
            "exceptions": [],
            "omissions": [noise],
        }
        still_malformed = copy.deepcopy(malformed)
        still_malformed["activities"][0].update({
            "action": "Implemented",
            "object": "private /Users model routing",
            "outcome": "for reliable delegated execution",
        })
        recovered = copy.deepcopy(malformed)
        recovered["activities"][0].update({
            "action": "Prepared",
            "object": "sale issue workflow",
            "outcome": "for invoice-ready client review",
        })
        scopes: list[str] = []
        wording_calls = 0

        def semantic_call(*_args, **kwargs):
            nonlocal wording_calls
            scopes.append(kwargs["review_scope"])
            if kwargs["review_scope"] in {
                "portfolio_wording_recovery",
                "portfolio_wording_recovery_retry",
            }:
                wording_calls += 1
                return still_malformed if wording_calls == 1 else recovered
            return malformed

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic, "_call_semantic_review", side_effect=semantic_call
        ):
            result = repair.repair_document(
                source,
                evidence,
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=lambda *_: {"probe": "ok"},
            )

        self.assertEqual(
            [
                "portfolio",
                "portfolio_validation",
                "portfolio_wording_recovery",
                "portfolio_wording_recovery_retry",
            ],
            scopes,
        )
        row = result["activities"][0]
        self.assertEqual(
            "SC — Prepared sale issue workflow for invoice-ready client review",
            row["description"],
        )
        self.assertEqual("Serenichron Level 2", row["client_project"])
        self.assertEqual(["Processes"], row["tag_names"])
        self.assertEqual(["ev-one"], row["evidence_ids"])
        self.assertEqual([noise], result["omissions"])

    def test_repair_keeps_source_backed_route_when_flash_wording_changes_route(self):
        source = source_document(
            "SC — Rebuilt Clockify review into a complete invoice-ready July package with too many secondary details across every client workstream and meeting across all three machines"
        )
        changed = {
            "activities": [{
                "evidence_ids": ["ev-one"],
                "action": "Rebuilt",
                "object": "July portfolio descriptions",
                "outcome": "for invoice-ready client review",
                "project_recommendation": {
                    "name": "TST Prep Level 2",
                    "prefix": "TSTP",
                    "tag_names": ["Technical development"],
                },
            }],
            "exceptions": [],
            "omissions": [],
        }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic, "_call_semantic_review", return_value=changed
        ):
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=lambda *_: {"probe": "ok"},
            )

        row = result["activities"][0]
        self.assertEqual("Serenichron Level 2", row["client_project"])
        self.assertEqual(["Processes"], row["tag_names"])
        self.assertTrue(row["description"].startswith("SC — "))

    def test_carries_unallocated_fathom_exception_without_adding_minutes(self):
        source = source_document(
            "SC — Rebuilt July portfolio descriptions for invoice-ready client review"
        )
        source.update({
            "source_minutes": 30,
            "review_minutes": 30,
            "excluded_minutes": 0,
        })
        meeting_exception = [{
            "evidence_id": "ev-fathom",
            "status": "exception",
            "reason": "meeting_overlap",
            "fixed_block_ids": ["ev-clockify"],
        }]

        with tempfile.TemporaryDirectory() as directory:
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=None,
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                fathom_reconciliation=meeting_exception,
            )

        self.assertEqual(1, len(result["activities"]))
        self.assertEqual(30, result["review_minutes"])
        self.assertEqual(0, result["excluded_minutes"])
        self.assertEqual([{
            "kind": "fathom_reconciliation",
            "evidence_ids": ["ev-fathom"],
            "reason": "meeting_overlap",
        }], result["exceptions"])
        self.assertEqual(
            ["ev-fathom"], result["repair"]["carried_fathom_exception_ids"]
        )

    def test_wording_failure_becomes_warning_without_aborting_batch(self):
        source = source_document("SC — Reviewed [unsafe] Markdown wording for July")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic,
            "_call_semantic_review",
            side_effect=repair.PortfolioRepairError("wording remained malformed"),
        ):
            result = repair.repair_document(
                source,
                ledger(),
                endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=lambda *_: {"probe": "ok"},
            )

        self.assertEqual(source["activities"], result["activities"])
        self.assertEqual("complete_with_warnings", result["repair"]["status"])
        self.assertEqual([{
            "review_id": "pvi-one",
            "reason": "wording remained malformed",
        }], result["repair"]["unresolved_wording"])

    def test_rejects_different_well_formed_flash_revision_before_transport(self):
        source = source_document(
            "SC — Rebuilt Clockify review into a complete invoice-ready July package with too many secondary details across every client workstream and meeting across all three machines"
        )
        wrong_endpoint = semantic.AnalyzerEndpoint(
            "primary", "http://flash", semantic.DEFAULT_PRIMARY_MODEL, revision="a" * 64
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(repair.PortfolioRepairError, "approved exact Flash revision"):
                repair.repair_document(
                    source, ledger(), endpoint=wrong_endpoint,
                    cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                    transport=lambda *_: self.fail("wrong revision must not call Flash"),
                )

    def test_workers_repair_rows_in_parallel_and_preserve_source_order(self):
        source = source_document(
            "SC — Rebuilt Clockify review into a complete invoice-ready July package with too many secondary details across every client workstream and meeting across all three machines"
        )
        second = copy.deepcopy(source["activities"][0])
        second.update({
            "review_id": "pvi-two",
            "activity_id": "act-two",
            "source_activity_ids": ["act-source-two"],
            "evidence_ids": ["ev-two"],
            "start": "2026-07-10T10:00+03:00",
            "end": "2026-07-10T10:30+03:00",
            "allocation_segments": [{
                "start": "2026-07-10T10:00+03:00",
                "end": "2026-07-10T10:30+03:00",
                "duration_minutes": 30,
            }],
        })
        source["activities"].append(second)
        evidence = ledger()
        second_event = copy.deepcopy(evidence["events"][0])
        second_event.update({
            "evidence_id": "ev-two",
            "observed_start": "2026-07-10T10:00+03:00",
            "observed_end": "2026-07-10T10:30+03:00",
        })
        evidence["events"].append(second_event)
        first_stage_barrier = threading.Barrier(2)
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["review_scope"] == "portfolio":
                first_stage_barrier.wait(timeout=2)
            calls.append(payload["review_scope"])
            return partition_response(payload)

        with tempfile.TemporaryDirectory() as directory:
            result = repair.repair_document(
                source, evidence, endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=transport, workers=2,
            )

        self.assertEqual(["pvi-one", "pvi-two"], [row["review_id"] for row in result["activities"]])
        self.assertEqual(2, calls.count("portfolio"))
        self.assertEqual(2, calls.count("portfolio_validation"))
        self.assertEqual(["pvi-one", "pvi-two"], result["repair"]["repaired_review_ids"])
        self.assertEqual(2, result["repair"]["workers"])

    def test_missing_route_repairs_from_consistent_source_and_preserves_other_fields(self):
        source = source_document("SC — Rebuilt July portfolio descriptions for invoice-ready client review")
        row = source["activities"][0]
        row.update({
            "client_project": "",
            "tag_names": [],
            "source_activity_ids": ["act-source1"],
            "opaque_integrity_marker": {"must": "stay unchanged"},
        })
        before = copy.deepcopy(row)
        taxonomy_sizes: list[int] = []

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            taxonomy_sizes.append(len(payload["clockify_taxonomy"]))
            return partition_response(payload)

        with tempfile.TemporaryDirectory() as directory:
            result = repair.repair_document(
                source, ledger(), endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=transport,
                routing=routing(),
                source_proposals=proposals(("Serenichron Level 2", ["Processes"])),
            )

        after = result["activities"][0]
        self.assertEqual("Serenichron Level 2", after["client_project"])
        self.assertEqual(["Processes"], after["tag_names"])
        self.assertEqual(
            {key: value for key, value in before.items() if key not in {"description", "client_project", "tag_names"}},
            {key: value for key, value in after.items() if key not in {"description", "client_project", "tag_names"}},
        )
        self.assertEqual([2, 2], taxonomy_sizes)
        self.assertEqual(["pvi-one"], result["repair"]["route_repaired_review_ids"])

    def test_inconsistent_source_routes_fail_closed_without_transport(self):
        source = source_document("SC — Rebuilt July portfolio descriptions for invoice-ready client review")
        source["activities"][0].update({
            "client_project": "",
            "tag_names": [],
            "source_activity_ids": ["act-source1", "act-source2"],
        })
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(repair.PortfolioRepairError, "inconsistent routes"):
                repair.repair_document(
                    source, ledger(), endpoint=self.endpoint(),
                    cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                    transport=lambda *_: self.fail("inconsistent source routes must not call Flash"),
                    routing=routing(),
                    source_proposals=proposals(
                        ("Serenichron Level 2", ["Processes"]),
                        ("TST Prep Level 2", ["Technical development"]),
                    ),
                )

    def test_cli_accepts_route_recovery_inputs(self):
        args = repair.parse_args([
            "portfolio-review.json",
            "--evidence-ledger", "evidence-ledger.json",
            "--routing", "routing.json",
            "--source-proposals", "proposals.json",
            "--output-dir", "output",
            "--cache", "cache.jsonl",
        ])

        self.assertEqual(Path("routing.json"), args.routing)
        self.assertEqual(Path("proposals.json"), args.source_proposals)
        self.assertEqual(4, args.workers)

    def test_clean_document_skips_flash_and_keeps_rows_unchanged(self):
        source = source_document("SC — Rebuilt July portfolio descriptions for invoice-ready client review")
        with tempfile.TemporaryDirectory() as directory:
            result = repair.repair_document(
                source, ledger(), endpoint=self.endpoint(),
                cache=semantic.AnalyzerResponseCache(Path(directory) / "cache.jsonl"),
                transport=lambda *_: self.fail("clean wording must not call Flash"),
            )

        self.assertEqual(source["activities"], result["activities"])
        self.assertEqual("pass", result["repair"]["status"])


if __name__ == "__main__":
    unittest.main()
