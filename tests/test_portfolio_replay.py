import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_portfolio_replay.py"
SPEC = importlib.util.spec_from_file_location("clockify_portfolio_replay", SCRIPT)
replay = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(replay)


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class PortfolioReplayTests(unittest.TestCase):
    def fixture(self, root: Path) -> dict[str, Path]:
        run = root / "run"
        for relative, value in {
            "evidence/evidence-ledger.json": {"manifest": {"manifest_id": "elm-a"}},
            "semantic-analysis.json": {"analyzer_cache": {"records": [{"cache_key": "arc-a", "decision_digest": "d" * 64}]}, "generated_at": "volatile"},
            "work-accounting-result.json": {
                "proposals": ["P001"],
                "meeting_reconciliation_digest": "sha256:" + "a" * 64,
                "meeting_dedup_version": "meeting-dedup/v1",
                "meeting_dedup_tolerance_seconds": 300,
                "meeting_split_digest": "sha256:" + "b" * 64,
            },
            "proposals.json": [{"id": "P001"}],
            "fathom-reconciliation.json": [],
        }.items():
            write(run / relative, value)
        paths = {"run_dir": run}
        for name, value in {
            "review": {"model": "deepseek-v4-flash:cloud", "revision": "rev", "activities": []},
            "repair": {"repair": {"model": "deepseek-v4-flash:cloud", "revision": "rev", "cache_hits": 99}},
            "quality": {"status": "pass", "updated_at": "volatile"},
            "routing": {"session_routes": [], "meeting_routes": []},
        }.items():
            path = root / f"{name}.json"
            write(path, value)
            paths[name] = path
        return paths

    def recording_ledger(self, *, local_minute=False):
        start = "2026-08-04 10:00" if local_minute else "2026-08-04T10:00:00Z"
        end = "2026-08-04 10:30" if local_minute else "2026-08-04T10:30:00Z"
        return {
            "manifest": {"manifest_id": "elm-a", "timezone": "Europe/Bucharest"},
            "events": [{
                "source_type": "fathom",
                "source_ref": {"source_id": "f-1"},
                "raw_source_span": {"start": start, "end": end},
                "attributes": {"recording_id": "f-1", "meeting_id": "event-1"},
            }],
        }

    def test_seal_and_exact_replay_pass_without_volatile_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            seal = replay.seal(**paths)
            changed = json.loads((paths["quality"]).read_text())
            changed["updated_at"] = "changed"
            write(paths["quality"], changed)
            report = replay.verify(seal, **paths)
        self.assertEqual("pass", report["status"])

    def test_tampered_accounting_or_cache_blocks_and_does_not_emit_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.fixture(root)
            seal = replay.seal(**paths)
            accounting = json.loads((paths["run_dir"] / "work-accounting-result.json").read_text())
            accounting["proposals"] = ["P002"]
            write(paths["run_dir"] / "work-accounting-result.json", accounting)
            with self.assertRaisesRegex(replay.PortfolioReplayError, "identity differs"):
                replay.verify(seal, **paths)
            self.assertFalse((root / "portfolio-replay-integrity.json").exists())

    def test_replay_rejects_dedup_algorithm_or_tolerance_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            sealed = replay.seal(**paths)
            self.assertEqual("meeting-dedup/v1", sealed["identity"]["meeting_dedup_version"])
            for field, value in (
                ("meeting_dedup_version", "meeting-dedup/v2"),
                ("meeting_dedup_tolerance_seconds", 301),
            ):
                with self.subTest(field=field):
                    accounting_path = paths["run_dir"] / "work-accounting-result.json"
                    changed = json.loads(accounting_path.read_text())
                    changed[field] = value
                    write(accounting_path, changed)
                    with self.assertRaises(replay.PortfolioReplayError):
                        replay.verify(sealed, **paths)
                    changed[field] = 300 if field.endswith("seconds") else "meeting-dedup/v1"
                    write(accounting_path, changed)

    def test_legacy_seal_remains_readable_without_canonical_meeting_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            sealed = replay.seal(**paths)
            legacy = copy.deepcopy(sealed)
            legacy["identity"]["schema_version"] = 1
            for field in (
                "meeting_reconciliation_digest",
                "meeting_dedup_version",
                "meeting_dedup_tolerance_seconds",
                "meeting_split_digest",
            ):
                legacy["identity"].pop(field)

            report = replay.verify(legacy, **paths)

        self.assertEqual("pass", report["status"])

    def test_new_seal_derives_canonical_dedup_identity_from_immutable_recordings(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            write(paths["run_dir"] / "evidence/evidence-ledger.json", {
                "manifest": {"manifest_id": "elm-a"},
                "events": [
                    {
                        "source_type": "fathom",
                        "source_ref": {"source_id": "f-1"},
                        "raw_source_span": {
                            "start": "2026-08-04T10:00:00Z",
                            "end": "2026-08-04T10:30:00Z",
                        },
                        "attributes": {"recording_id": "f-1", "meeting_id": "event-1"},
                    },
                    {
                        "source_type": "calendly",
                        "source_ref": {"source_id": "c-1"},
                        "raw_source_span": {
                            "start": "2026-08-04T10:00:00Z",
                            "end": "2026-08-04T10:30:00Z",
                        },
                        "attributes": {"recording_id": "c-1", "meeting_id": "event-1"},
                    },
                ],
            })
            accounting_path = paths["run_dir"] / "work-accounting-result.json"
            accounting = json.loads(accounting_path.read_text())
            for field in (
                "meeting_reconciliation_digest",
                "meeting_dedup_version",
                "meeting_dedup_tolerance_seconds",
                "meeting_split_digest",
            ):
                accounting.pop(field)
            write(accounting_path, accounting)

            sealed = replay.seal(**paths)

        self.assertEqual("meeting-dedup/v1", sealed["identity"]["meeting_dedup_version"])
        self.assertEqual(300, sealed["identity"]["meeting_dedup_tolerance_seconds"])

    def test_seal_rejects_well_formed_supplied_identity_that_disagrees_with_recordings(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            write(paths["run_dir"] / "evidence/evidence-ledger.json", self.recording_ledger())

            with self.assertRaisesRegex(replay.PortfolioReplayError, "does not match"):
                replay.seal(**paths)

    def test_verify_rejects_supplied_identity_drift_against_immutable_recordings(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            write(paths["run_dir"] / "evidence/evidence-ledger.json", self.recording_ledger())
            accounting_path = paths["run_dir"] / "work-accounting-result.json"
            accounting = json.loads(accounting_path.read_text())
            for field in (
                "meeting_reconciliation_digest", "meeting_dedup_version",
                "meeting_dedup_tolerance_seconds", "meeting_split_digest",
            ):
                accounting.pop(field)
            write(accounting_path, accounting)
            sealed = replay.seal(**paths)
            accounting.update({
                "meeting_reconciliation_digest": "sha256:" + "c" * 64,
                "meeting_dedup_version": "meeting-dedup/v9",
                "meeting_dedup_tolerance_seconds": 1,
                "meeting_split_digest": "sha256:" + "d" * 64,
            })
            write(accounting_path, accounting)

            with self.assertRaisesRegex(replay.PortfolioReplayError, "does not match"):
                replay.verify(sealed, **paths)

    def test_legacy_seal_with_local_minute_fathom_does_not_require_modern_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            ledger = self.recording_ledger(local_minute=True)
            write(paths["run_dir"] / "evidence/evidence-ledger.json", ledger)
            accounting_path = paths["run_dir"] / "work-accounting-result.json"
            accounting = json.loads(accounting_path.read_text())
            for field in (
                "meeting_reconciliation_digest", "meeting_dedup_version",
                "meeting_dedup_tolerance_seconds", "meeting_split_digest",
            ):
                accounting.pop(field)
            write(accounting_path, accounting)
            sealed = replay.seal(**self.fixture(Path(tmp) / "legacy-source"))
            legacy = copy.deepcopy(sealed)
            legacy["identity"]["schema_version"] = 1
            legacy["identity"]["artifacts"]["immutable_ledger"] = replay._digest(ledger)
            legacy["identity"]["artifacts"]["work_accounting_result"] = replay._digest(accounting)
            for field in (
                "meeting_reconciliation_digest", "meeting_dedup_version",
                "meeting_dedup_tolerance_seconds", "meeting_split_digest",
            ):
                legacy["identity"].pop(field)

            report = replay.verify(legacy, **paths)

        self.assertEqual("pass", report["status"])

    def test_modern_seal_normalizes_local_minute_fathom_with_manifest_timezone(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            write(paths["run_dir"] / "evidence/evidence-ledger.json", self.recording_ledger(local_minute=True))
            accounting_path = paths["run_dir"] / "work-accounting-result.json"
            accounting = json.loads(accounting_path.read_text())
            for field in (
                "meeting_reconciliation_digest", "meeting_dedup_version",
                "meeting_dedup_tolerance_seconds", "meeting_split_digest",
            ):
                accounting.pop(field)
            write(accounting_path, accounting)

            sealed = replay.seal(**paths)

        self.assertEqual("meeting-dedup/v1", sealed["identity"]["meeting_dedup_version"])

    def test_nonpassing_quality_cannot_be_sealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            write(paths["quality"], {"status": "blocked"})
            with self.assertRaisesRegex(replay.PortfolioReplayError, "passing portfolio"):
                replay.seal(**paths)
