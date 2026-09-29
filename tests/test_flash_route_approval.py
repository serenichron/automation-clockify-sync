"""Catches the retired DeepSeek V4 Flash release blocking every new analysis run.

Ollama Cloud retired deepseek-v4-flash:0731 (the target of deepseek-v4-flash:cloud)
on 2026-09-25.  New inference must use deepseek-v4.1-flash:cloud at its exact
release, while artifacts already produced by the retired release stay valid.
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_accounting_runner as runner
from scripts import semantic_analyzer as semantic


SCRIPTS = Path(__file__).parents[1] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


quality = _load("clockify_portfolio_quality")
repair = _load("clockify_portfolio_repair")

CURRENT_MODEL = "deepseek-v4.1-flash:cloud"
CURRENT_REVISION = "e04da138d31e0c9468e982e1ae9503d06cb7e170caa16a90c17d931c4aa140f8"
RETIRED_MODEL = "deepseek-v4-flash:cloud"
RETIRED_REVISION = "6ca9e29c41ded618e527ee40e305ed5e4d8319b571d5b6695a30e1df65f103cc"


class FlashRouteApprovalTests(unittest.TestCase):
    def test_new_inference_defaults_to_current_flash_release(self):
        self.assertEqual(CURRENT_MODEL, semantic.DEFAULT_PRIMARY_MODEL)
        self.assertIn(CURRENT_MODEL, semantic.APPROVED_PRIMARY_MODELS)

    def test_primary_environment_rejects_retired_release_before_live_use(self):
        environment = {
            "CLOCKIFY_ANALYZER_PRIMARY_URL": "https://analyzer.example/v1",
            "CLOCKIFY_ANALYZER_PRIMARY_MODEL": RETIRED_MODEL,
            "CLOCKIFY_ANALYZER_PRIMARY_REVISION": RETIRED_REVISION,
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(semantic.AnalyzerError, "current exact Flash release"):
                semantic.AnalyzerEndpoint.from_env(
                    "CLOCKIFY_ANALYZER_PRIMARY",
                    default_model=semantic.DEFAULT_PRIMARY_MODEL,
                )

    def test_fallback_environment_is_rejected_even_for_current_release(self):
        environment = {
            "CLOCKIFY_ANALYZER_FALLBACK_URL": "https://fallback.example/v1",
            "CLOCKIFY_ANALYZER_FALLBACK_MODEL": CURRENT_MODEL,
            "CLOCKIFY_ANALYZER_FALLBACK_REVISION": CURRENT_REVISION,
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(semantic.AnalyzerError, "fallback.*not approved"):
                semantic.AnalyzerEndpoint.from_env("CLOCKIFY_ANALYZER_FALLBACK")

    def test_probe_rejects_retired_release_before_transport(self):
        endpoint = semantic.AnalyzerEndpoint(
            "primary",
            "https://analyzer.example/v1/chat/completions",
            RETIRED_MODEL,
            revision=RETIRED_REVISION,
        )

        with self.assertRaisesRegex(semantic.AnalyzerError, "current exact Flash release"):
            semantic.probe_endpoint(
                endpoint,
                transport=lambda *_args: self.fail("retired route must not be probed"),
            )

    def test_http_send_rejects_retired_release_before_network(self):
        endpoint = semantic.AnalyzerEndpoint(
            "primary",
            "https://analyzer.example/v1/chat/completions",
            RETIRED_MODEL,
            revision=RETIRED_REVISION,
        )

        with mock.patch.object(semantic.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(semantic.AnalyzerError, "current exact Flash release"):
                semantic.http_transport(endpoint, {"model": RETIRED_MODEL})
        urlopen.assert_not_called()

    def test_runner_accepts_current_release_and_rejects_mismatched_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.jsonl"
            route = runner._validated_analyzer_route(
                {
                    "CLOCKIFY_ANALYZER_PRIMARY_MODEL": CURRENT_MODEL,
                    "CLOCKIFY_ANALYZER_PRIMARY_REVISION": CURRENT_REVISION,
                },
                cache,
            )
            self.assertEqual(CURRENT_MODEL, route["model"])
            self.assertEqual(CURRENT_REVISION, route["revision"])
            with self.assertRaises(runner.RunnerConfigurationError):
                runner._validated_analyzer_route(
                    {
                        "CLOCKIFY_ANALYZER_PRIMARY_MODEL": CURRENT_MODEL,
                        "CLOCKIFY_ANALYZER_PRIMARY_REVISION": RETIRED_REVISION,
                    },
                    cache,
                )

    def test_portfolio_quality_accepts_both_releases_but_not_mixed_pairs(self):
        self.assertTrue(quality.is_approved_flash_route(CURRENT_MODEL, CURRENT_REVISION))
        self.assertTrue(quality.is_approved_flash_route(RETIRED_MODEL, RETIRED_REVISION))
        self.assertFalse(quality.is_approved_flash_route(CURRENT_MODEL, RETIRED_REVISION))
        self.assertFalse(quality.is_approved_flash_route(RETIRED_MODEL, CURRENT_REVISION))
        self.assertFalse(quality.is_approved_flash_route("deepseek-v4-pro:cloud", CURRENT_REVISION))

    def test_portfolio_repair_accepts_current_release_endpoint(self):
        self.assertTrue(repair.is_approved_flash_route(CURRENT_MODEL, CURRENT_REVISION))
        self.assertFalse(repair.is_approved_flash_route(CURRENT_MODEL, "0" * 64))
        self.assertTrue(repair.is_current_live_flash_route(CURRENT_MODEL, CURRENT_REVISION))
        self.assertFalse(repair.is_current_live_flash_route(RETIRED_MODEL, RETIRED_REVISION))


if __name__ == "__main__":
    unittest.main()
