"""Parallel agent sessions are billed once per minute and within the caps.

Decided 2026-11-19: the same wall-clock minute is never billed to two
clients; billed time per client stays at or below 1.5 times the wall-clock
time of a day and 1.25 times that of a calendar month.
"""
from __future__ import annotations

import contextlib
import datetime as dt
from fractions import Fraction
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import evidence_ledger, work_accounting_pipeline as pipeline, work_allocator
import test_work_accounting_pipeline as fixtures


ZONE = dt.timezone(dt.timedelta(hours=3))
TSTP = "TST Prep Level 2"
SC = "Serenichron Level 2"
POLICY = work_allocator.BillingPolicy(project_clients={"bc17f7": "TST Prep", "775f9f": "Serenichron"})


def at(hour: int, minute: int = 0, *, day: int = 10) -> dt.datetime:
    return dt.datetime(2026, 7, day, hour, minute, tzinfo=ZONE)


def stamp(hour: int, minute: int = 0, *, day: int = 10) -> str:
    return at(hour, minute, day=day).isoformat(timespec="seconds")


def session(name: str, start: str, end: str):
    """One observed agent session span on a path no deterministic route claims."""
    return evidence_ledger.evidence_event(
        "codex_sessions_event",
        {"source_type": "codex_sessions", "source_id": f"{name}:event", "machine": "macbook",
         "session_id": name},
        observed_at=start,
        raw_source_span={"start": start, "end": end, "path": f"/tmp/parallel-billing/{name}.jsonl"},
        attributes={"role": "user", "kind": "message", "content": f"Completed {name}"},
    )


def existing(start: str, end: str, project_suffix: str, name: str = "existing-1"):
    return evidence_ledger.evidence_event(
        "clockify",
        {"source_type": "clockify", "source_id": name},
        observed_at=start,
        raw_source_span={"start": start, "end": end},
        attributes={"description": f"Existing {name}", "project_id_suffix": project_suffix, "billable": True},
    )


def analysis(*work: tuple[object, str, int]) -> dict:
    """One completed activity per (event, project, recommended minutes)."""
    activities = []
    for index, (event, project, minutes) in enumerate(work):
        activity = fixtures.analysis_for([event.evidence_id], recommended=minutes)["activities"][0]
        activity["object"] = f"parallel billing outcome {index}"
        activity["project_recommendation"] = {
            "name": project,
            "prefix": "TSTP" if project == TSTP else "SC",
            "tag_names": ["Technical development"] if project == TSTP else ["Processes"],
        }
        for span in activity["evidence_spans"]:
            span["start"], span["end"] = event.raw_source_span["start"], event.raw_source_span["end"]
        activities.append(activity)
    return {"activities": activities, "exceptions": [], "omissions": []}


def billed_rows(result: dict, events=()) -> list[tuple[str, dt.datetime, dt.datetime]]:
    """Everything billed after the run: proposals plus existing Clockify entries."""
    policy = work_allocator.billing_policy_from_routing(json.loads((fixtures.ROOT / "routing.json").read_text()))
    rows = [
        (policy.client_for_project(row["clockify_project_suffix"]),
         dt.datetime.fromisoformat(row["start"]), dt.datetime.fromisoformat(row["end"]))
        for row in result["proposals"] if row.get("billable", True)
    ]
    for event in events:
        if event.source_type == "clockify":
            rows.append((policy.client_for_project(event.attributes["project_id_suffix"]),
                         dt.datetime.fromisoformat(event.raw_source_span["start"]),
                         dt.datetime.fromisoformat(event.raw_source_span["end"])))
    return rows


def ratio(row: dict) -> Fraction:
    return Fraction(row["billed_seconds"], row["wall_seconds"])


class ExclusiveSharesTests(unittest.TestCase):
    def test_contested_time_is_split_evenly_between_equal_sessions(self):
        shares = work_allocator.exclusive_shares([
            work_allocator.BillingClaim("TST Prep", at(9), at(10)),
            work_allocator.BillingClaim("Serenichron", at(9), at(10)),
        ])

        self.assertEqual(1800, work_allocator.interval_seconds(shares.owned["TST Prep"]))
        self.assertEqual(1800, work_allocator.interval_seconds(shares.owned["Serenichron"]))
        self.assertEqual([], work_allocator.intersect_intervals(shares.owned["TST Prep"], shares.owned["Serenichron"]))
        self.assertEqual(3600, shares.contested_seconds)

    def test_contested_time_follows_each_sessions_estimated_effort(self):
        shares = work_allocator.exclusive_shares([
            work_allocator.BillingClaim("TST Prep", at(9), at(10), Fraction(3, 4)),
            work_allocator.BillingClaim("Serenichron", at(9), at(10), Fraction(1, 4)),
        ])

        self.assertEqual(45 * 60, work_allocator.interval_seconds(shares.owned["TST Prep"]))
        self.assertEqual(15 * 60, work_allocator.interval_seconds(shares.owned["Serenichron"]))

    def test_only_the_overlapping_part_is_split(self):
        shares = work_allocator.exclusive_shares([
            work_allocator.BillingClaim("TST Prep", at(9), at(10)),
            work_allocator.BillingClaim("Serenichron", at(9, 30), at(11)),
        ])

        self.assertEqual(((at(9), at(9, 45)),), shares.owned["TST Prep"])
        self.assertEqual(((at(9, 45), at(11)),), shares.owned["Serenichron"])
        self.assertEqual(1800, shares.contested_seconds)

    def test_rounding_is_carried_so_short_runs_stay_proportional(self):
        claims = []
        for index in range(10):
            start = at(9, index * 5)
            claims.append(work_allocator.BillingClaim("TST Prep", start, start + dt.timedelta(minutes=1)))
            claims.append(work_allocator.BillingClaim("Serenichron", start, start + dt.timedelta(minutes=1)))

        shares = work_allocator.exclusive_shares(claims)

        self.assertEqual(300, work_allocator.interval_seconds(shares.owned["TST Prep"]))
        self.assertEqual(300, work_allocator.interval_seconds(shares.owned["Serenichron"]))

    def test_another_clients_fixed_entry_takes_the_time_and_an_own_entry_does_not(self):
        shares = work_allocator.exclusive_shares(
            [work_allocator.BillingClaim("Serenichron", at(9), at(10)), work_allocator.BillingClaim("TST Prep", at(11), at(12))],
            [("TST Prep", at(9, 15), at(9, 45)), ("TST Prep", at(11), at(11, 30))],
        )

        self.assertEqual(((at(9), at(9, 15)), (at(9, 45), at(10))), shares.owned["Serenichron"])
        self.assertEqual(((at(11), at(12)),), shares.owned["TST Prep"])
        self.assertEqual({"Serenichron": 1800}, dict(shares.blocked_seconds))


class CapLedgerTests(unittest.TestCase):
    def test_time_that_adds_wall_clock_is_never_limited(self):
        ledger = work_allocator.CapLedger(POLICY)
        ledger.add("TST Prep", at(9), at(10))

        self.assertEqual(3600, ledger.acceptable_seconds("TST Prep", at(10), at(11)))

    def test_parallel_time_stops_at_the_day_cap(self):
        policy = work_allocator.BillingPolicy(month_multiplier=Fraction(3, 2))
        ledger = work_allocator.CapLedger(policy)
        ledger.add("TST Prep", at(9), at(10))

        self.assertEqual(30 * 60, ledger.acceptable_seconds("TST Prep", at(9), at(10)))

    def test_without_month_knowledge_every_day_also_holds_the_month_cap(self):
        ledger = work_allocator.CapLedger(POLICY)
        ledger.add("TST Prep", at(9), at(10))

        self.assertEqual(15 * 60, ledger.acceptable_seconds("TST Prep", at(9), at(10)))
        self.assertEqual(30 * 60, ledger.acceptable_seconds_day_only("TST Prep", at(9), at(10)))

    def test_caps_are_per_client(self):
        ledger = work_allocator.CapLedger(POLICY)
        ledger.add("TST Prep", at(9), at(10))

        self.assertEqual(3600, ledger.acceptable_seconds("Serenichron", at(9), at(10)))

    def test_a_day_may_reach_the_day_cap_while_the_month_has_room(self):
        ledger = work_allocator.CapLedger(POLICY, month_scope=work_allocator.MONTH_SCOPE_CUMULATIVE)
        ledger.add("TST Prep", at(9, day=1), at(11, day=1))
        ledger.add("TST Prep", at(9, day=2), at(10, day=2))

        # Day 2 alone: 60 wall minutes allow 30 parallel; the month allows (180 * 1.25) - 180 = 45.
        self.assertEqual(30 * 60, ledger.acceptable_seconds("TST Prep", at(9, day=2), at(10, day=2)))

    def test_the_month_cap_binds_before_the_day_cap_once_the_month_is_used_up(self):
        ledger = work_allocator.CapLedger(POLICY, month_scope=work_allocator.MONTH_SCOPE_CUMULATIVE)
        ledger.add("TST Prep", at(9, day=1), at(10, day=1))
        ledger.add("TST Prep", at(9, day=1), at(9, 15, day=1))
        ledger.add("TST Prep", at(9, day=2), at(10, day=2))

        # Month so far: 135 billed over 120 wall; 1.25 * 120 = 150 leaves 15 minutes.
        self.assertEqual(15 * 60, ledger.acceptable_seconds("TST Prep", at(9, day=2), at(10, day=2)))
        self.assertEqual(30 * 60, ledger.acceptable_seconds_day_only("TST Prep", at(9, day=2), at(10, day=2)))

    def test_measure_matches_the_timesheet_checker_definitions(self):
        measured = work_allocator.measure_billing([
            ("TST Prep", at(9), at(10)), ("TST Prep", at(9, 30), at(10, 30)),
            ("Serenichron", at(10), at(11)),
        ], POLICY)

        tst = next(row for row in measured["client_days"] if row["client"] == "TST Prep")
        self.assertEqual((7200, 5400), (tst["billed_seconds"], tst["wall_seconds"]))
        self.assertEqual(1800, measured["cross_client_seconds"])
        self.assertEqual([], measured["days_over_cap"])
        self.assertEqual(["TST Prep"], [row["client"] for row in measured["months_over_cap"]])


class CapEntriesTests(unittest.TestCase):
    def test_replayed_entries_end_exclusive_and_within_both_caps(self):
        entries = []
        for day in (1, 2, 3):
            for index in range(4):
                entries.append({"id": f"tst-{day}-{index}", "client": "TST Prep",
                                "start": at(9, day=day), "end": at(10, day=day)})
            entries.append({"id": f"sc-{day}", "client": "Serenichron",
                            "start": at(9, 30, day=day), "end": at(11, day=day)})

        replay = work_allocator.cap_entries(entries, POLICY)
        measured = work_allocator.measure_billing(
            [(row["client"], row["start"], row["end"]) for row in replay["entries"]], POLICY)

        self.assertEqual(0, measured["cross_client_seconds"])
        self.assertEqual([], measured["days_over_cap"])
        self.assertEqual([], measured["months_over_cap"])
        before = sum(int((row["end"] - row["start"]).total_seconds()) for row in entries)
        after = sum(int((row["end"] - row["start"]).total_seconds()) for row in replay["entries"])
        self.assertEqual(before - after, sum(replay["cut_seconds"].values()))
        self.assertGreater(replay["cut_seconds"]["cross_client"], 0)
        self.assertGreater(replay["cut_seconds"]["day_cap"], 0)


class ReplayToolTests(unittest.TestCase):
    def test_replay_reads_saved_entries_and_reports_what_the_rules_remove(self):
        from scripts import clockify_billing_caps_replay as tool

        def row(name, client, start, end, **extra):
            return {"id": name, "client": client, "start": start.isoformat(), "end": end.isoformat(),
                    "me": True, "billable": True, **extra}

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        fixtures.write_json(folder / "entries-2026-07.private.json", [
            row("a", "TST Prep", at(9), at(10)),
            row("b", "Serenichron", at(9), at(10)),
            row("c", "TST Prep", at(11), at(12)),
            row("d", "TST Prep", at(11), at(12)),
            row("someone-else", "TST Prep", at(9), at(10), me=False),
            row("unbilled", "TST Prep", at(9), at(10), billable=False),
            row("outside", "TST Prep", at(9, day=20), at(10, day=20)),
        ])
        report = folder / "replay.json"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = tool.main([str(folder), "--from", "2026-07-10", "--to", "2026-07-10", "--json", str(report)])

        self.assertEqual(0, code)
        result = json.loads(report.read_text())
        self.assertEqual(4, result["entries"])
        self.assertEqual(3600, result["before"]["cross_client_seconds"])
        self.assertEqual(0, result["after"]["cross_client_seconds"])
        # One contested hour is halved between the two clients.  TST Prep then
        # has 90 wall minutes: the day cap leaves 45 of its doubled hour and the
        # month cap, with this one day as the whole month, 22 whole minutes.
        self.assertEqual({"cross_client": 3600, "day_cap": 900, "month_cap": 1380}, result["cut_seconds"])
        self.assertIn("| Same minute billed to another client | 60 |", output.getvalue())

    def test_replay_refuses_routing_without_the_declaration(self):
        from scripts import clockify_billing_caps_replay as tool

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        routing = Path(temporary.name) / "routing.json"
        fixtures.write_json(routing, {"timezone": "Europe/Bucharest"})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, tool.main([temporary.name, "--from", "2026-07-10", "--to", "2026-07-10",
                                           "--routing", str(routing)]))


class RoutingDeclarationTests(unittest.TestCase):
    def test_the_guards_are_off_without_a_declaration(self):
        self.assertIsNone(work_allocator.billing_policy_from_routing({}))
        self.assertIsNone(work_allocator.billing_policy_from_routing(None))

    def test_an_empty_declaration_uses_the_decided_caps(self):
        policy = work_allocator.billing_policy_from_routing({"billing_caps": {}})

        self.assertEqual((Fraction(3, 2), Fraction(5, 4)), (policy.day_multiplier, policy.month_multiplier))
        self.assertEqual("project:abc123", policy.client_for_project("abc123"))
        self.assertEqual("", policy.client_for_project(""))
        self.assertEqual(Fraction(5, 4), policy.enforced_day_multiplier)

    def test_invalid_declarations_are_rejected(self):
        for declaration in ([], {"day_multiplier": 0.9}, {"month_multiplier": "many"},
                            {"project_clients": {"abc123": ""}}, {"unknown": 1}):
            with self.subTest(declaration=declaration):
                with self.assertRaises(work_allocator.BillingCapsError):
                    work_allocator.billing_policy_from_routing({"billing_caps": declaration})

    def test_every_routed_project_has_a_declared_client(self):
        routing = json.loads((fixtures.ROOT / "routing.json").read_text())
        policy = work_allocator.billing_policy_from_routing(routing)
        suffixes: set[str] = set()

        def walk(value):
            if isinstance(value, dict):
                if value.get("project_suffix"):
                    suffixes.add(str(value["project_suffix"]))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(routing)
        self.assertTrue(suffixes)
        self.assertEqual([], sorted(suffix for suffix in suffixes if suffix not in policy.project_clients))
        self.assertEqual("TST Prep", policy.client_for_project("bc17f7"))
        self.assertEqual("TST Prep", policy.client_for_project("319d88"))


class PipelineBillingCapTests(unittest.TestCase):
    maxDiff = None

    def run_pipeline(self, events, work, *, routing_path=None):
        _, result = fixtures.WorkAccountingPipelineTests.make_run(
            self, events, analysis(*work), routing_path=routing_path)
        return result

    def measured(self, result, events=()):
        policy = work_allocator.billing_policy_from_routing(json.loads((fixtures.ROOT / "routing.json").read_text()))
        return work_allocator.measure_billing(billed_rows(result, events), policy)

    def test_overlapping_sessions_for_two_clients_never_bill_the_same_minute(self):
        tst = session("tst", stamp(9), stamp(10))
        sc = session("sc", stamp(9), stamp(10))

        result = self.run_pipeline([tst, sc], [(tst, TSTP, 60), (sc, SC, 60)])

        measured = self.measured(result)
        self.assertEqual(0, measured["cross_client_seconds"])
        by_project: dict[str, int] = {}
        for row in result["proposals"]:
            by_project[row["client_project"]] = by_project.get(row["client_project"], 0) + row["duration_minutes"]
        self.assertEqual({TSTP: 30, SC: 30}, by_project)
        report = result["allocation"]["billing_caps"]
        self.assertEqual(60, report["contested_wall_minutes"])
        self.assertEqual(60, report["cut_minutes"]["cross_client"])

    def test_a_partial_overlap_keeps_each_clients_own_time_whole(self):
        tst = session("tst", stamp(9), stamp(10))
        sc = session("sc", stamp(9, 30), stamp(11))

        result = self.run_pipeline([tst, sc], [(tst, TSTP, 60), (sc, SC, 90)])

        self.assertEqual(0, self.measured(result)["cross_client_seconds"])
        by_project: dict[str, int] = {}
        for row in result["proposals"]:
            by_project[row["client_project"]] = by_project.get(row["client_project"], 0) + row["duration_minutes"]
        self.assertEqual({TSTP: 45, SC: 75}, by_project)

    def test_a_session_does_not_bill_time_another_client_already_has_in_clockify(self):
        booked = existing(stamp(9), stamp(10), "bc17f7")
        sc = session("sc", stamp(9), stamp(10))

        result = self.run_pipeline([booked, sc], [(sc, SC, 30)])

        self.assertEqual([], result["proposals"])
        self.assertEqual(0, self.measured(result, [booked])["cross_client_seconds"])
        self.assertEqual(
            ["billing cap: the observed time is billed to another client"],
            [row["reason"] for row in result["skipped"] if row["reason"].startswith("billing cap")],
        )
        self.assertEqual(30, result["allocation"]["billing_caps"]["cut_minutes"]["cross_client"])

    def test_one_client_day_stays_at_or_below_the_day_cap(self):
        sessions = [session(f"tst-{index}", stamp(9), stamp(10)) for index in range(3)]

        result = self.run_pipeline(sessions, [(item, TSTP, 60) for item in sessions])

        day = self.measured(result)["client_days"]
        self.assertEqual(1, len(day))
        self.assertEqual(3600, day[0]["wall_seconds"])
        self.assertLessEqual(ratio(day[0]), Fraction(3, 2))
        self.assertGreater(ratio(day[0]), 1, "parallel same-client work may still exceed wall-clock time")
        self.assertEqual(105, result["allocation"]["billing_caps"]["cut_minutes"]["caps"])

    def test_every_day_of_a_run_holds_the_month_multiplier_so_the_month_does_too(self):
        # The pipeline has no month-to-date state: it holds 1.25 on each day,
        # which bounds the month.  Month-to-date budgeting is CapLedgerTests'.
        events, work = [], []
        for day in (10, 11, 12):
            for index in range(2):
                item = session(f"tst-{day}-{index}", stamp(9, day=day), stamp(10, day=day))
                events.append(item)
                work.append((item, TSTP, 60))

        result = self.run_pipeline(events, work)

        measured = self.measured(result)
        self.assertEqual(3, len(measured["client_days"]))
        self.assertEqual([], measured["months_over_cap"])
        month = measured["client_months"][0]
        self.assertLessEqual(ratio(month), Fraction(5, 4))
        self.assertEqual(3 * 75 * 60, month["billed_seconds"])

    def test_a_session_over_the_same_clients_clockify_entry_stays_a_reviewed_overlap(self):
        # It may be the same work already posted, which only the credit steps
        # and the reviewer can tell; the report still shows the day over the cap.
        booked = existing(stamp(9), stamp(10), "bc17f7")
        tst = session("tst", stamp(9), stamp(10))

        result = self.run_pipeline([booked, tst], [(tst, TSTP, 60)])

        self.assertEqual([60], [row["duration_minutes"] for row in result["proposals"]])
        self.assertIn("existing_clockify_overlap",
                      [warning["type"] for warning in result["proposals"][0]["review_warnings"]])
        report = result["allocation"]["billing_caps"]
        self.assertEqual({"cross_client": 0, "caps": 0}, report["cut_minutes"])
        self.assertEqual(["TST Prep"], [row["client"] for row in report["remaining_violations"]["days_over_cap"]])

    def test_a_clockify_entry_on_an_undeclared_project_still_blocks_other_clients(self):
        # Whoever pays for that entry, it is not the session's client.
        booked = existing(stamp(9), stamp(10), "ffffff")
        sc = session("sc", stamp(9), stamp(10))

        result = self.run_pipeline([booked, sc], [(sc, SC, 30)])

        self.assertEqual([], result["proposals"])
        self.assertEqual(
            ["billing cap: the observed time is billed to another client"],
            [row["reason"] for row in result["skipped"] if row["reason"].startswith("billing cap")],
        )
        report = result["allocation"]["billing_caps"]
        self.assertEqual(30, report["cut_minutes"]["cross_client"])
        self.assertEqual(0, report["remaining_violations"]["cross_client_seconds"])

    def test_a_clockify_entry_without_a_project_bills_no_client_and_blocks_nobody(self):
        booked = existing(stamp(9), stamp(10), "")
        sc = session("sc", stamp(9), stamp(10))

        result = self.run_pipeline([booked, sc], [(sc, SC, 30)])

        self.assertEqual(30, sum(row["duration_minutes"] for row in result["proposals"]))

    def test_the_report_counts_an_overlap_with_an_undeclared_project_as_cross_client(self):
        policy = work_allocator.billing_policy_from_routing(
            json.loads((fixtures.ROOT / "routing.json").read_text()))
        block = {"kind": "existing_clockify", "project_id_suffix": "ffffff", "start": at(9), "end": at(10)}
        proposal = {"clockify_project_suffix": "775f9f", "client_project": SC,
                    "start": stamp(9, 30), "end": stamp(10), "billable": True}

        report = pipeline._billing_caps_report(
            policy, {"adjustments": [], "shares": work_allocator.exclusive_shares([])}, [proposal], [block])

        self.assertEqual(1800, report["remaining_violations"]["cross_client_seconds"])

    def test_dropping_a_rejected_activity_does_not_leave_its_parallel_time_above_the_cap(self):
        # A and C give the day 120 wall-clock minutes, which pays for B's 30
        # parallel minutes.  Once the reviewer's skip removes C, only 60 remain.
        first = session("tst-a", stamp(9), stamp(10))
        parallel = session("tst-b", stamp(9), stamp(10))
        later = session("tst-c", stamp(10), stamp(11))
        work = analysis((first, TSTP, 60), (parallel, TSTP, 30), (later, TSTP, 60))
        run_dir, initial = fixtures.WorkAccountingPipelineTests.make_run(self, [first, parallel, later], work)
        self.assertEqual(150, sum(row["duration_minutes"] for row in initial["proposals"]))
        rejected = next(row for row in initial["proposals"] if row["start"] >= stamp(10))
        corrections_path = run_dir.parent.parent / "review-corrections.jsonl"
        fixtures.WorkAccountingPipelineTests.append_correction(self, corrections_path, rejected, "skip")

        rerun = pipeline.run_accounting(
            run_dir, root=fixtures.ROOT, analysis_fixture=run_dir.parent.parent / "analysis.json",
            corrections_path=corrections_path,
        )

        self.assertFalse(any(row["activity_id"] == rejected["activity_id"] for row in rerun["proposals"]))
        day = self.measured(rerun)["client_days"][0]
        self.assertEqual((75 * 60, 3600), (day["billed_seconds"], day["wall_seconds"]))
        report = rerun["allocation"]["billing_caps"]
        self.assertEqual(15, report["cut_minutes"]["caps"])
        self.assertEqual([], report["remaining_violations"]["days_over_cap"])
        for row in rerun["proposals"]:
            self.assertEqual(row["duration_seconds"], row["duration_minutes"] * 60)
            self.assertEqual(row["end"], row["provenance"]["burst_end"])
            for warning in row["review_warnings"]:
                fixtures.sheet_publisher._validate_review_warning(warning, {})
                if "overlap_end" in warning:
                    self.assertLessEqual(warning["overlap_end"], row["end"])
        recovery = rerun["allocation"]["capacity_recoveries"][0]
        self.assertEqual(15, recovery["recovered_minutes"])
        saved = {row["activity_id"]: row for row in rerun["allocation"]["evidence"]}
        self.assertEqual(15, saved[recovery["activity_id"]]["effort"]["recommended_minutes"]
                         - recovery["allocator_allocated_minutes"])

    def test_the_report_measures_against_the_limit_actually_enforced(self):
        # 80 billed over 60 wall-clock minutes is below 1.5 but above the 1.25 each day must hold.
        booked = existing(stamp(9), stamp(10), "bc17f7")
        tst = session("tst", stamp(9), stamp(9, 20))

        result = self.run_pipeline([booked, tst], [(tst, TSTP, 20)])

        report = result["allocation"]["billing_caps"]
        self.assertEqual("5/4", report["policy"]["enforced_day_multiplier"])
        self.assertEqual(["TST Prep"], [row["client"] for row in report["remaining_violations"]["days_over_cap"]])
        self.assertEqual(["TST Prep"], [row["client"] for row in report["remaining_violations"]["months_over_cap"]])

    def test_a_billable_route_without_a_project_id_is_still_one_client(self):
        routing = json.loads((fixtures.ROOT / "routing.json").read_text())
        routing["session_routes"].append({
            "pattern": "no-project-id-client", "project_name": "Unlisted Client — B2B", "project_suffix": "",
            "tag_suffixes": [], "tag_names": [], "billable": True, "confidence": "high", "prefix": "UC"})
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        routing_path = Path(temporary.name) / "routing.json"
        fixtures.write_json(routing_path, routing)
        tst = session("tst", stamp(9), stamp(10))
        other = session("other", stamp(9), stamp(10))
        work = analysis((tst, TSTP, 60), (other, SC, 60))
        work["activities"][1]["project_recommendation"] = {
            "name": "Unlisted Client — B2B", "prefix": "UC", "tag_names": []}

        _, result = fixtures.WorkAccountingPipelineTests.make_run(
            self, [tst, other], work, routing_path=routing_path)

        minutes = {row["client_project"]: row["duration_minutes"] for row in result["proposals"]}
        self.assertEqual({TSTP: 30, "Unlisted Client — B2B": 30}, minutes)
        self.assertEqual(0, result["allocation"]["billing_caps"]["remaining_violations"]["cross_client_seconds"])

    def test_capped_recovery_follows_the_allocators_priority_order(self):
        sessions = [session(f"tst-{index}", stamp(9), stamp(10)) for index in range(3)]
        work = analysis(*[(item, TSTP, 60) for item in sessions])
        for activity, confidence in zip(work["activities"], ("low", "medium", "high")):
            activity["semantic_confidence"] = confidence

        _, result = fixtures.WorkAccountingPipelineTests.make_run(self, sessions, work)

        minutes: dict[str, int] = {}
        for row in result["proposals"]:
            minutes[row["source_label"]] = minutes.get(row["source_label"], 0) + row["duration_minutes"]
        self.assertEqual({"parallel billing outcome 2": 60, "parallel billing outcome 1": 15}, minutes)
        self.assertEqual(
            ["billing cap: parallel time for this client and day is used up"],
            [row["reason"] for row in result["skipped"] if row["reason"].startswith("billing cap")],
        )

    def test_a_routing_snapshot_without_the_declaration_allocates_as_before(self):
        routing = json.loads((fixtures.ROOT / "routing.json").read_text())
        del routing["billing_caps"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        routing_path = Path(temporary.name) / "routing.json"
        fixtures.write_json(routing_path, routing)
        tst = session("tst", stamp(9), stamp(10))
        sc = session("sc", stamp(9), stamp(10))

        result = self.run_pipeline([tst, sc], [(tst, TSTP, 60), (sc, SC, 60)], routing_path=routing_path)

        self.assertEqual(120, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertNotIn("billing_caps", result["allocation"])
        self.assertNotIn("billing_caps", result["allocation"]["deterministic_inputs"])

    def test_capped_demands_leave_nothing_for_the_pending_selection_gate_to_recover(self):
        sessions = [session(f"tst-{index}", stamp(9), stamp(10)) for index in range(3)]
        sc = session("sc", stamp(9, 30), stamp(10, 30))

        result = self.run_pipeline([*sessions, sc], [*[(item, TSTP, 60) for item in sessions], (sc, SC, 60)])

        segments = [
            work_allocator.AllocationSegment(
                row["candidate_key"], row["activity_id"], row["workstream_id"],
                dt.datetime.fromisoformat(row["start"]), dt.datetime.fromisoformat(row["end"]),
                row["duration_minutes"], tuple(row["provenance"]["evidence_ids"]))
            for row in result["proposals"]
        ]
        for raw in result["allocation"]["evidence"]:
            demand = work_allocator._as_activity(raw)
            own = [segment for segment in segments if segment.activity_id == demand.activity_id]
            credited = sum(segment.duration_minutes for segment in own)
            self.assertTrue(all(
                any(low <= segment.start < segment.end <= high for low, high in demand.allowed_intervals)
                for segment in own
            ))
            residual = demand.effort.recommended_minutes - credited
            self.assertGreaterEqual(residual, 0)
            self.assertEqual([], pipeline._capacity_recovery_slices(demand, segments, residual))

    def test_report_and_result_stay_schema_valid_and_reproducible(self):
        sessions = [session(f"tst-{index}", stamp(9), stamp(10)) for index in range(3)]
        sc = session("sc", stamp(9, 30), stamp(10, 30))
        work = [*[(item, TSTP, 60) for item in sessions], (sc, SC, 60)]

        first = self.run_pipeline([*sessions, sc], work)
        second = self.run_pipeline([*sessions, sc], work)

        self.assertEqual(first["proposals"], second["proposals"])
        self.assertEqual(first["allocation"], second["allocation"])
        schema = json.loads((fixtures.ROOT / "schemas" / "work-accounting-result-v1.json").read_text())
        fixtures.assert_schema_valid(schema, first)
        report = first["allocation"]["billing_caps"]
        self.assertEqual({"day_multiplier": "3/2", "month_multiplier": "5/4", "enforced_day_multiplier": "5/4",
                          "timezone": "Europe/Bucharest", "month_scope": "per_day"}, report["policy"])
        self.assertEqual(
            json.loads((fixtures.ROOT / "routing.json").read_text())["billing_caps"],
            first["allocation"]["deterministic_inputs"]["billing_caps"],
        )
        self.assertEqual(0, report["remaining_violations"]["cross_client_seconds"])
        self.assertEqual([], report["remaining_violations"]["days_over_cap"])
        for row in first["proposals"]:
            for warning in row["review_warnings"]:
                fixtures.sheet_publisher._validate_review_warning(warning, {})

    def test_unshared_time_is_billed_exactly_as_before(self):
        tst = session("tst", stamp(9), stamp(10))
        sc = session("sc", stamp(11), stamp(12))

        result = self.run_pipeline([tst, sc], [(tst, TSTP, 60), (sc, SC, 45)])

        self.assertEqual(
            [(TSTP, stamp(9), stamp(10)), (SC, stamp(11), stamp(11, 45))],
            [(row["client_project"], row["start"], row["end"]) for row in result["proposals"]],
        )
        report = result["allocation"]["billing_caps"]
        self.assertEqual({"cross_client": 0, "caps": 0}, report["cut_minutes"])
        self.assertEqual([], report["adjustments"])


if __name__ == "__main__":
    unittest.main()
