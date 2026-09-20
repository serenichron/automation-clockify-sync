from __future__ import annotations

import configparser
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
USER_SYSTEMD = ROOT / "ops/systemd/user"
SERVICE = USER_SYSTEMD / "clockify-review-cycle.service"
CANARY = USER_SYSTEMD / "clockify-review-cycle-canary.service"
CONFIG_EXAMPLE = USER_SYSTEMD / "clockify-review-cycle.config.example.json"
ENV_EXAMPLE = USER_SYSTEMD / "clockify-review-cycle.env.example"
OVERRIDE_EXAMPLE = USER_SYSTEMD / "clockify-review-cycle-override.env.example"
RELEASE_TOOL = USER_SYSTEMD / "clockify_review_cycle_release.py"


def unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


def repeated_directives(path: Path, name: str) -> list[str]:
    prefix = name + "="
    return [
        line[len(prefix):]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(prefix)
    ]


def load_release_tool():
    spec = importlib.util.spec_from_file_location("clockify_review_cycle_release", RELEASE_TOOL)
    if spec is None or spec.loader is None:
        raise AssertionError("release helper is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UserSystemdArtifactTests(unittest.TestCase):
    maxDiff = None

    def test_recurring_unit_is_user_scoped_hardened_and_sheet_capable(self) -> None:
        """Catches a root-only, broadly writable, or non-publishing recurring unit."""
        parser = unit(SERVICE)
        service = parser["Service"]

        self.assertNotIn("User", service)
        self.assertNotIn("Group", service)
        self.assertEqual("oneshot", service["Type"])
        self.assertEqual("/", service["WorkingDirectory"])
        self.assertEqual("no", service["Restart"])
        self.assertEqual("0077", service["UMask"])
        self.assertEqual("true", service["NoNewPrivileges"])
        self.assertEqual("true", service["PrivateTmp"])
        self.assertEqual("strict", service["ProtectSystem"])
        self.assertEqual("read-only", service["ProtectHome"])
        self.assertEqual("default.target", parser["Install"]["WantedBy"])

        environment = set(shlex.split(service["Environment"]))
        self.assertEqual(
            {
                "PYTHONUNBUFFERED=1",
                "GOOGLE_WORKSPACE_CLI_CONFIG_DIR=%h/.config/gws",
                "CLOCKIFY_AUTOPILOT_ROOT=%h/Work/automation-clockify-sync-releases/REPLACE_WITH_GIT_SHA",
                "CLOCKIFY_REVIEW_CYCLE_CONFIG=%h/.config/serenichron/clockify-review-cycle.REPLACE_WITH_GIT_SHA.json",
            },
            environment,
        )
        self.assertEqual(
            [
                "%h/.config/serenichron/clockify-review-cycle.env",
                "%h/.config/serenichron/precision-inference-client.env",
                "%h/.config/serenichron/clockify-review-cycle-override.env",
            ],
            repeated_directives(SERVICE, "EnvironmentFile"),
        )
        self.assertEqual(
            {
                "CLOCKIFY_ANALYZER_FALLBACK_URL",
                "CLOCKIFY_ANALYZER_FALLBACK_MODEL",
                "CLOCKIFY_ANALYZER_FALLBACK_API_KEY",
                "CLOCKIFY_ANALYZER_FALLBACK_TIMEOUT_SECONDS",
                "CLOCKIFY_ANALYZER_FALLBACK_REVISION",
            },
            set(shlex.split(service["UnsetEnvironment"])),
        )
        self.assertEqual(
            {
                "%h/Work/automation-clockify-sync/runs",
                "%h/Work/automation-clockify-sync/state",
                "%h/.config/gws",
            },
            set(repeated_directives(SERVICE, "ReadWritePaths")),
        )
        command = shlex.split(service["ExecStart"])
        self.assertEqual(
            [
                "/usr/bin/flock",
                "--nonblock",
                "%h/Work/automation-clockify-sync/state/autopilot-runner.lock",
                "/usr/bin/python3",
            ],
            command[:4],
        )
        self.assertIn(
            "${CLOCKIFY_AUTOPILOT_ROOT}/scripts/clockify_review_cycle.py",
            command,
        )
        self.assertIn("--enable-sheet-write", command)
        preflight = shlex.split(service["ExecStartPre"])
        self.assertEqual("/usr/bin/python3", preflight[0])
        self.assertIn("preflight", preflight)
        self.assertIn("%h/.config/gws", preflight)
        self.assertIn("%h/.config/serenichron/clockify-review-cycle.env", preflight)
        self.assertIn("%h/.config/serenichron/precision-inference-client.env", preflight)
        self.assertIn("%h/.config/serenichron/clockify-review-cycle-override.env", preflight)
        service_text = SERVICE.read_text(encoding="utf-8")
        self.assertNotIn("/etc/", service_text)
        self.assertNotIn("/var/lib/", service_text)
        self.assertNotIn("ReadWritePaths=%h\n", service_text)

    def test_canary_is_plan_only_and_never_install_enrolled(self) -> None:
        """Catches a canary that can publish or auto-activate."""
        parser = unit(CANARY)
        service = parser["Service"]
        command = shlex.split(service["ExecStart"])

        self.assertNotIn("Install", parser)
        self.assertNotIn("User", service)
        self.assertNotIn("Group", service)
        self.assertNotIn("--enable-sheet-write", command)
        self.assertEqual(
            [
                "/usr/bin/flock",
                "--nonblock",
                "%h/Work/automation-clockify-sync/state/autopilot-runner.lock",
                "/usr/bin/python3",
            ],
            command[:4],
        )
        self.assertEqual(
            repeated_directives(SERVICE, "EnvironmentFile"),
            repeated_directives(CANARY, "EnvironmentFile"),
        )
        self.assertEqual(
            repeated_directives(SERVICE, "ReadWritePaths"),
            repeated_directives(CANARY, "ReadWritePaths"),
        )
        self.assertEqual(service["ProtectSystem"], "strict")
        self.assertEqual(service["ProtectHome"], "read-only")
        self.assertEqual(
            shlex.split(unit(SERVICE)["Service"]["ExecStartPre"]),
            shlex.split(service["ExecStartPre"]),
        )
        self.assertFalse(any(USER_SYSTEMD.glob("*.timer")))

    def test_user_config_and_environment_examples_are_private_path_contracts(self) -> None:
        """Catches release drift, copied credentials, or root-owned runtime paths."""
        document = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        release = "/home/blackthorne/Work/automation-clockify-sync-releases/REPLACE_WITH_GIT_SHA"

        self.assertEqual(release, document["root"])
        self.assertEqual(release + "/routing.json", document["routing"])
        self.assertEqual(
            "/home/blackthorne/Work/automation-clockify-sync/runs",
            document["runs_dir"],
        )
        self.assertEqual(
            "/home/blackthorne/Work/automation-clockify-sync/state",
            document["state_dir"],
        )
        self.assertNotIn("/etc/", json.dumps(document))
        self.assertNotIn("/var/lib/", json.dumps(document))

        env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("CLOCKIFY_ANALYZER_PRIMARY_MODEL=deepseek-v4-flash:cloud", env_text)
        self.assertNotIn("CLOCKIFY_ANALYZER_FALLBACK_", env_text)
        self.assertNotIn("OPENAI_API_KEY=", env_text)
        self.assertNotIn("CF_ACCESS_CLIENT_SECRET=", env_text)
        override_lines = [
            line for line in OVERRIDE_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(
            [
                "CLOCKIFY_AUTOPILOT_ROOT=%h/Work/automation-clockify-sync-releases/REPLACE_WITH_GIT_SHA",
                "CLOCKIFY_REVIEW_CYCLE_CONFIG=%h/.config/serenichron/clockify-review-cycle.REPLACE_WITH_GIT_SHA.json",
            ],
            override_lines,
        )


class UserSystemdReleaseToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = load_release_tool()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def git(self, repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def repository(self) -> tuple[Path, str]:
        source = self.root / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "config", "user.name", "Fixture")
        self.git(source, "config", "user.email", "fixture@example.invalid")
        (source / "routing.json").write_text('{"workspace_id":"w"}\n', encoding="utf-8")
        scripts = source / "scripts"
        scripts.mkdir()
        (scripts / "clockify_review_cycle.py").write_text("# fixture\n", encoding="utf-8")
        self.git(source, "add", "routing.json", "scripts/clockify_review_cycle.py")
        self.git(source, "commit", "-qm", "fixture")
        return source, self.git(source, "rev-parse", "HEAD")

    def config(self, release: Path, sha: str, name: str) -> Path:
        path = self.root / "config" / f"{name}.{sha}.json"
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text(
            json.dumps({
                "root": str(release),
                "routing": str(release / "routing.json"),
            }) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def test_materialize_binds_exact_commit_and_publishes_directory_atomically(self) -> None:
        """Catches a mutable working tree or wrong commit becoming a release."""
        source, sha = self.repository()
        releases = self.root / "releases"

        release = self.tool.materialize(source, releases, sha)

        self.assertEqual(releases / sha, release)
        identity = json.loads((release / ".clockify-release.json").read_text())
        self.assertEqual("clockify-user-release/v1", identity["schema_version"])
        self.assertEqual(sha, identity["git_sha"])
        self.assertEqual(str(release), identity["root"])
        self.assertRegex(identity["tree_digest"], r"^[0-9a-f]{64}$")
        manifest_paths = [entry["path"] for entry in identity["tree_manifest"]]
        self.assertEqual(sorted(manifest_paths), manifest_paths)
        self.assertEqual(".", manifest_paths[0])
        self.assertNotIn(".clockify-release.json", manifest_paths)
        self.assertIn("routing.json", manifest_paths)
        self.assertIn("scripts/clockify_review_cycle.py", manifest_paths)
        self.assertEqual(
            '{"workspace_id":"w"}\n',
            (release / "routing.json").read_text(encoding="utf-8"),
        )
        self.assertFalse(any(path.name.startswith(".materialize-") for path in releases.iterdir()))
        self.assertFalse(stat.S_IMODE((release / "routing.json").stat().st_mode) & 0o222)

    def test_existing_release_reuse_rejects_tree_add_delete_content_and_mode_drift(self) -> None:
        """Catches code drift hidden behind an unchanged routing digest and restored mode."""
        source, sha = self.repository()
        releases = self.root / "releases"
        release = self.tool.materialize(source, releases, sha)
        coordinator = release / "scripts" / "clockify_review_cycle.py"
        routing = release / "routing.json"
        original_coordinator = coordinator.read_bytes()
        original_routing = routing.read_bytes()

        coordinator.chmod(0o644)
        coordinator.write_text("# modified coordinator\n", encoding="utf-8")
        coordinator.chmod(0o444)
        with self.assertRaisesRegex(ValueError, "tree"):
            self.tool.materialize(source, releases, sha)
        coordinator.chmod(0o644)
        coordinator.write_bytes(original_coordinator)
        coordinator.chmod(0o444)

        release.chmod(0o755)
        added = release / "unexpected.py"
        added.write_text("# unexpected\n", encoding="utf-8")
        added.chmod(0o444)
        release.chmod(0o555)
        with self.assertRaisesRegex(ValueError, "tree"):
            self.tool.materialize(source, releases, sha)
        release.chmod(0o755)
        added.unlink()
        release.chmod(0o555)

        release.chmod(0o755)
        routing.unlink()
        release.chmod(0o555)
        with self.assertRaisesRegex(ValueError, "tree"):
            self.tool.materialize(source, releases, sha)
        release.chmod(0o755)
        routing.write_bytes(original_routing)
        routing.chmod(0o444)
        release.chmod(0o555)

        routing.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "tree"):
            self.tool.materialize(source, releases, sha)
        routing.chmod(0o444)
        self.assertEqual(release, self.tool.materialize(source, releases, sha))

    def test_atomic_activation_rejects_mismatch_and_rollback_restores_old_release(self) -> None:
        """Catches a partial switch or rollback to a config for another release."""
        source, first_sha = self.repository()
        (source / "routing.json").write_text('{"workspace_id":"w2"}\n', encoding="utf-8")
        self.git(source, "add", "routing.json")
        self.git(source, "commit", "-qm", "second")
        second_sha = self.git(source, "rev-parse", "HEAD")
        releases = self.root / "releases"
        first = self.tool.materialize(source, releases, first_sha)
        second = self.tool.materialize(source, releases, second_sha)
        first_config = self.config(first, first_sha, "cycle")
        second_config = self.config(second, second_sha, "cycle")
        override = self.root / "config" / "clockify-review-cycle-override.env"

        self.tool.activate(second, second_sha, second_config, override)
        active = override.read_text(encoding="utf-8")
        self.assertIn(f"CLOCKIFY_AUTOPILOT_ROOT={second}", active)
        self.assertIn(f"CLOCKIFY_REVIEW_CYCLE_CONFIG={second_config}", active)
        self.assertEqual(0o600, stat.S_IMODE(override.stat().st_mode))

        mismatched = self.config(first, first_sha, "mismatched")
        mismatch_document = json.loads(mismatched.read_text(encoding="utf-8"))
        mismatch_document["routing"] = str(second / "routing.json")
        mismatched.write_text(json.dumps(mismatch_document), encoding="utf-8")
        mismatched.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "routing"):
            self.tool.activate(first, first_sha, mismatched, override)
        self.assertEqual(active, override.read_text(encoding="utf-8"))

        self.tool.activate(first, first_sha, first_config, override)
        rolled_back = override.read_text(encoding="utf-8")
        self.assertIn(f"CLOCKIFY_AUTOPILOT_ROOT={first}", rolled_back)
        self.assertIn(f"CLOCKIFY_REVIEW_CYCLE_CONFIG={first_config}", rolled_back)
        self.assertNotEqual(active, rolled_back)

    def test_runtime_preflight_enforces_private_file_and_gws_permissions(self) -> None:
        """Catches a user unit starting with exposed credentials or symlinked GWS state."""
        source, sha = self.repository()
        release = self.tool.materialize(source, self.root / "releases", sha)
        config = self.config(release, sha, "cycle")
        private = self.root / "private"
        private.mkdir(mode=0o700)
        environment = private / "clockify-review-cycle.env"
        credential = private / "precision-inference-client.env"
        override = private / "clockify-review-cycle-override.env"
        for path in (environment, credential):
            path.write_text("SAFE_FIXTURE=value\n", encoding="utf-8")
            path.chmod(0o600)
        self.tool.activate(release, sha, config, override)
        gws = self.root / "gws"
        gws.mkdir(mode=0o700)
        token = gws / "fixture"
        token.write_text("not-a-real-secret\n", encoding="utf-8")
        token.chmod(0o600)
        public_directory = gws / "public-metadata"
        public_directory.mkdir(mode=0o755)
        public_file = public_directory / "non-secret.json"
        public_file.write_text("{}\n", encoding="utf-8")
        public_file.chmod(0o644)

        identity = self.tool.verify_runtime(
            release, config, environment, credential, override, gws
        )
        self.assertEqual(sha, identity["git_sha"])

        environment.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            self.tool.verify_runtime(
                release, config, environment, credential, override, gws
            )
        environment.chmod(0o600)
        link = gws / "link"
        link.symlink_to(token)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.tool.verify_runtime(
                release, config, environment, credential, override, gws
            )
        link.unlink()

        token.chmod(0o620)
        with self.assertRaisesRegex(ValueError, "group/other writable"):
            self.tool.verify_runtime(
                release, config, environment, credential, override, gws
            )
        token.chmod(0o600)
        fifo = gws / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(ValueError, "special"):
            self.tool.verify_runtime(
                release, config, environment, credential, override, gws
            )
        fifo.unlink()
        with mock.patch.object(self.tool.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "owned"):
                self.tool._private_tree(gws, "GWS directory")

    def test_release_and_private_parents_must_be_user_owned_and_not_writable_by_others(self) -> None:
        """Catches atomic publication through adversarial world-writable parents."""
        source, sha = self.repository()
        releases = self.root / "releases"
        releases.mkdir(mode=0o700)
        releases.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "group/other writable"):
            self.tool.materialize(source, releases, sha)
        releases.chmod(0o700)
        release = self.tool.materialize(source, releases, sha)
        config = self.config(release, sha, "cycle")
        config.parent.chmod(0o777)
        override = config.parent / "clockify-review-cycle-override.env"
        with self.assertRaisesRegex(ValueError, "group/other writable"):
            self.tool.activate(release, sha, config, override)
        config.parent.chmod(0o700)
        self.tool.activate(release, sha, config, override)

        private = self.root / "private-parent"
        private.mkdir(mode=0o700)
        environment = private / "clockify-review-cycle.env"
        credential = private / "precision-inference-client.env"
        for path in (environment, credential):
            path.write_text("SAFE_FIXTURE=value\n", encoding="utf-8")
            path.chmod(0o600)
        private_override = private / "clockify-review-cycle-override.env"
        self.tool.activate(release, sha, config, private_override)
        gws = self.root / "secure-gws"
        gws.mkdir(mode=0o700)
        private.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "group/other writable"):
            self.tool.verify_runtime(
                release, config, environment, credential, private_override, gws
            )

    def test_runtime_sha_is_external_and_identity_file_is_immutable_owned_regular(self) -> None:
        """Catches trusting a rewritten identity SHA or unsafe identity inode."""
        source, sha = self.repository()
        releases = self.root / "releases"
        release = self.tool.materialize(source, releases, sha)
        config = self.config(release, sha, "cycle")
        private = self.root / "identity-private"
        private.mkdir(mode=0o700)
        environment = private / "clockify-review-cycle.env"
        credential = private / "precision-inference-client.env"
        override = private / "clockify-review-cycle-override.env"
        for path in (environment, credential):
            path.write_text("SAFE_FIXTURE=value\n", encoding="utf-8")
            path.chmod(0o600)
        self.tool.activate(release, sha, config, override)
        gws = self.root / "identity-gws"
        gws.mkdir(mode=0o700)
        identity_path = release / ".clockify-release.json"
        original_identity = identity_path.read_bytes()

        identity_path.chmod(0o644)
        rewritten = json.loads(original_identity)
        rewritten["git_sha"] = "0" * 40
        identity_path.write_text(
            json.dumps(rewritten, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        identity_path.chmod(0o444)
        with self.assertRaisesRegex(ValueError, "SHA"):
            self.tool.verify_runtime(
                release, config, environment, credential, override, gws
            )
        identity_path.chmod(0o644)
        identity_path.write_bytes(original_identity)
        identity_path.chmod(0o444)

        identity_path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, "0444"):
            self.tool.verify_runtime(
                release, config, environment, credential, override, gws
            )
        identity_path.chmod(0o444)

        with mock.patch.object(self.tool.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "owned"):
                self.tool._identity_document(identity_path)

        identity_target = self.root / "identity-target.json"
        identity_target.write_bytes(original_identity)
        identity_target.chmod(0o444)
        release.chmod(0o755)
        identity_path.unlink()
        identity_path.symlink_to(identity_target)
        release.chmod(0o555)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.tool.verify_runtime(
                release, config, environment, credential, override, gws
            )
        release.chmod(0o755)
        identity_path.unlink()
        identity_path.write_bytes(original_identity)
        identity_path.chmod(0o444)
        release.chmod(0o555)

        wrong_config = json.loads(config.read_text(encoding="utf-8"))
        wrong_config["root"] = str(releases / ("f" * 40))
        config.write_text(json.dumps(wrong_config) + "\n", encoding="utf-8")
        config.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "config root"):
            self.tool.verify_runtime(
                release, config, environment, credential, override, gws
            )

        moved = releases / ("1" * 40)
        release.rename(moved)
        moved_identity = moved / ".clockify-release.json"
        moved_identity.chmod(0o644)
        moved_document = json.loads(moved_identity.read_text(encoding="utf-8"))
        moved_document["root"] = str(moved)
        moved_identity.write_text(
            json.dumps(moved_document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        moved_identity.chmod(0o444)
        moved_config = self.config(moved, sha, "moved")
        with self.assertRaisesRegex(ValueError, "SHA"):
            self.tool.activate(moved, sha, moved_config, override)


if __name__ == "__main__":
    unittest.main()
