#!/usr/bin/env python3
"""Probe and evaluate an analyzer route using only built-in synthetic evidence.

This is the executable bridge between a minimal live route probe and the
offline digest-bound evaluator. It never accepts session, meeting, issue, or
user-supplied prose, so it can establish route fitness before private evidence
egress is authorized.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

try:
    from scripts import analyzer_evaluation, semantic_analyzer
except ModuleNotFoundError:  # direct script execution
    import analyzer_evaluation  # type: ignore[no-redef]
    import semantic_analyzer  # type: ignore[no-redef]


Transport = Callable[
    [semantic_analyzer.AnalyzerEndpoint, dict[str, Any]], dict[str, Any]
]


def _evidence_id(case_id: str, index: int) -> str:
    value = f"{case_id}:{index}".encode("utf-8")
    return "ev-" + hashlib.sha256(value).hexdigest()


def _event(
    case_id: str,
    index: int,
    *,
    content: str,
    start: str,
    end: str,
    role: str = "user",
    source_type: str = "codex_sessions",
    meeting_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": _evidence_id(case_id, index),
        "source_type": source_type,
        "role": role,
        "content": content,
        "raw_source_span": {"start": start, "end": end},
        "project_context": {"name": "Serenichron Level 2"},
        **({"meeting_context": meeting_context} if meeting_context else {}),
    }


def synthetic_cases() -> list[dict[str, Any]]:
    """Return fixed non-private cases covering the production failure modes."""
    atomic = "synthetic.atomic"
    split = "synthetic.split"
    merge = "synthetic.merge"
    meeting = "synthetic.title-only-meeting"
    noise = "synthetic.noise"
    return [
        {
            "case_id": atomic,
            "events": [
                _event(
                    atomic,
                    1,
                    content="Implemented a deny-by-default export gate and verified blocked requests.",
                    start="2026-01-05T09:00:00+02:00",
                    end="2026-01-05T09:24:00+02:00",
                )
            ],
            "expected_activity_partitions": [[_evidence_id(atomic, 1)]],
            "expected_activity_concepts": [{
                "evidence_ids": [_evidence_id(atomic, 1)],
                "required_terms": ["export", "gate", "blocked"],
            }],
        },
        {
            "case_id": split,
            "events": [
                _event(
                    split,
                    1,
                    content="Reduced worker memory by removing duplicate document buffers.",
                    start="2026-01-05T10:00:00+02:00",
                    end="2026-01-05T10:28:00+02:00",
                ),
                _event(
                    split,
                    2,
                    content="Wrote the guarded rollout plan for the worker memory change.",
                    start="2026-01-05T10:30:00+02:00",
                    end="2026-01-05T10:48:00+02:00",
                ),
            ],
            "expected_activity_partitions": [
                [_evidence_id(split, 1)],
                [_evidence_id(split, 2)],
            ],
            "expected_activity_concepts": [
                {
                    "evidence_ids": [_evidence_id(split, 1)],
                    "required_terms": ["memory", "duplicate", "buffer"],
                },
                {
                    "evidence_ids": [_evidence_id(split, 2)],
                    "required_terms": ["rollout", "plan"],
                },
            ],
        },
        {
            "case_id": merge,
            "events": [
                _event(
                    merge,
                    1,
                    content="Implemented stable review identity from activity evidence fingerprints.",
                    start="2026-01-05T11:00:00+02:00",
                    end="2026-01-05T11:20:00+02:00",
                ),
                _event(
                    merge,
                    2,
                    content="Confirmed the same stable review identity survives allocation movement.",
                    start="2026-01-05T11:22:00+02:00",
                    end="2026-01-05T11:35:00+02:00",
                ),
            ],
            "expected_activity_partitions": [
                [_evidence_id(merge, 1), _evidence_id(merge, 2)]
            ],
            "expected_activity_concepts": [{
                "evidence_ids": [_evidence_id(merge, 1), _evidence_id(merge, 2)],
                "required_terms": ["review", "identity", "allocation"],
            }],
        },
        {
            "case_id": meeting,
            "events": [
                _event(
                    meeting,
                    1,
                    content="",
                    start="2026-01-05T12:00:00+02:00",
                    end="2026-01-05T12:30:00+02:00",
                    role="meeting",
                    source_type="fathom",
                    meeting_context={"title": "Synthetic discovery call"},
                )
            ],
            "expected_activity_partitions": [],
            "expected_activity_concepts": [],
        },
        {
            "case_id": noise,
            "events": [
                _event(
                    noise,
                    1,
                    content="Still waiting for approval; no work was performed.",
                    start="2026-01-05T13:00:00+02:00",
                    end="2026-01-05T13:01:00+02:00",
                )
            ],
            "expected_activity_partitions": [],
            "expected_activity_concepts": [],
        },
    ]


def _response_object(raw: dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict) and "choices" not in raw:
        return raw
    return semantic_analyzer._json_object_from_response(raw)


def capture_evaluation(
    endpoint: semantic_analyzer.AnalyzerEndpoint,
    *,
    tier: str,
    replay_count: int = 2,
    transport: Transport = semantic_analyzer.http_transport,
) -> dict[str, Any]:
    if tier not in {"primary", "fallback"}:
        raise analyzer_evaluation.EvaluationError("tier must be primary or fallback")
    if replay_count < 2:
        raise analyzer_evaluation.EvaluationError("live evaluation requires at least two replays")
    semantic_analyzer.probe_endpoint(endpoint, transport=transport)
    corpus: list[dict[str, Any]] = []
    captured_cases: list[dict[str, Any]] = []
    for case in synthetic_cases():
        events = list(case["events"])
        evidence_ids = sorted(str(event["evidence_id"]) for event in events)
        for event in events:
            span = semantic_analyzer._safe_time_span(event)
            if span is None:
                raise analyzer_evaluation.EvaluationError(
                    "synthetic evaluation event lacks a complete time span"
                )
            corpus.append({"evidence_id": event["evidence_id"], "time_span": span})
        replays: list[dict[str, Any]] = []
        for _ in range(replay_count):
            body = semantic_analyzer._body_for(
                events,
                model=endpoint.model,
                mode="extract",
                private_text_approved=True,
            )
            raw = _response_object(transport(endpoint, body))
            replays.append(
                semantic_analyzer._restore_evidence_references(
                    raw, evidence_ids=evidence_ids
                )
            )
        captured_cases.append(
            {
                "case_id": case["case_id"],
                "evidence_ids": evidence_ids,
                "expected_activity_partitions": case["expected_activity_partitions"],
                "expected_activity_concepts": case["expected_activity_concepts"],
                "replays": replays,
            }
        )
    corpus.sort(key=lambda item: item["evidence_id"])
    captured_cases.sort(key=lambda item: item["case_id"])
    return {
        "schema_version": analyzer_evaluation.INPUT_SCHEMA_VERSION,
        "corpus": {
            "records": corpus,
            "digest": analyzer_evaluation.sha256_hex(corpus),
        },
        "route": {
            "route_id": semantic_analyzer.stable_digest(
                "route-", {"name": endpoint.name, "model": endpoint.model, "tier": tier}
            ),
            "model": endpoint.model,
            "tier": tier,
        },
        "prompt_version": semantic_analyzer.PROMPT_VERSION,
        "semantic_schema_version": semantic_analyzer.SCHEMA_VERSION,
        "cases": captured_cases,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=("primary", "fallback"), default="primary")
    parser.add_argument("--capture-output", type=Path, required=True)
    parser.add_argument("--scorecard-output", type=Path, required=True)
    args = parser.parse_args(argv)
    prefix = "CLOCKIFY_ANALYZER_PRIMARY" if args.tier == "primary" else "CLOCKIFY_ANALYZER_FALLBACK"
    endpoint = semantic_analyzer.AnalyzerEndpoint.from_env(
        prefix,
        default_model=(semantic_analyzer.DEFAULT_PRIMARY_MODEL if args.tier == "primary" else ""),
    )
    if endpoint is None or not endpoint.model:
        print(f"analyzer live evaluation blocked: {prefix}_URL and {prefix}_MODEL are required")
        return 2
    try:
        capture = capture_evaluation(endpoint, tier=args.tier)
        scorecard = analyzer_evaluation.evaluate(capture)
        _write_json(args.capture_output, capture)
        _write_json(args.scorecard_output, scorecard)
    except (
        OSError,
        ValueError,
        semantic_analyzer.AnalyzerError,
        analyzer_evaluation.EvaluationError,
    ) as exc:
        print(f"analyzer live evaluation blocked: {exc}")
        return 2
    print(
        f"analyzer live evaluation: {'passed' if scorecard['passed'] else 'failed'} "
        f"({scorecard['scorecard_digest']})"
    )
    return 0 if scorecard["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
