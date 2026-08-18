"""Tests for deterministic, local Fathom/Calendly meeting reconciliation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.meeting_reconciliation import (
    MeetingReconciliationError,
    MeetingSplit,
    reconcile_meetings,
    validate_meeting_splits,
)


ROOT = Path(__file__).resolve().parents[1]


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def fathom(**changes):
    value = {
        "recording_id": "f-1",
        "meeting_id": "event-1",
        "title": "Client review",
        "start": "2026-08-04T10:00:00Z",
        "end": "2026-08-04T10:37:00Z",
        "recorded_by_email": "vlad@example.test",
        "calendar_invitees": [
            {"email": "vlad@example.test"}, {"email": "client@example.test"}
        ],
        "share_url": "https://meet.example.test/recording/one",
        "summary": "Fathom summary",
        "transcript": [{"text": "Discussed launch", "offset_seconds": 0}],
        "source_digest": "sha256:" + "f" * 64,
    }
    value.update(changes)
    return value


def calendly(**changes):
    value = {
        "recording_id": "rec-1",
        "meeting_id": "event-1",
        "title": "Client review",
        "start": "2026-08-04T10:02:00Z",
        "end": "2026-08-04T10:39:00Z",
        "duration_seconds": 2220,
        "organizer": {"email": "vlad@example.test"},
        "participants": [{"email": "client@example.test"}],
        "join_url": "https://meet.example.test/recording/one",
        "summary": "Calendly summary",
        "transcript": [],
        "source_digest": "sha256:" + "c" * 64,
    }
    value.update(changes)
    return value


VLAD_IDS = {"vlad@example.test", "Vlad"}


class CanonicalMeetingTests(unittest.TestCase):
    def test_calendly_only_recording_remains_full_duration(self):
        result = reconcile_meetings([], [calendly()], vlad_identities=VLAD_IDS)
        self.assertEqual(1, len(result.meetings))
        self.assertEqual(37 * 60, result.meetings[0].duration_seconds)
        self.assertEqual(("calendly:rec-1",), result.meetings[0].source_ids)

    def test_shared_provider_identity_has_priority_over_other_join_data(self):
        result = reconcile_meetings(
            [fathom(recording_id="shared-recording", meeting_id="f-event")],
            [calendly(recording_id="shared-recording", meeting_id="c-event", join_url="")],
            vlad_identities=VLAD_IDS,
        )
        self.assertEqual(1, len(result.meetings))
        self.assertEqual(
            ("calendly:shared-recording", "fathom:shared-recording"),
            result.meetings[0].source_ids,
        )

    def test_explicit_meeting_identity_and_join_url_match(self):
        by_meeting = reconcile_meetings([fathom()], [calendly(join_url="")], vlad_identities=VLAD_IDS)
        by_join = reconcile_meetings(
            [fathom(meeting_id="", share_url="https://meet.example.test/same")],
            [calendly(meeting_id="", join_url="https://meet.example.test/same")],
            vlad_identities=VLAD_IDS,
        )
        self.assertEqual(1, len(by_meeting.meetings))
        self.assertEqual(1, len(by_join.meetings))

    def test_explicit_identity_with_conflicting_people_is_quarantined(self):
        """Changing either source's participant set must not merge a conflict."""
        result = reconcile_meetings(
            [fathom()],
            [calendly(participants=[{"email": "different-client@example.test"}])],
            vlad_identities=VLAD_IDS,
        )

        self.assertEqual(2, len(result.meetings))
        self.assertEqual("participant_conflict", result.exceptions[0]["reason"])
        self.assertEqual(
            {"fathom:f-1", "calendly:rec-1"},
            set(result.exceptions[0]["source_ids"]),
        )

    def test_same_email_with_extra_name_token_is_same_participant(self):
        """An optional display name cannot split an explicitly identified person."""
        for fathom_record, calendly_record in (
            (
                fathom(calendar_invitees=[
                    {"email": "vlad@example.test"},
                    {"email": "client@example.test", "name": "Client"},
                ]),
                calendly(participants=[{"email": "client@example.test"}]),
            ),
            (
                fathom(
                    meeting_id="", share_url="",
                    calendar_invitees=[
                        {"email": "vlad@example.test"},
                        {"email": "client@example.test", "name": "Client"},
                    ],
                ),
                calendly(
                    meeting_id="", join_url="",
                    participants=[{"email": "client@example.test"}],
                ),
            ),
        ):
            with self.subTest(fallback=not fathom_record["meeting_id"]):
                result = reconcile_meetings(
                    [fathom_record], [calendly_record], vlad_identities=VLAD_IDS
                )
                self.assertEqual(1, len(result.meetings), result.exceptions)
                self.assertEqual(
                    ("calendly:rec-1", "fathom:f-1"), result.meetings[0].source_ids
                )

    def test_same_name_with_disjoint_explicit_emails_is_participant_conflict(self):
        """A shared display name cannot override contradictory email identities."""
        result = reconcile_meetings(
            [fathom(calendar_invitees=[
                {"email": "vlad@example.test"},
                {"email": "fathom-client@example.test", "name": "Client"},
            ])],
            [calendly(participants=[{
                "email": "calendly-client@example.test", "name": "Client",
            }])],
            vlad_identities=VLAD_IDS,
        )

        self.assertEqual(2, len(result.meetings))
        self.assertEqual("participant_conflict", result.exceptions[0]["reason"])

    def test_participant_people_require_one_to_one_matching(self):
        """Two source people cannot both match one target through a weak name."""
        result = reconcile_meetings(
            [fathom(
                meeting_id="", share_url="",
                calendar_invitees=[
                    {"email": "vlad@example.test"},
                    {"name": "Client"},
                    {"name": "Client"},
                ],
            )],
            [calendly(
                meeting_id="", join_url="",
                participants=[{"name": "Client"}, {"name": "Other"}],
            )],
            vlad_identities=VLAD_IDS,
        )

        self.assertEqual(2, len(result.meetings))
        self.assertEqual("participant_conflict", result.exceptions[0]["reason"])

    def test_near_window_with_conflicting_people_is_quarantined(self):
        """A plausible overlap with different people is review evidence, not two silos."""
        result = reconcile_meetings(
            [fathom(meeting_id="", share_url="")],
            [calendly(
                meeting_id="", join_url="",
                participants=[{"email": "different-client@example.test"}],
            )],
            vlad_identities=VLAD_IDS,
        )

        self.assertEqual(2, len(result.meetings))
        self.assertEqual("participant_conflict", result.exceptions[0]["reason"])

    def test_fallback_duplicate_requires_same_participants_and_five_minute_boundaries(self):
        result = reconcile_meetings(
            [fathom(meeting_id="", share_url="")],
            [calendly(meeting_id="", join_url="")],
            vlad_identities=VLAD_IDS,
        )
        self.assertEqual(1, len(result.meetings))
        self.assertEqual(("calendly:rec-1", "fathom:f-1"), result.meetings[0].source_ids)

    def test_missing_participants_or_late_boundary_is_exception(self):
        for value in (
            calendly(meeting_id="", join_url="", participants=[]),
            calendly(
                meeting_id="", join_url="", start="2026-08-04T10:06:00Z", end="2026-08-04T10:43:00Z"
            ),
        ):
            with self.subTest(value=value):
                result = reconcile_meetings(
                    [fathom(meeting_id="", share_url="")], [value], vlad_identities=VLAD_IDS
                )
                self.assertEqual("duplicate_ambiguous", result.exceptions[0]["kind"])
                self.assertEqual(2, len(result.meetings))

    def test_multiple_qualified_candidates_is_an_exception(self):
        result = reconcile_meetings(
            [fathom(meeting_id="", share_url="")],
            [calendly(meeting_id="", join_url=""), calendly(recording_id="rec-2", meeting_id="", join_url="")],
            vlad_identities=VLAD_IDS,
        )
        self.assertEqual(3, len(result.meetings))
        self.assertEqual("duplicate_ambiguous", result.exceptions[0]["kind"])
        self.assertEqual("multiple_candidates", result.exceptions[0]["reason"])

    def test_higher_priority_ambiguity_quarantines_fallback_compatible_sources(self):
        result = reconcile_meetings(
            [fathom(meeting_id="", share_url="", provider_recording_id="shared-provider")],
            [
                calendly(
                    meeting_id="", join_url="", provider_recording_id="shared-provider",
                    participants=[{"email": "first@example.test"}],
                ),
                calendly(
                    recording_id="rec-2", meeting_id="", join_url="", provider_recording_id="shared-provider",
                    participants=[{"email": "second@example.test"}],
                ),
                calendly(recording_id="rec-3", meeting_id="", join_url=""),
            ],
            vlad_identities=VLAD_IDS,
        )
        self.assertEqual(4, len(result.meetings))
        self.assertTrue(any(item["reason"] == "multiple_candidates" for item in result.exceptions))
        self.assertFalse(any(len(meeting.source_ids) > 1 for meeting in result.meetings))

    def test_fallback_uses_participant_lists_not_organizer_or_recorder_identities(self):
        result = reconcile_meetings(
            [fathom(
                meeting_id="", share_url="", calendar_invitees=[{"email": "client@example.test"}],
                organizer={"email": "first@example.test"}, recorded_by_email="vlad@example.test",
            )],
            [calendly(
                meeting_id="", join_url="", participants=[{"email": "client@example.test"}],
                organizer={"email": "second@example.test"},
            )],
            vlad_identities=VLAD_IDS,
        )
        self.assertEqual(1, len(result.meetings))
        self.assertEqual((), result.exceptions)

    def test_timing_conflict_never_averages_or_trims_time(self):
        result = reconcile_meetings(
            [fathom()],
            [calendly(start="2026-08-04T10:08:00Z", end="2026-08-04T10:45:00Z")],
            vlad_identities=VLAD_IDS,
        )
        self.assertEqual(2, len(result.meetings))
        self.assertEqual("timing_conflict", result.exceptions[0]["reason"])
        self.assertEqual(2220, result.meetings[0].duration_seconds)

    def test_canonical_identity_and_document_are_order_independent_and_replay_exactly(self):
        second_fathom = fathom(
            recording_id="f-2", meeting_id="f-event-2", share_url="",
            calendar_invitees=[{"email": "vlad@example.test"}, {"email": "other@example.test"}],
        )
        second_calendly = calendly(
            recording_id="rec-2", meeting_id="c-event-2", join_url="",
            participants=[{"email": "other-calendly@example.test"}],
        )
        first = reconcile_meetings(
            [fathom(), second_fathom], [calendly(), second_calendly], vlad_identities=VLAD_IDS
        )
        second = reconcile_meetings(
            [second_fathom, fathom()], [second_calendly, calendly()], vlad_identities=VLAD_IDS
        )
        self.assertEqual(first.document(), second.document())
        self.assertEqual(first.meetings[0].canonical_id, second.meetings[0].canonical_id)
        self.assertEqual(first.meetings[0].source_digests, second.meetings[0].source_digests)
        self.assertEqual(
            json.dumps(first.document(), sort_keys=True, separators=(",", ":")).encode(),
            json.dumps(second.document(), sort_keys=True, separators=(",", ":")).encode(),
        )


class MeetingSplitTests(unittest.TestCase):
    def setUp(self):
        self.meeting = reconcile_meetings(
            [],
            [calendly(start="2026-08-04T10:00:00Z", end="2026-08-04T10:37:00Z")],
            vlad_identities=VLAD_IDS,
        ).meetings[0]

    def split(self, index, start, end, project, evidence_ids):
        return MeetingSplit(
            canonical_id=self.meeting.canonical_id,
            index=index,
            start=f"2026-08-04T{start}:00Z",
            end=f"2026-08-04T{end}:00Z",
            route={"project_name": project, "task_name": "Meeting"},
            evidence_ids=evidence_ids,
        )

    def test_timestamped_split_covers_full_meeting_without_overlap(self):
        splits = validate_meeting_splits(self.meeting, [
            self.split(0, "10:00", "10:20", "Client A", ("transcript:0-1200",)),
            self.split(1, "10:20", "10:37", "Client B", ("transcript:1200-2220",)),
        ])
        self.assertEqual(37, sum(item.duration_minutes for item in splits))

    def test_untimestamped_share_is_rejected_even_when_human_approved(self):
        with self.assertRaisesRegex(MeetingReconciliationError, "timestamped boundary"):
            validate_meeting_splits(self.meeting, [
                self.split(0, "10:00", "10:20", "Client A", ()),
                self.split(1, "10:20", "10:37", "Client B", ()),
            ])

    def test_split_rejects_gap_or_reused_route(self):
        with self.assertRaisesRegex(MeetingReconciliationError, "cover the full meeting"):
            validate_meeting_splits(self.meeting, [
                self.split(0, "10:00", "10:15", "Client A", ("transcript:0-900",)),
                self.split(1, "10:20", "10:37", "Client B", ("transcript:1200-2220",)),
            ])
        with self.assertRaisesRegex(MeetingReconciliationError, "distinct"):
            validate_meeting_splits(self.meeting, [
                self.split(0, "10:00", "10:20", "Client A", ("transcript:0-1200",)),
                self.split(1, "10:20", "10:37", "Client A", ("transcript:1200-2220",)),
            ])


class ReconciliationCliTests(unittest.TestCase):
    def _write(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")

    def test_cli_writes_schema_document_and_preserves_output_on_unverified_input(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fathom_path = base / "fathom.json"
            calendly_path = base / "calendly.json"
            manifest_path = base / "period-manifest.json"
            output_path = base / "meeting-reconciliation.json"
            fathom_source = {"status": "ok", "complete": True, "meetings": [fathom()]}
            signed_calendly = calendly()
            signed_calendly["source_digest"] = digest(
                {key: value for key, value in signed_calendly.items() if key != "source_digest"}
            )
            calendly_source = {"status": "ok", "complete": True, "recordings": [signed_calendly]}
            self._write(fathom_path, fathom_source)
            self._write(calendly_path, calendly_source)
            manifest = {
                "vlad_identities": ["vlad@example.test"],
                "artifacts": {"fathom": {"path": "fathom.json", "digest": "sha256:" + hashlib.sha256(fathom_path.read_bytes()).hexdigest()}},
            }
            self._write(manifest_path, manifest)
            command = [
                sys.executable, str(ROOT / "scripts" / "meeting_reconciliation.py"),
                "--period-manifest", str(manifest_path), "--fathom-from-manifest",
                "--calendly", str(calendly_path), "--algorithm", "meeting-dedup/v1",
                "--tolerance-seconds", "300", "--output", str(output_path),
            ]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            document = json.loads(output_path.read_text())
            self.assertEqual("meeting-reconciliation/v1", document["schema_version"])
            self.assertEqual(1, len(document["meetings"]))
            original = output_path.read_bytes()

            self._write(
                fathom_path,
                {"status": "ok", "complete": True, "meetings": [fathom(title="tampered Fathom")]},
            )
            failed_manifest = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(0, failed_manifest.returncode)
            self.assertEqual(original, output_path.read_bytes())
            self._write(fathom_path, fathom_source)

            changed = calendly_source["recordings"][0] | {"title": "tampered"}
            self._write(calendly_path, {"status": "ok", "complete": True, "recordings": [changed]})
            failed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(original, output_path.read_bytes())

    def test_cli_normalizes_local_minute_fathom_artifact_with_manifest_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fathom_path = base / "fathom.json"
            calendly_path = base / "calendly.json"
            manifest_path = base / "period-manifest.json"
            output_path = base / "meeting-reconciliation.json"
            fathom_source = {
                "status": "ok", "complete": True,
                "meetings": [{
                    "recording_id": "f-local", "title": "Local Fathom recording",
                    "start": "2026-08-04 13:00", "end": "2026-08-04 13:37",
                    "recorded_by_email": "vlad@example.test",
                    "calendar_invitees": [
                        {"email": "vlad@example.test"}, {"email": "client@example.test"},
                    ],
                    "share_url": "", "summary": "Local Fathom summary", "transcript": [],
                }],
            }
            signed_calendly = calendly(recording_id="rec-local", meeting_id="", join_url="")
            signed_calendly["source_digest"] = digest(
                {key: value for key, value in signed_calendly.items() if key != "source_digest"}
            )
            self._write(fathom_path, fathom_source)
            self._write(calendly_path, {"status": "ok", "complete": True, "recordings": [signed_calendly]})
            manifest = {
                "timezone": "Europe/Bucharest", "vlad_identities": ["vlad@example.test"],
                "artifacts": {"fathom": {"path": "fathom.json", "digest": "sha256:" + hashlib.sha256(fathom_path.read_bytes()).hexdigest()}},
            }
            self._write(manifest_path, manifest)
            command = [
                sys.executable, str(ROOT / "scripts" / "meeting_reconciliation.py"),
                "--period-manifest", str(manifest_path), "--fathom-from-manifest",
                "--calendly", str(calendly_path), "--output", str(output_path),
            ]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            document = json.loads(output_path.read_text())
            self.assertEqual(1, len(document["meetings"]))
            self.assertEqual("2026-08-04T10:00:00Z", document["meetings"][0]["start"])
            original = output_path.read_bytes()

            self._write(manifest_path, manifest | {"timezone": "Not/A_Timezone"})
            failed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(original, output_path.read_bytes())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
