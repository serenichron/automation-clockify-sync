"""Preserve rejected historical classification without widening response contracts."""
import json
import os
from pathlib import Path
import unittest

from scripts import semantic_analyzer as analyzer


class HistoricalRejectionClassifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = os.environ.get("CLOCKIFY_APPEND_FIXTURE")
        if not fixture:
            raise unittest.SkipTest("genuine historical rejected cache not supplied")
        binding = json.loads(Path(fixture).read_bytes())["binding"]
        source = Path(binding["sources"]["20261008T183839Z-repair-pm0kbo1e"]["artifacts"]["proposals"]["path"]).parent
        cache = analyzer.AnalyzerResponseCache(source / "analyzer-cache-used.jsonl")
        cls.record = cache._records["arc-bb417fded74b57e7a3227ed8167471339e4d59c67b6d544fd3d11fd683368cb2"]

    def test_genuine_sealed_historical_rejection_code_survives_failure_classification(self):
        self.assertEqual("rejected", self.record["status"])
        code = self.record["failure_code"]
        error = analyzer.AnalyzerContractError("analyzer cache records " + code)
        self.assertEqual(code, analyzer._contract_failure_code(error))

    def test_historical_rejection_does_not_enable_new_contract_repair(self):
        code = self.record["failure_code"]
        self.assertNotIn(code, analyzer.CONTRACT_FAILURE_CODES)
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer._repair_instruction(code)
        self.assertNotIn("response", self.record)

    def test_unknown_contract_code_is_still_other(self):
        self.assertEqual("contract_rejected_other", analyzer._contract_failure_code(
            analyzer.AnalyzerContractError("analyzer cache records contract_rejected_unknown_invented")))


if __name__ == "__main__":
    unittest.main()
