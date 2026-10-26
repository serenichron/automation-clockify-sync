"""Historical runtimes are vouched for only by the archived release that recorded them."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle
from scripts import clockify_selected_delivery_adoption as selected
from scripts import clockify_native_pending_review_availability as native
from scripts import clockify_pending_review_selection as pending

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "historical_release_tool", ROOT / "ops/systemd/user/clockify_review_cycle_release.py")
release_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_tool)
PIPELINE = "scripts/" + Path(pending.pipeline.__file__).name
ALLOCATOR = "scripts/" + Path(pending.allocator.__file__).name
CONSUMER = "scripts/" + Path(native.__file__).name


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def archive_release(releases: Path, git_sha: str, files: dict[str, bytes]) -> Path:
    """Build an immutable release exactly as materialize() seals it."""
    release = releases / git_sha
    for relative, data in {"routing.json": b"{}", **files}.items():
        (release / relative).parent.mkdir(parents=True, exist_ok=True)
        (release / relative).write_bytes(data)
    release_tool._make_payload_read_only(release)
    identity = release / release_tool.IDENTITY_NAME
    release.chmod(0o755)
    identity.touch(mode=0o600)
    release.chmod(0o555)
    manifest = release_tool._tree_manifest(release)
    release.chmod(0o755)
    identity.write_text(json.dumps({
        "schema_version": release_tool.IDENTITY_SCHEMA, "git_sha": git_sha, "root": str(release),
        "routing_sha256": hashlib.sha256(b"{}").hexdigest(),
        "tree_digest": release_tool._manifest_digest(manifest), "tree_manifest": manifest,
    }, sort_keys=True, separators=(",", ":")) + "\n")
    identity.chmod(0o444)
    release.chmod(0o555)
    release_tool._identity(release, git_sha)
    return release


class ArchivedReleaseFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.releases = Path(temporary.name).resolve() / "releases"
        self.releases.mkdir(mode=0o700)
        self.addCleanup(lambda: [p.chmod(0o700) for p in self.releases.rglob("*") if p.is_dir()])
        self.old_pipeline, self.consumer = b"historical pipeline bytes", b"historical consumer bytes"
        self.old = archive_release(self.releases, "a" * 40, {
            PIPELINE: self.old_pipeline, ALLOCATOR: b"allocator bytes", CONSUMER: self.consumer})
        self.current = archive_release(self.releases, "b" * 40, {
            PIPELINE: b"current pipeline bytes", ALLOCATOR: b"allocator bytes", CONSUMER: b"current consumer"})
        self.config = {"root": str(self.current)}
        self.recorded = {"native_consumer_runtime": {
            "path": str(self.old / CONSUMER), "sha256": sha(self.consumer)}}


class HistoricalReleaseAuthenticationTests(ArchivedReleaseFixture):
    def test_recorded_runtime_release_is_authenticated_from_its_immutable_manifest(self):
        archived = cycle._historical_runtime_release(self.config, self.recorded)
        self.assertEqual({"root": str(self.old), "git_sha": "a" * 40}, {k: archived[k] for k in ("root", "git_sha")})
        self.assertEqual(sha(self.old_pipeline), archived["files"][PIPELINE])
        self.assertEqual(sha(self.consumer), archived["files"][CONSUMER])

    def test_receipt_without_recorded_runtime_has_no_archived_authority(self):
        self.assertIsNone(cycle._historical_runtime_release(self.config, {"selected_minutes": 1}))

    def test_unsealed_or_foreign_directories_never_become_archived_authority(self):
        foreign = self.releases.parent / "foreign" / ("c" * 40) / "scripts"
        foreign.mkdir(parents=True)
        (foreign / Path(CONSUMER).name).write_bytes(self.consumer)
        outside = archive_release(self.releases.parent / "other-releases", "d" * 40, {CONSUMER: self.consumer})
        for path in (foreign / Path(CONSUMER).name, outside / CONSUMER):
            with self.subTest(path=path):
                recorded = {"native_consumer_runtime": {"path": str(path), "sha256": sha(self.consumer)}}
                self.assertIsNone(cycle._historical_runtime_release(self.config, recorded))

    def test_tampered_archived_release_tree_is_not_authority(self):
        target = self.old / PIPELINE
        target.parent.chmod(0o755); target.chmod(0o644)
        target.write_bytes(b"rewritten after archival")
        self.assertIsNone(cycle._historical_runtime_release(self.config, self.recorded))

    def test_recorded_runtime_digest_must_match_its_archived_release(self):
        self.recorded["native_consumer_runtime"]["sha256"] = sha(b"other consumer")
        with self.assertRaisesRegex(cycle.CycleError, "archived release"):
            cycle._historical_runtime_release(self.config, self.recorded)

    def test_runtime_locators_cannot_name_two_releases(self):
        self.recorded["consumer_runtime"] = {"path": str(self.current / CONSUMER), "sha256": sha(b"current consumer")}
        with self.assertRaisesRegex(cycle.CycleError, "one archived release"):
            cycle._historical_runtime_release(self.config, self.recorded)


class ProducerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.current = {k: {"path": "/current/" + k, "sha256": sha(k.encode() + b" current")}
                        for k in ("consumer", "pipeline", "allocator")}
        self.roles = {"consumer": {"path": "/tmp/repo/consumer", "sha256": native.REPEATED_WARNING_PRODUCER},
                      "pipeline": {"path": "/tmp/repo/pipeline", "sha256": sha(b"historical pipeline")},
                      "allocator": dict(self.current["allocator"])}
        self.archived = {"root": "/releases/" + "a" * 40, "git_sha": "a" * 40, "files": {
            PIPELINE: sha(b"historical pipeline"), ALLOCATOR: self.current["allocator"]["sha256"]}}

    def test_without_archive_companions_must_equal_current_runtime(self):
        with self.assertRaisesRegex(ValueError, "producer runtime differs"):
            native.producer_runtime(self.roles, self.current, None)
        self.roles["pipeline"] = dict(self.current["pipeline"])
        native.producer_runtime(self.roles, self.current, None)

    def test_archived_producer_release_vouches_for_its_exact_companion_bytes(self):
        self.assertEqual({"pipeline": sha(b"historical pipeline"), "allocator": self.current["allocator"]["sha256"]},
                         native.producer_runtime(self.roles, self.current, self.archived))

    def test_archived_release_is_exact_not_an_additional_allowance(self):
        self.roles["pipeline"] = dict(self.current["pipeline"])
        with self.assertRaisesRegex(ValueError, "producer runtime differs"):
            native.producer_runtime(self.roles, self.current, self.archived)
        del self.archived["files"][ALLOCATOR]
        self.roles["pipeline"]["sha256"] = sha(b"historical pipeline")
        with self.assertRaisesRegex(ValueError, "producer runtime differs"):
            native.producer_runtime(self.roles, self.current, self.archived)

    def test_original_repeated_warning_consumer_stays_fixed(self):
        self.roles["consumer"]["sha256"] = sha(b"another consumer")
        with self.assertRaisesRegex(ValueError, "producer runtime differs"):
            native.producer_runtime(self.roles, self.current, self.archived)


class VerificationScopeTests(ArchivedReleaseFixture):
    def test_selected_adoption_and_delivery_validate_with_the_recorded_release(self):
        seen = []
        def validate(config, *args, **kwargs):
            seen.append(config.get("_historical_runtime_release"))
            raise ValueError("stop after scope capture")
        with mock.patch.object(selected, "validate", side_effect=validate), \
             mock.patch.object(cycle, "_sheet_title", return_value="title"):
            with self.assertRaises(cycle.CycleError):
                cycle._verify_selected_delivery({**self.config, "monthly_sheet_title_template": "x"},
                    {"selected_delivery": {**self.recorded, "proofs": {}}}, "2026-10-07", "2026-10-08", {}, {})
        self.assertEqual("a" * 40, seen[0]["git_sha"])
        self.assertEqual(sha(self.old_pipeline), seen[0]["files"][PIPELINE])
        self.assertNotIn("_historical_runtime_release", self.config)

    def test_changed_consumer_bytes_are_accepted_only_from_the_authenticated_recording_release(self):
        current = {"native_consumer_runtime": {"path": str(self.current / CONSUMER), "sha256": sha(b"current consumer")}}
        archived = cycle._historical_runtime_release(self.config, self.recorded)
        self.assertEqual(self.recorded, cycle._selected_delivery_recorded_runtime(current, self.recorded, archived))
        with self.assertRaises(ValueError):
            cycle._selected_delivery_recorded_runtime(current, self.recorded, None)


if __name__ == "__main__":
    unittest.main()
