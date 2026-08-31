import sys
import tempfile
import unittest
from pathlib import Path

from scripts.autopilot_process import ChildTimeoutConfig, run_child_bounded


class AutopilotProcessTests(unittest.TestCase):
    def test_hung_child_is_terminated_and_returns_sanitized_timeout(self):
        """Catches a timeout path that leaves its owned child running."""
        with tempfile.TemporaryDirectory() as directory:
            result = run_child_bounded(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=Path(directory),
                timeout=ChildTimeoutConfig(total_seconds=2, grace_seconds=1),
                environment={},
            )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertLess(result.duration_seconds, 5)

    def test_timeout_terminates_descendant_in_owned_process_group(self):
        """Catches signaling only the direct child while its descendant survives."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            terminated = root / "terminated"
            descendant = (
                "import pathlib, signal, time\n"
                f"ready = pathlib.Path({str(ready)!r})\n"
                f"terminated = pathlib.Path({str(terminated)!r})\n"
                "def stop(*_):\n"
                "    terminated.write_text('terminated')\n"
                "    raise SystemExit\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "ready.write_text('ready')\n"
                "while True: time.sleep(0.1)\n"
            )
            parent = (
                "import pathlib, subprocess, sys, time\n"
                f"ready = pathlib.Path({str(ready)!r})\n"
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
                "while not ready.exists(): time.sleep(0.01)\n"
                "time.sleep(30)\n"
            )
            result = run_child_bounded(
                [sys.executable, "-c", parent],
                cwd=root,
                timeout=ChildTimeoutConfig(total_seconds=2, grace_seconds=1),
                environment={},
            )

            self.assertTrue(result.timed_out)
            for _ in range(50):
                if terminated.exists():
                    break
                __import__("time").sleep(0.02)
            self.assertEqual("terminated", terminated.read_text())

    def test_term_ignoring_child_is_killed_after_grace(self):
        """Catches a timeout implementation that never escalates past SIGTERM."""
        with tempfile.TemporaryDirectory() as directory:
            result = run_child_bounded(
                [
                    sys.executable,
                    "-c",
                    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
                ],
                cwd=Path(directory),
                timeout=ChildTimeoutConfig(total_seconds=2, grace_seconds=1),
                environment={},
            )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertLess(result.duration_seconds, 5)

    def test_child_stderr_is_not_retained_as_a_failure_payload(self):
        """Catches failure receipts that expose credentials emitted by a child."""
        with tempfile.TemporaryDirectory() as directory:
            result = run_child_bounded(
                [sys.executable, "-c", "import sys; sys.stderr.write('secret-token=abc123')"],
                cwd=Path(directory),
                timeout=ChildTimeoutConfig(total_seconds=2, grace_seconds=1),
                environment={},
            )

        self.assertEqual("child stderr suppressed", result.stderr)


if __name__ == "__main__":
    unittest.main()
