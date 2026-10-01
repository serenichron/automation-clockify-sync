"""A repair may append proved machine credits without changing its parent."""
from __future__ import annotations

import contextlib
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_review_run as fixtures
import test_review_corrections as human_fixtures
import test_verified_posted_credit as credit_fixtures
from test_review_run_chained_repair_replay import validate_repair
from scripts import (
    clockify_review_cycle, collector_receipts, evidence_ledger,
    review_corrections, semantic_analyzer, work_accounting_pipeline,
)


run = fixtures.review_run
write_json = fixtures.write_json


class PostedCreditSnapshotTests(unittest.TestCase):
    def fixture(self, root: Path):
        runs = root / "runs"
        source, _replay = fixtures.ReviewRunResultTests._complete_replay_fixture(runs)
        fixtures.ReviewRunResultTests._bootstrap_snapshots(source, _replay)
        (source / "run-report.md").write_text("# synthetic source\n")
        current, _blocks, credits = credit_fixtures.VerifiedPostedCreditTests().fixture(runs)
        prior_path = runs / "prior-run" / "proposals.json"
        prior = json.loads(prior_path.read_text())
        prior.append(credit_fixtures._proposal(
            "old-legal", ["ev-legal"], "Old legal outcome",
            "2026-09-12T08:21:00+03:00", 2,
        ))
        legal = credit_fixtures._proposal(
            "new-legal", ["ev-legal"], "Current legal outcome",
            "2026-09-12T09:21:00+03:00", 2,
        )
        current.append(legal)
        current.extend(
            credit_fixtures._proposal(
                f"other-{index}", [f"ev-other-{index}"], f"Other outcome {index}",
                f"2026-09-12T10:{index:02d}:00+03:00", 1,
            ) for index in range(4)
        )
        write_json(prior_path, prior)
        capture = prior_path.read_bytes()
        for credit in credits:
            credit["prior_proposals_sha256"] = "sha256:" + hashlib.sha256(capture).hexdigest()
            credit["prior_proposals_base64"] = base64.b64encode(capture).decode("ascii")
        credits.append({
            **{key: credits[0][key] for key in (
                "schema_version", "record_type", "project_suffix", "prior_run_id",
                "sheet_publication_run_id", "prior_proposals_sha256",
                "prior_proposals_base64",
            )},
            "evidence_fingerprint": review_corrections.evidence_fingerprint(["ev-legal"]),
            "current_description_sha256": credit_fixtures._description_digest(legal["description"]),
            "posted_rows": [{
                "sheet_row": credit_fixtures._sheet_row(prior[-1]),
                "clockify_block_id": "clockify-3",
            }],
        })
        events = []
        for index, proposal in enumerate(prior):
            event = evidence_ledger.evidence_event(
                "clockify", {"source_type": "clockify", "source_id": f"posted-{index}"},
                observed_at=proposal["start"],
                raw_source_span={"start": proposal["start"], "end": proposal["end"]},
                attributes={
                    "description": proposal["description"],
                    "project_id_suffix": proposal["clockify_project_suffix"],
                },
            )
            events.append(event)
            for credit in credits:
                for posted in credit["posted_rows"]:
                    if posted["sheet_row"][0] == credit_fixtures._sheet_row(proposal)[0]:
                        posted["clockify_block_id"] = event.evidence_id
        ledger = evidence_ledger.EvidenceLedger(
            tuple(events), {"clockify": {"status": "complete"}}, "Europe/Bucharest",
        )
        write_json(source / "evidence" / "evidence-ledger.json", {
            "schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(),
            "events": [event.document() for event in ledger.events],
        })
        report = json.loads((source / "run-report.json").read_text())
        report["evidence_ledger"]["source_completeness"] = ledger.manifest.document()["source_completeness"]
        write_json(source / "run-report.json", report)
        write_json(source / "proposals.json", current)
        slice_ = run.clockify_sync_collect.plan_slices(
            fixtures.dt.datetime(2026, 8, 1, tzinfo=fixtures.dt.timezone.utc),
            fixtures.dt.datetime(2026, 8, 2, tzinfo=fixtures.dt.timezone.utc),
            zone=run.clockify_sync_collect.BUCHAREST,
        )[0]
        (source / "completion-bundle.json").unlink()
        collector_receipts.write_completion_bundle(
            source / "completion-bundle.json",
            collector_receipts.build_completion_bundle(source, slice_=slice_),
        )
        override = root / "credits.jsonl"
        kwargs = {
            "runs_root": runs, "current_proposals": current,
            "existing_blocks": [
                credit_fixtures._block(row, events[index].evidence_id)
                for index, row in enumerate(prior)
            ],
        }
        for credit in credits:
            self.assertTrue(review_corrections.append_verified_posted_credit(
                override, credit, **kwargs,
            ))
        return runs, source, override

    def test_repair_cli_snapshots_only_proved_machine_credit_tail(self):
        """Catches a repair silently rejecting or replacing a proved credit snapshot."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs, source, override = self.fixture(root)
            before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}

            def process(args, child, _gate):
                self.assertEqual(override.read_bytes(), args.corrections.read_bytes())
                self.assertEqual(child / "review-corrections.jsonl", args.corrections)
                proposals = json.loads((source / "proposals.json").read_text())
                credits = review_corrections.load_verified_posted_credits(args.corrections)
                _ledger, events = work_accounting_pipeline.load_ledger(
                    source / "evidence" / "evidence-ledger.json"
                )
                remaining, skipped = work_accounting_pipeline._apply_verified_posted_credits(
                    proposals, work_accounting_pipeline._existing_blocks(events), credits,
                )
                self.assertEqual(8, len(proposals))
                self.assertEqual(3, len(credits))
                self.assertEqual(5, len(remaining))
                credited_keys = {row["candidate_key"] for row in skipped}
                self.assertEqual(360, sum(
                    row["duration_seconds"] for row in proposals
                    if row["candidate_key"] in credited_keys
                ))
                return 0, child / "autopilot-result.json"

            with mock.patch.object(run, "RUNS", runs), mock.patch.object(
                run, "_process_run", side_effect=process,
            ), mock.patch.object(
                run, "_run", side_effect=AssertionError("collector invoked"),
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, run.main([
                    "--repair-from", str(source), "--corrections", str(override),
                    "--state", str(root / "state"),
                ]))
            self.assertEqual(before, {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()})

    def test_repair_rejects_invalid_machine_tail_and_human_decision(self):
        """Catches accepting a syntactically chained but unproved correction tail."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs, source, override = self.fixture(root)
            original = override.read_text()
            first = json.loads(original.splitlines()[0])
            for label, change, rehash in (
                ("broken-chain", lambda row: row.update(project_suffix="other"), False),
                ("wrong-prior-source", lambda row: row.update(prior_run_id="missing-run"), True),
                ("wrong-clockify-block", lambda row: row["posted_rows"][0].update(clockify_block_id="wrong"), True),
            ):
                with self.subTest(label=label):
                    row = json.loads(json.dumps(first))
                    change(row)
                    if rehash:
                        row["canonical_digest"] = review_corrections.canonical_digest(
                            review_corrections._without_integrity(row)
                        )
                    candidate = root / f"{label}.jsonl"
                    candidate.write_text(json.dumps(row) + "\n")
                    with mock.patch.object(run, "RUNS", runs), self.assertRaises(run.ReviewRunError):
                        run._prepare_repair_run(source, corrections_override=candidate)
            human = root / "human.jsonl"
            human.write_text(original)
            review_corrections.append_decision(
                human, human_fixtures.decision(human_fixtures.item()),
                item=human_fixtures.item(),
            )
            with mock.patch.object(run, "RUNS", runs), self.assertRaises(run.ReviewRunError):
                run._prepare_repair_run(source, corrections_override=human)
            self.assertEqual(b"", (source / "review-corrections.jsonl").read_bytes())

    def test_repair_rejects_changed_parent_prefix_even_with_valid_hash_chain(self):
        """Catches replacing the first frozen record with equivalent JSON text."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs, source, override = self.fixture(root)
            lines = override.read_text().splitlines()
            (source / "review-corrections.jsonl").write_text(lines[0] + "\n")
            source_bundle = source / "completion-bundle.json"
            source_bundle.unlink()
            slice_ = run.clockify_sync_collect.plan_slices(
                fixtures.dt.datetime(2026, 8, 1, tzinfo=fixtures.dt.timezone.utc),
                fixtures.dt.datetime(2026, 8, 2, tzinfo=fixtures.dt.timezone.utc),
                zone=run.clockify_sync_collect.BUCHAREST,
            )[0]
            collector_receipts.write_completion_bundle(
                source_bundle, collector_receipts.build_completion_bundle(source, slice_=slice_),
            )
            candidate = root / "changed-prefix.jsonl"
            first = json.dumps(json.loads(lines[0]), separators=(", ", ": "))
            candidate.write_text("\n".join([first, *lines[1:]]) + "\n")
            self.assertEqual(3, len(review_corrections.load_verified_posted_credits(candidate)))
            with mock.patch.object(run, "RUNS", runs), self.assertRaises(run.ReviewRunError):
                run._prepare_repair_run(source, corrections_override=candidate)

    def test_completion_rejects_incomplete_credit_digest_binding(self):
        """Catches sealing a repair when only half the correction transition is bound."""
        with tempfile.TemporaryDirectory() as temporary:
            runs, source, override = self.fixture(Path(temporary))
            with mock.patch.object(run, "RUNS", runs):
                child = run._prepare_repair_run(source, corrections_override=override)
                lineage_path = child / "repair-source.json"
                lineage = json.loads(lineage_path.read_text())
                del lineage["repair_corrections_sha256"]
                write_json(lineage_path, lineage)
                with self.assertRaises(run.ReviewRunError):
                    run._finalize_repair_completion(child)


class InvalidEffortV3ConsumerTests(unittest.TestCase):
    def test_sealed_v3_repair_replays_and_verifies_collector_ancestry_offline(self):
        """Catches a consumer rejecting or relabeling the producer's v3 retry mode."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            original_provider = fixtures.analyzer_provider_response
            review_calls = 0

            def invalid_review_provider(payload):
                nonlocal review_calls
                response = original_provider(payload)
                if payload.get("mode") == "review":
                    review_calls += 1
                    if review_calls <= 3:
                        response["activities"][0]["effort"]["minimum_minutes"] = 0
                return response

            with mock.patch.object(
                fixtures, "analyzer_provider_response", side_effect=invalid_review_provider,
            ):
                source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(
                    runs, root, failed_review=True,
                )
            source_analysis = json.loads((source / "semantic-analysis.json").read_text())
            failures = [
                row for row in source_analysis["exceptions"]
                if row["kind"] == "analyzer_review_failure"
            ]
            self.assertEqual(1, len(failures), failures)
            self.assertIn("contract_rejected_invalid_effort", failures[0]["reason"])
            target = semantic_analyzer.stable_digest(
                "frt-", failures[0]["evidence_ids"], length=64,
            )
            endpoint = semantic_analyzer.AnalyzerEndpoint(
                "clockify_analyzer_primary", "https://offline.invalid/v1/chat/completions",
                semantic_analyzer.DEFAULT_PRIMARY_MODEL,
                revision=semantic_analyzer.DEFAULT_PRIMARY_REVISION,
            )
            original_analyze = semantic_analyzer.analyze_tiered
            scoped = work_accounting_pipeline.run_scoped_failed_review_retry
            calls = []

            def still_invalid_transport(_endpoint, body):
                payload = json.loads(body["messages"][-1]["content"])
                response = original_provider(payload)
                if payload.get("mode") == "review":
                    response["activities"][0]["effort"]["minimum_minutes"] = 0
                return response

            def repaired_transport(_endpoint, body):
                payload = json.loads(body["messages"][-1]["content"])
                calls.append(payload)
                return original_provider(payload)

            with mock.patch.object(run, "RUNS", runs):
                parent = run._prepare_repair_run(source)
                parent_cache = parent / "analyzer-cache-retry.jsonl"
                parent_cache.write_bytes((parent / "analyzer-cache-used.jsonl").read_bytes())
                with (
                    mock.patch.object(
                        semantic_analyzer.AnalyzerEndpoint, "from_env",
                        side_effect=lambda name, **_kw: endpoint
                        if name == "CLOCKIFY_ANALYZER_PRIMARY" else None,
                    ),
                    mock.patch.object(
                        semantic_analyzer, "analyze_tiered",
                        side_effect=lambda events, **kwargs: original_analyze(
                            events, transport=still_invalid_transport,
                            private_text_approved=True, **kwargs,
                        ),
                    ),
                ):
                    work_accounting_pipeline.run_accounting(
                        parent, root=fixtures.ROOT,
                        routing_path=parent / "routing.json",
                        corrections_path=parent / "review-corrections.jsonl",
                        analyzer_cache_path=parent_cache,
                        failed_review_retry_source=run._repair_analysis_fixture(parent),
                        failed_review_retry_digest=target,
                        analyzer_workers=1,
                    )
                parent_analysis = json.loads((parent / "semantic-analysis.json").read_text())
                parent_failure = next(
                    row for row in parent_analysis["exceptions"]
                    if row["kind"] == "analyzer_review_failure"
                )
                self.assertIn("contract_rejected_invalid_effort", parent_failure["reason"])
                self.assertIn("bounded failed-review retry", parent_failure["reason"])
                validate_repair(parent, runs, root / "parent-items.json")
                run._finalize_repair_completion(parent)
                target = semantic_analyzer.stable_digest(
                    "frt-", parent_failure["evidence_ids"], length=64,
                )
                repair = run._prepare_repair_run(parent)
                retry_cache = repair / "analyzer-cache-retry.jsonl"
                retry_cache.write_bytes((repair / "analyzer-cache-used.jsonl").read_bytes())
                with (
                    mock.patch.object(
                        semantic_analyzer.AnalyzerEndpoint, "from_env",
                        side_effect=lambda name, **_kw: endpoint
                        if name == "CLOCKIFY_ANALYZER_PRIMARY" else None,
                    ),
                    mock.patch.object(
                        work_accounting_pipeline, "run_scoped_failed_review_retry",
                        side_effect=lambda *args, **kwargs: scoped(
                            *args, **{**kwargs, "transport": repaired_transport,
                                     "private_text_approved": True},
                        ),
                    ),
                ):
                    work_accounting_pipeline.run_accounting(
                        repair, root=fixtures.ROOT,
                        routing_path=repair / "routing.json",
                        corrections_path=repair / "review-corrections.jsonl",
                        analyzer_cache_path=retry_cache,
                        failed_review_retry_source=run._repair_analysis_fixture(repair),
                        failed_review_retry_digest=target,
                        analyzer_workers=1,
                    )
                repaired = json.loads((repair / "semantic-analysis.json").read_text())
                self.assertEqual(
                    "scoped_review_v3_invalid_effort", repaired["failed_review_retry"]["mode"],
                )
                self.assertEqual(1, len(calls))
                self.assertEqual(
                    "scoped_review_v3_invalid_effort",
                    calls[0]["scoped_failed_review"]["mode"],
                )
                validate_repair(repair, runs, root / "repair-items.json")
                bundle = run._finalize_repair_completion(repair)
                ancestor, _ = clockify_review_cycle._collector_ancestor_from_repair(
                    {"runs_dir": str(runs)}, repair, bundle,
                )
                self.assertEqual(source, ancestor)
                with (
                    mock.patch.dict(os.environ, {
                        "CLOCKIFY_ANALYZER_PRIMARY_URL": "",
                        "CLOCKIFY_ANALYZER_FALLBACK_URL": "",
                    }),
                    mock.patch.object(
                        run, "_sealed_replay_transport",
                        side_effect=AssertionError("replay must not call inference"),
                    ),
                ):
                    code = run.main([
                        "--replay-from", str(repair), "--runs-root", str(runs),
                        "--state", str(root / "replay-items.json"),
                    ])
            self.assertEqual(0, code)
            replays = list(runs.glob(f"*-replay-{repair.name}*"))
            self.assertEqual(1, len(replays))
            self.assertEqual(
                "pass", json.loads((replays[0] / "replay-integrity.json").read_text())["status"],
            )
            self.assertEqual(
                (repair / "work-accounting-result.json").read_bytes(),
                (replays[0] / "work-accounting-result.json").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
