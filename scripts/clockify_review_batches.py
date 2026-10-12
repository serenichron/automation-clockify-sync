"""Pure packaging of already prepared proposals; no inference or publication.

The caller owns editorial review, Clockify comparison, deduplication and protected
human decisions. This helper neither performs nor claims those checks. Existing
portfolio allocation_segments preserve disjoint windows; child_review_ids and
prepared_descriptions retain their exact native review identities and wording.
No Flash validation result is manufactured. The returned activities require a
source-bound publication/approval adapter before they may be used externally.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from scripts.clockify_sheet_publish import PublicationError, _validate_review_warning, stable_review_id

OVERLAP_REVIEW_NOTICE = (
    "Review presentation includes overlapping workstreams; summed reviewed seconds can exceed "
    "elapsed wall-clock time. This is not financial credit or certified human hours."
)

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\n" not in value and "\r" not in value


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(_text(item) for item in value) and len(set(value)) == len(value)


def _activity(members: list[dict[str, Any]], *, allow_review_overlaps: bool = False) -> dict[str, Any]:
    members = sorted(members, key=lambda item: (item["start"], item["end"], item["review_id"]))
    sources = [item["proposal"] for item in members]
    descriptions = [{"review_id": item["review_id"], "description": item["description"]} for item in members]
    segments = [{"start": source["start"], "end": source["end"],
                 "duration_seconds": source["duration_seconds"],
                 "duration_minutes": source["duration_seconds"] // 60} for source in sources]
    # Bind editorial wording and exact source content, not just the grouping key.
    notice = OVERLAP_REVIEW_NOTICE if allow_review_overlaps and any(
        item.get("has_review_overlap") for item in members) else None
    digest = hashlib.sha256(_canonical({
        "children": [{"review_id": item["review_id"], "proposal": item["proposal"],
                      "description": item["description"], "context": item["context"]} for item in members],
        **({"review_notice": notice} if notice else {}),
    }).encode()).hexdigest()[:24]
    first = sources[0]
    seconds = sum(source["duration_seconds"] for source in sources)
    confidence = {str(source.get("confidence") or "") for source in sources}
    return {
        "review_id": f"pvi-{digest}",
        "activity_id": str(first.get("activity_id") or "") if len(members) == 1 else f"review-batch-{digest}",
        "source_activity_ids": sorted({str(source.get("activity_id") or "") for source in sources}),
        "child_review_ids": [item["review_id"] for item in members],
        "child_review_warnings": [{"review_id": item["review_id"],
                                   "warnings": copy.deepcopy(item["proposal"].get("review_warnings", []))}
                                  for item in members],
        "prepared_descriptions": descriptions,
        "evidence_ids": sorted({str(value) for source in sources
                                for value in (source.get("provenance") or {}).get("evidence_ids", [])}),
        "allocation_segments": segments,
        "start": sources[0]["start"], "end": max(members, key=lambda item: item["end"])["proposal"]["end"],
        "duration_seconds": seconds, "duration_minutes": seconds // 60,
        "client_project": first["client_project"], "tag_names": sorted(first["tag_names"]),
        "billable": first["billable"],
        "confidence": next(iter(confidence)) if len(confidence) == 1 else "",
        "description": "; ".join(dict.fromkeys(item["description"] for item in members)),
        "disposition": "pending",
        **({"review_notice": notice} if notice else {}),
    }


def package_review_batches(
    proposals: Sequence[Mapping[str, Any]], *,
    editorial_descriptions: Mapping[str, str], work_contexts: Mapping[str, str],
    protected_ids: Iterable[str] = (), timezone: str = "Europe/Bucharest",
    wording_only_ids: Iterable[str] = (),
    allow_review_overlaps: bool = False,
    allow_clockify_overlaps: bool = False,
    natural_batches: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Consolidate compatible sub-fifteen-minute sources to reviewable batches.

    Accumulate until at least fifteen minutes, then start the next batch. A
    trailing compatible residual joins the preceding batch. A group with less
    than fifteen minutes total stays honest; time is never invented to meet a
    presentation target. No arbitrary upper duration cap is imposed.
    Explicit ``natural_batches`` presentation instead keeps all eligible
    children in each existing day/project/tags/billable/work-context group in
    one parent. Original child windows and seconds are always retained.

    ``work_contexts`` must contain caller-verified work identities, not labels
    synthesized from date/project. ``editorial_descriptions`` must contain the
    already prepared wording; raw source descriptions are never a fallback.
    Explicit protected IDs must include all authoritative approvals, postings
    and human corrections, even if those facts are absent from source objects.
    Only explicit, source-bound wording-only authority may exempt a source's
    human_corrected flag; it never overrides protected IDs or other guards.
    The default remains the historic protected-human-correction behavior.
    ``allow_review_overlaps`` preserves the historical review-proposal overlap
    policy. Separate explicit ``allow_clockify_overlaps`` authority permits
    well-formed existing Clockify overlap warnings to group without merging or
    deducting time. Meeting/unknown/malformed overlaps remain standalone under
    the new policy; all original warnings remain unchanged.

    Every input survives exactly once as a child or a retained original copy.
    Malformed/duplicate identities remain explicit retained records, never
    silently deduplicated. Retained entries are not publication-ready output.
    """
    zone = ZoneInfo(timezone)
    protected = frozenset(protected_ids)
    wording_only = frozenset(wording_only_ids)
    items: list[dict[str, Any]] = []
    for raw in proposals:
        source = copy.deepcopy(raw)
        reasons: list[str] = []
        review_id = None
        if not isinstance(source, Mapping):
            items.append({"proposal": source, "review_id": None,
                          "reasons": ["invalid_source"], "start": None, "end": None})
            continue
        try:
            review_id = stable_review_id(source)
            if (not re.fullmatch(r"wka-[a-f0-9]{24}-s[0-9]{2,}", review_id)
                    or type(source.get("allocation_segment")) is not int):
                raise ValueError("invalid native review identity")
        except (PublicationError, TypeError, ValueError):
            review_id = None
            reasons.append("invalid_review_identity")
        if review_id in protected:
            reasons.append("protected_review_id")
        if any(str(source.get(field) or "").strip().casefold() in {"approved", "posted"}
               for field in ("disposition", "review_status", "status")):
            reasons.append("approved_or_posted")
        if (allow_review_overlaps or allow_clockify_overlaps or natural_batches or review_id in wording_only) and any(
                str(source.get(field) or "").strip().casefold() in {"approve", "skip", "skipped", "rejected", "superseded"}
                for field in ("disposition", "review_status", "status")):
            reasons.append("protected_terminal_status")
        if source.get("human_corrected") and review_id not in wording_only:
            reasons.append("human_corrected")
        provenance = source.get("provenance") or {}
        if not isinstance(provenance, Mapping):
            provenance = {}
            reasons.append("invalid_provenance")
        elif not _strings(provenance.get("evidence_ids")) or not provenance["evidence_ids"]:
            reasons.append("invalid_provenance")
        if (provenance.get("source_type") in {"recorded_meeting", "fathom", "calendly"}
                or provenance.get("canonical_meeting_id") or source.get("lifecycle") == "meeting"):
            reasons.append("meeting")
        if source.get("routing_disposition") == "unresolved-routing":
            reasons.append("unresolved_routing")
        warnings = source.get("review_warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(w, Mapping) for w in warnings):
            reasons.append("invalid_review_warnings")
        else:
            for warning in warnings:
                if "overlap" not in str(warning.get("type") or "").casefold():
                    continue
                if (allow_review_overlaps and warning.get("type") == "review_proposal_overlap"
                        and not allow_clockify_overlaps):
                    continue  # Preserve historical v3 reconstruction exactly.
                if allow_clockify_overlaps and (warning.get("type") == "existing_clockify_overlap"
                        or allow_review_overlaps and warning.get("type") == "review_proposal_overlap"):
                    try:
                        _validate_review_warning(warning, {})
                    except PublicationError:
                        pass
                    else:
                        continue
                reasons.append("overlap_warning")
                break
        description = editorial_descriptions.get(review_id)
        context = work_contexts.get(review_id)
        if not _text(description):
            reasons.append("missing_editorial_description")
        if not _text(context):
            reasons.append("missing_work_context")
        start, end = _time(source.get("start")), _time(source.get("end"))
        if start is None or end is None or end <= start:
            reasons.append("invalid_interval")
        else:
            elapsed = (end - start).total_seconds()
            seconds = source.get("duration_seconds")
            if type(seconds) is not int or seconds <= 0 or not elapsed.is_integer() or seconds != elapsed:
                reasons.append("invalid_duration_seconds")
            else:
                if seconds >= 900:
                    reasons.append("not_micro_entry")
                if "duration_minutes" in source and (type(source["duration_minutes"]) is not int
                                                     or source["duration_minutes"] != seconds // 60):
                    reasons.append("inconsistent_duration_minutes")
            # An exclusive end exactly at midnight remains on the prior date.
            if start.astimezone(zone).date() != (end - dt.timedelta(microseconds=1)).astimezone(zone).date():
                reasons.append("cross_local_day")
        if (not _text(source.get("client_project")) or not _strings(source.get("tag_names"))
                or not _strings(source.get("tag_suffixes", [])) or type(source.get("billable")) is not bool
                or not _text(source.get("activity_id"))):
            reasons.append("invalid_route_or_activity")
        elif not source["tag_names"]:
            # A valid untagged native entry cannot be represented by a
            # portfolio parent, whose publication contract requires tags.
            reasons.append("untagged_native_entry")
        items.append({"proposal": source, "review_id": review_id, "reasons": reasons,
                      "start": start, "end": end, "description": description, "context": context,
                      "has_review_overlap": isinstance(warnings, list) and any(
                          isinstance(w, Mapping) and w.get("type") in (
                              "review_proposal_overlap", "existing_clockify_overlap") for w in warnings)})

    counts = Counter(item["review_id"] for item in items if item["review_id"] is not None)
    for item in items:
        if counts[item["review_id"]] > 1:
            item["reasons"].append("duplicate_review_identity")
    # Check all original intervals, including sources retained for other reasons.
    spans = sorted((item for item in items if item["start"] is not None and item["end"] is not None
                    and item["end"] > item["start"]), key=lambda item: (item["start"], item["end"]))
    active: list[dict[str, Any]] = []
    for item in spans:
        active = [prior for prior in active if prior["end"] > item["start"]]
        if active:
            item["has_review_overlap"] = True
            if not allow_review_overlaps:
                item["reasons"].append("source_interval_overlap")
            for prior in active:
                prior["has_review_overlap"] = True
                if not allow_review_overlaps:
                    prior["reasons"].append("source_interval_overlap")
        active.append(item)

    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    retained = []
    for item in items:
        if item["reasons"]:
            retained.append({"review_id": item["review_id"], "proposal": item["proposal"],
                             "preparation_reasons": sorted(set(item["reasons"]))})
            continue
        source = item["proposal"]
        key = (item["start"].astimezone(zone).date().isoformat(), source["client_project"],
               str(source.get("clockify_project_suffix") or ""), tuple(sorted(source["tag_names"])),
               tuple(sorted(source.get("tag_suffixes", []))), source["billable"], item["context"])
        grouped.setdefault(key, []).append(item)
    activities = []
    for key in sorted(grouped):
        if natural_batches:
            activities.append(_activity(grouped[key],
                allow_review_overlaps=allow_review_overlaps or allow_clockify_overlaps))
            continue
        partitions: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        total = 0
        for item in sorted(grouped[key], key=lambda item: (item["start"], item["end"], item["review_id"])):
            seconds = item["proposal"]["duration_seconds"]
            current.append(item)
            total += seconds
            if total >= 900:
                partitions.append(current)
                current, total = [], 0
        if current:
            if partitions:
                partitions[-1].extend(current)
            else:
                partitions.append(current)
        activities.extend(_activity(members, allow_review_overlaps=allow_review_overlaps or allow_clockify_overlaps)
                          for members in partitions)
    activities.sort(key=lambda row: (_time(row["start"]), row["review_id"]))
    retained.sort(key=lambda row: (row["review_id"] or "", _canonical(row["proposal"])))
    return {"activities": activities, "retained": retained}


def verify_package(
    package: Mapping[str, Any], *, proposals: Sequence[Mapping[str, Any]],
    work_contexts: Mapping[str, str], protected_ids: Iterable[str] = (),
    timezone: str = "Europe/Bucharest",
    wording_only_ids: Iterable[str] = (),
    allow_review_overlaps: bool = False,
    allow_clockify_overlaps: bool = False,
    natural_batches: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Bind a prepared projection to original sources, not claimed totals.

    This proves mechanical source/segment conservation only. It does not grant
    editorial approval, source credit, or permission to publish/post anything.
    Reconstruct from frozen inputs; consumers need not trust supplied intervals
    or reread mutable source files during approval-bound execution.
    """
    if (not isinstance(package, Mapping) or set(package) != {"activities", "retained"}
            or not isinstance(package["activities"], list)
            or not isinstance(package["retained"], list)):
        raise ValueError("review batch package shape differs")
    descriptions: dict[str, str] = {}
    for activity in package["activities"]:
        if not isinstance(activity, Mapping):
            raise ValueError("review batch activity is invalid")
        prepared = activity.get("prepared_descriptions")
        if (not isinstance(prepared, list) or not prepared
                or any(not isinstance(item, Mapping)
                       or set(item) != {"review_id", "description"}
                       or not _text(item["review_id"]) or not _text(item["description"])
                       for item in prepared)
                or [item["review_id"] for item in prepared] != activity.get("child_review_ids")):
            raise ValueError("review batch prepared wording or membership differs")
        for item in prepared:
            if item["review_id"] in descriptions:
                raise ValueError("review batch child appears more than once")
            descriptions[item["review_id"]] = item["description"]
    expected = package_review_batches(
        proposals, editorial_descriptions=descriptions, work_contexts=work_contexts,
        protected_ids=protected_ids, timezone=timezone, wording_only_ids=wording_only_ids,
        allow_review_overlaps=allow_review_overlaps,
        allow_clockify_overlaps=allow_clockify_overlaps,
        natural_batches=natural_batches,
    )
    if package["activities"] != expected["activities"]:
        raise ValueError("review batch differs from original sources or preparation")
    # Unused editorial metadata can change preparation reasons, but it cannot
    # change which original source objects remain visible for preparation.
    def retained_sources(rows: list[Any]) -> Counter:
        values = []
        for row in rows:
            if (not isinstance(row, Mapping)
                    or set(row) != {"review_id", "proposal", "preparation_reasons"}
                    or not isinstance(row["preparation_reasons"], list)
                    or not row["preparation_reasons"]
                    or any(not _text(reason) for reason in row["preparation_reasons"])):
                raise ValueError("retained review source is invalid")
            values.append(_canonical({"review_id": row["review_id"], "proposal": row["proposal"]}))
        return Counter(values)
    if retained_sources(package["retained"]) != retained_sources(expected["retained"]):
        raise ValueError("review batch drops or changes retained sources")
    return copy.deepcopy(dict(package))
