"""Recover only sealed extractor contract failures, without re-extracting work."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_scoped_semantic_recovery as command
from scripts import evidence_ledger
from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_semantic_analyzer import provider_partitions, provider_response, provider_members


TAXONOMY = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]
REASON = "qualified primary rejected this semantic chunk"
BINDING = {"source_type": "multica", "server_origin": "https://source.example",
           "workspace_id": "fixture-workspace", "author_id": "fixture-subject"}


class ExtractorFailureScopedRecoveryTests(unittest.TestCase):
    endpoint = semantic.AnalyzerEndpoint(
        "primary", "http://fixture", semantic.CURRENT_LIVE_FLASH_ROUTE[0],
        revision=semantic.CURRENT_LIVE_FLASH_ROUTE[1],
    )

    def fixture(self, root):
        """Actual rejected extraction decisions and native failure rows, not aliases."""
        source_dir = root / "source"
        source_dir.mkdir()
        cache = semantic.AnalyzerResponseCache(root / "working-cache.jsonl")
        events, failures, chunks = [], [], []
        for group, count in enumerate((5, 2, 3)):
            rows = [evidence_ledger.evidence_event(
                "codex_sessions_event",
                {"source_type": "codex_sessions", "source_id": f"group{group}:{ordinal}",
                 "machine": "fixture", "session_id": f"group{group}", "ordinal": ordinal + 1},
                observed_at=f"2026-10-19T10:{group * 10 + ordinal:02}:00+03:00",
                raw_source_span={"timestamp": f"2026-10-19T10:{group * 10 + ordinal:02}:00+03:00"},
                attributes={"kind": "message", "role": "user" if ordinal == 0 else "assistant",
                            "content": "Requested investigation" if ordinal == 0 else "Completed investigation"},
            ).document() for ordinal in range(count)]
            events.extend(rows)

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                if payload.get("probe"):
                    return {"probe": "ok"}
                if group < 2:
                    return {"activities": None, "exceptions": [], "omissions": []}
                response = provider_response(payload, provider_members(payload)[:1])
                members = provider_members(payload)
                response["exceptions"] = [{"kind": "insufficient_evidence", "reason": "Unrelated evidence limitation",
                                           "evidence_partitions": provider_partitions(members[1:2])}]
                response["omissions"] = [{"lifecycle": "noise", "reason": "Unrelated source noise",
                                          "evidence_partitions": provider_partitions(members[2:])}]
                return response

            analysis = semantic.analyze_tiered(
                semantic.with_actor_context(rows, subject_binding=BINDING),
                primary=self.endpoint, cache=cache, transport=transport,
                private_text_approved=True, review_taxonomy=TAXONOMY, max_workers=1,
                review_prompt_version=semantic.COMMISSIONED_REVIEW_PROMPT_VERSION,
            )
            chunks.extend(analysis["analysis_chunks"])
            if group < 2:
                self.assertEqual([REASON], [row["reason"] for row in analysis["exceptions"]])
                self.assertEqual([count], [len(row["evidence_ids"]) for row in analysis["exceptions"]])
                failures.extend(analysis["exceptions"])
        source = copy.deepcopy(analysis)
        source.update(exceptions=failures + source["exceptions"], analysis_chunks=chunks,
                      ledger_event_count=len(events),
                      ledger_evidence_digest=semantic.stable_digest("led-", sorted(e["evidence_id"] for e in events)),
                      analyzer_cache=cache.summary())
        records = cache.records_for_snapshot(source["analyzer_cache"]["records"], configured_endpoints=(self.endpoint,))
        sealed = b"".join((semantic.canonical_json(row) + "\n").encode() for row in records)
        (source_dir / "analyzer-cache-used.jsonl").write_bytes(sealed)
        source["analyzer_cache"]["snapshot"] = {"path": "analyzer-cache-used.jsonl",
                                               "record_count": len(records),
                                               "sha256": hashlib.sha256(sealed).hexdigest()}
        ledger = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.EvidenceEvent.from_document(e) for e in events),
            timezone="Europe/Bucharest", member_identities=("member@example.com",),
        )
        (source_dir / "evidence").mkdir()
        documents = {
            "semantic-analysis.json": source,
            "evidence/evidence-ledger.json": {"schema_version": evidence_ledger.SCHEMA_VERSION,
                                             "events": events, "manifest": ledger.manifest.document()},
            "routing.json": {"session_routes": [{"pattern": "unmatched-route", **TAXONOMY[0]}],
                             "meeting_routes": [], "semantic_actor_contract": semantic.ACTOR_CONTRACT,
                             "semantic_subject_binding": BINDING},
            "proposals.json": [{"id": "P001", "start": "2026-10-19T11:00:00+03:00",
                                "end": "2026-10-19T11:10:00+03:00"}],
        }
        for name, value in documents.items():
            (source_dir / name).write_text(json.dumps(value))
        groups = [tuple(row["evidence_ids"]) for row in failures]
        digests = [semantic.stable_digest("frt-", list(key), length=64) for key in groups]
        selected = sorted(eid for key in groups for eid in key)
        scope = root / "scope.json"
        scope.write_text(json.dumps({"evidence_ids": selected}))
        return source_dir, source, events, groups, digests, selected, scope

    def test_native_plan_and_cached_reconstruction_preserve_all_non_targets(self):
        # A rejected initial extract must become scoped review, never all-ledger extraction.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir, source, events, groups, digests, selected, scope = self.fixture(root)
            before = {p: p.read_bytes() for p in source_dir.rglob("*") if p.is_file()}
            argv = [str(source_dir), "--scope-file", str(scope), "--output-dir", str(root / "recovery")]
            for digest in digests:
                argv.extend(["--failed-review-digest", digest])
            payloads = []

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                self.assertEqual("review", payload["mode"])
                self.assertEqual("failed_review_scoped_recovery", payload["review_scope"])
                self.assertEqual("clockify-semantic-review-v9", payload["review_prompt_version"])
                payloads.append(payload)
                return provider_response(payload)

            with mock.patch.object(semantic.AnalyzerEndpoint, "from_env", return_value=self.endpoint), \
                 mock.patch.object(semantic, "http_transport", side_effect=transport), \
                 mock.patch.object(semantic, "analyze_tiered", side_effect=AssertionError("Recovery must not re-extract")), \
                 mock.patch.dict("os.environ", {semantic.PRIVATE_TEXT_APPROVAL_ENV: "approved"}):
                try:
                    plan = command.run(command.parse_args(argv + ["--plan"]))
                except pipeline.WorkAccountingError as exc:
                    self.fail(f"Native planning must admit the exact sealed extractor failures: {exc}")
                self.assertEqual(7, plan["selected_event_count"])
                self.assertEqual([2, 5], sorted(row["event_count"] for row in plan["requests"]))
                self.assertEqual(set(selected), {eid for row in plan["requests"] for eid in row["evidence_ids"]})
                self.assertEqual([], payloads)
                self.assertFalse((root / "recovery").exists())
                receipt = command.run(command.parse_args(argv))
            self.assertEqual(2, len(payloads))
            self.assertEqual(0, receipt["new_proposal_count"])
            recovered = json.loads((root / "recovery/semantic-analysis.json").read_text())
            self.assertEqual(source["activities"], recovered["activities"][:len(source["activities"])])
            self.assertEqual(source["exceptions"][2:], recovered["exceptions"])
            self.assertEqual(source["omissions"], recovered["omissions"])
            self.assertEqual({frozenset(g) for g in groups},
                             {frozenset(a["evidence_ids"]) for a in recovered["activities"][len(source["activities"]):]})
            self.assertEqual({digest: "contract_rejected" for digest in digests},
                             recovered["failed_review_retry"]["failure_codes"])
            self.assertEqual(source["ledger_evidence_digest"], recovered["ledger_evidence_digest"])
            self.assertEqual(source["analyzer_cache"]["snapshot"]["sha256"],
                             recovered["failed_review_retry"]["source_cache_sha256"])
            after = {p: p.read_bytes() for p in (root / "recovery").rglob("*") if p.is_file()}
            with mock.patch.object(semantic, "http_transport", side_effect=AssertionError("Replay inferred")):
                replay = command.validate_cached_recovery(source_dir, root / "recovery", digests)
            self.assertEqual(recovered["activities"], replay["analysis"]["activities"])
            with mock.patch.object(semantic.AnalyzerEndpoint, "from_env", return_value=self.endpoint), \
                 mock.patch.object(semantic, "analyze_tiered", side_effect=AssertionError("Recovery re-extracted unaffected work")):
                routing = json.loads((source_dir / "routing.json").read_text())
                native = pipeline.analyze_ledger(
                    events, analyzer_cache_path=root / "recovery/analyzer-response-cache.jsonl",
                    review_taxonomy=pipeline._semantic_review_taxonomy(routing), review_routing=routing,
                    failed_review_retry_source=source_dir / "semantic-analysis.json",
                    failed_review_retry_digest=digests,
                )
            self.assertEqual(recovered["activities"], native["activities"])
            self.assertEqual(before, {p: p.read_bytes() for p in source_dir.rglob("*") if p.is_file()})
            self.assertEqual(after, {p: p.read_bytes() for p in (root / "recovery").rglob("*") if p.is_file()})

    def test_transport_and_nonexact_reasons_are_not_recoverable_extractor_contracts(self):
        # The new branch must not turn network failures or arbitrary exception text into retries.
        with tempfile.TemporaryDirectory() as temporary:
            source_dir, source, events, groups, digests, selected, _ = self.fixture(Path(temporary))
            for kind, reason in (("analyzer_failure", "qualified primary exhausted bounded transport recovery"),
                                 ("analyzer_failure", "qualified primary exhausted bounded timeout recovery"),
                                 ("analyzer_failure", REASON + " extra"),
                                 ("unrelated_failure", REASON)):
                with self.subTest(kind=kind, reason=reason):
                    bad = copy.deepcopy(source)
                    bad["exceptions"][0].update(kind=kind, reason=reason)
                    with self.assertRaises(pipeline.WorkAccountingError):
                        pipeline._failed_review_retry_targets(bad, events, source_dir / "analyzer-cache-used.jsonl", digests)
                    with self.assertRaises(pipeline.WorkAccountingError):
                        pipeline.run_scoped_failed_review_retry(
                            bad, events, primary=self.endpoint,
                            cache=semantic.AnalyzerResponseCache(source_dir / "analyzer-cache-used.jsonl"),
                            review_taxonomy=TAXONOMY, targets={key: "contract_rejected" for key in groups},
                            source_semantic_sha256="a" * 64, selected_evidence_ids=selected, plan_only=True,
                            actor_contract=semantic.ACTOR_CONTRACT, actor_subject_binding=BINDING,
                        )

    def test_extractor_target_does_not_acquire_a_fabricated_reviewer_category(self):
        # Extractor leaves retain only generic rejection, not duplicate/effort diagnostics.
        with tempfile.TemporaryDirectory() as temporary:
            source_dir, source, events, groups, _, selected, _ = self.fixture(Path(temporary))
            with self.assertRaisesRegex(pipeline.WorkAccountingError, "extractor.*generic"):
                pipeline.run_scoped_failed_review_retry(
                    source, events, primary=self.endpoint,
                    cache=semantic.AnalyzerResponseCache(source_dir / "analyzer-cache-used.jsonl"),
                    review_taxonomy=TAXONOMY,
                    targets={key: "contract_rejected_duplicate_evidence" for key in groups},
                    source_semantic_sha256="a" * 64, selected_evidence_ids=selected, plan_only=True,
                    actor_contract=semantic.ACTOR_CONTRACT, actor_subject_binding=BINDING,
                )

    def test_source_ledger_and_cache_bindings_remain_required(self):
        # A matching failure reason does not authorize a changed source ledger or cache prefix.
        with tempfile.TemporaryDirectory() as temporary:
            source_dir, source, events, _, digests, _, _ = self.fixture(Path(temporary))
            for field in ("ledger_evidence_digest", "cache_hash"):
                bad = copy.deepcopy(source)
                if field == "cache_hash":
                    bad["analyzer_cache"]["snapshot"]["sha256"] = "0" * 64
                else:
                    bad[field] = "foreign"
                with self.subTest(field=field), self.assertRaisesRegex(pipeline.WorkAccountingError, "binding"):
                    pipeline._failed_review_retry_targets(bad, events, source_dir / "analyzer-cache-used.jsonl", digests)


if __name__ == "__main__":
    unittest.main()
