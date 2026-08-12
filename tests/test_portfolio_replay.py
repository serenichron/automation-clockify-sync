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
            "work-accounting-result.json": {"proposals": ["P001"]},
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

    def test_nonpassing_quality_cannot_be_sealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.fixture(Path(tmp))
            write(paths["quality"], {"status": "blocked"})
            with self.assertRaisesRegex(replay.PortfolioReplayError, "passing portfolio"):
                replay.seal(**paths)
