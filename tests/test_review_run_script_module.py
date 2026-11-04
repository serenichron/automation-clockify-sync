from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class ReviewRunScriptModuleTest(unittest.TestCase):
    def test_script_execution_shares_configured_runs_root_with_lazy_importers(self):
        # clockify_source_debt_recover imports clockify_review_run lazily to
        # verify a derived recovery parent. When the review run is executed as
        # a script, that import must reach the running module, whose RUNS was
        # bound by --runs-root, not a second copy rooted in the release.
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = (Path(tmp) / "runs").resolve()
            runs_root.mkdir()
            probe = textwrap.dedent(f"""
                import runpy, sys
                sys.path.insert(0, {str(SCRIPTS)!r})
                sys.argv = ["clockify_review_run.py", "--runs-root", {str(runs_root)!r},
                            "--recover-source", "peer/macbook"]
                try:
                    runpy.run_path({str(SCRIPTS / "clockify_review_run.py")!r}, run_name="__main__")
                except SystemExit:
                    pass
                from scripts import clockify_review_run as packaged, clockify_source_debt_recover as recovery
                import clockify_review_run as bare
                print(packaged.RUNS)
                print(bare.RUNS)
                print(recovery.RUNS)
            """)
            completed = subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual([str(runs_root)] * 3, completed.stdout.split())


if __name__ == "__main__":
    unittest.main()
