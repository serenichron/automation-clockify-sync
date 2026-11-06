"""A master routing.json change must not strand the self-updater on a private routing overlay."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from test_historical_release_runtime import release_tool
import test_release_switch_followups as followups

# Borrow the fixture methods without re-collecting the borrowed TestCase.
ReleaseUpdates = followups.ReleaseUpdateTests
_git = followups._git

ACTOR_BINDING = {
    "source_type": "multica",
    "server_origin": "https://multica.example.invalid",
    "workspace_id": "workspace-1",
    "author_id": "author-1",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PrivateRoutingRebaseTests(unittest.TestCase):
    setUp = ReleaseUpdates.setUp
    private = ReleaseUpdates.private
    write_config = ReleaseUpdates.write_config
    update = ReleaseUpdates.update
    active = ReleaseUpdates.active
    commit = ReleaseUpdates.commit

    def bind_private_routing(self) -> Path:
        base_bytes = (self.current / "routing.json").read_bytes()
        overlay = {**json.loads(base_bytes),
                   "semantic_actor_contract": release_tool.ACTOR_CONTRACT,
                   "semantic_subject_binding": ACTOR_BINDING}
        routing = self.private(
            self.config_dir / f"clockify-review-cycle.{self.first}.routing.json",
            json.dumps(overlay, indent=2, sort_keys=True) + "\n",
        )
        self.config = self.write_config(self.first, {
            "root": str(self.current),
            "routing": str(routing),
            "private_routing": {"sha256": _digest(routing.read_bytes()), "base_sha256": _digest(base_bytes)},
            "state_dir": str(self.state),
        })
        release_tool._validated_config(self.current, self.config)
        return routing

    def commit_routing(self, document: dict) -> str:
        (self.work / "routing.json").write_text(json.dumps(document) + "\n")
        (self.work / "scripts/clockify_review_cycle.py").write_text("# routing change\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "--quiet", "-m", "routing change")
        _git(self.work, "push", "--quiet", "origin", "HEAD:master")
        return _git(self.work, "rev-parse", "HEAD")

    def test_changed_base_routing_is_rebased_under_the_same_actor_binding(self) -> None:
        self.bind_private_routing()
        second = self.commit_routing({"personal_session_rules": [{"id": "x"}], "session_routes": []})

        code, _out, err = self.update()

        self.assertEqual(0, code, err)
        self.assertNotIn("skipped", err)
        release = self.releases / second
        config = self.config_dir / f"clockify-review-cycle.{second}.json"
        self.assertIn(f"CLOCKIFY_AUTOPILOT_ROOT={release}\n", self.active())
        _path, document = release_tool._validated_config(release, config)
        rebased = json.loads(Path(document["routing"]).read_text())
        self.assertEqual(ACTOR_BINDING, rebased.pop("semantic_subject_binding"))
        self.assertEqual(release_tool.ACTOR_CONTRACT, rebased.pop("semantic_actor_contract"))
        self.assertEqual(json.loads((release / "routing.json").read_text()), rebased)
        self.assertEqual(0o600, Path(document["routing"]).stat().st_mode & 0o777)

    def test_unchanged_base_routing_keeps_the_private_bytes(self) -> None:
        routing = self.bind_private_routing()
        second = self.commit("v2")

        code, _out, err = self.update()

        self.assertEqual(0, code, err)
        copied = self.config_dir / f"clockify-review-cycle.{second}.routing.json"
        self.assertEqual(routing.read_bytes(), copied.read_bytes())
        self.assertIn(f"CLOCKIFY_AUTOPILOT_ROOT={self.releases / second}\n", self.active())

    def test_overlay_with_non_actor_edits_is_never_silently_rebased(self) -> None:
        # A hand-edited overlay that is not "base + actor fields" must not be laundered
        # into a valid one: the update stays skipped and the active release unchanged.
        self.bind_private_routing()
        before = self.active()
        routing = self.config_dir / f"clockify-review-cycle.{self.first}.routing.json"
        tampered = json.loads(routing.read_text())
        tampered["session_routes"] = [{"pattern": "local-only"}]
        routing.write_text(json.dumps(tampered) + "\n")
        # Re-bind the digest so only the structural "actor fields only" check can catch it.
        config = json.loads(self.config.read_text())
        config["private_routing"]["sha256"] = _digest(routing.read_bytes())
        self.config.write_text(json.dumps(config, indent=2) + "\n")
        second = self.commit_routing({"session_routes": []})

        code, _out, err = self.update()

        self.assertEqual(0, code)
        self.assertIn("skipped: config_invalid", err)
        self.assertEqual(before, self.active())
        record = json.loads(self.record.read_text()) if self.record.exists() else {"rejected": []}
        self.assertNotIn(second, [item["sha"] for item in record.get("rejected", [])])
        self.assertFalse((self.config_dir / f"clockify-review-cycle.{second}.routing.json").exists())


del ReleaseUpdates

if __name__ == "__main__":
    unittest.main()
