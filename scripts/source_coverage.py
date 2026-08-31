#!/usr/bin/env python3
"""Durable per-source coverage debt for intermittent fleet collectors.

The ledger is local scheduler state.  It never claims that an unavailable
source contained no work: a failed interval remains debt until a later run
successfully recollects that source from the original interval start.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
LEGACY_COMPATIBILITY_VERSION = "legacy/source-coverage/v1"
PEER_PREFIXES = ("sessions/", "repositories/")


def _utc_timestamp(value: str) -> str:
    """Normalize an explicit UTC timestamp used in source interval identity."""
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("source interval timestamps must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("source interval timestamps must be UTC")
    return parsed.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_digest(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or not value[7:]:
        raise ValueError(f"{name} must be a sha256 digest")
    return value


@dataclass(frozen=True)
class SourceInterval:
    """One source's exact UTC half-open collection slice."""

    source: str
    since_utc: str
    until_utc: str
    slice_id: str
    compatibility_version: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.source, self.slice_id, self.compatibility_version)
        ):
            raise ValueError("source interval identity fields must be non-empty strings")
        since = _utc_timestamp(self.since_utc)
        until = _utc_timestamp(self.until_utc)
        if since >= until:
            raise ValueError("source interval must be half-open with since before until")
        object.__setattr__(self, "since_utc", since)
        object.__setattr__(self, "until_utc", until)

    @property
    def debt_id(self) -> str:
        identity = json.dumps(
            {
                "compatibility_version": self.compatibility_version,
                "since_utc": self.since_utc,
                "slice_id": self.slice_id,
                "source": self.source,
                "until_utc": self.until_utc,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return "source-debt/" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def document(self) -> dict[str, str]:
        return {
            "source": self.source,
            "since_utc": self.since_utc,
            "until_utc": self.until_utc,
            "slice_id": self.slice_id,
            "compatibility_version": self.compatibility_version,
        }


@dataclass(frozen=True)
class DebtItem:
    """Derived state for one exact source interval debt identity."""

    interval: SourceInterval
    retry_count: int
    retryable: bool
    status: str
    failure_class: str | None = None
    resume_state_digest: str | None = None
    completion_bundle_digest: str | None = None
    attempted_at: str | None = None
    completed_at: str | None = None
    next_eligible_at: str | None = None
    terminal_reason: str | None = None

    @property
    def debt_id(self) -> str:
        return self.interval.debt_id


class SourceDebtStore:
    """Append-only in-memory debt events keyed by exact interval identity."""

    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []
        self._items: dict[str, DebtItem] = {}

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "SourceDebtStore":
        if document.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("source debt document schema is unsupported")
        events = document.get("events")
        if not isinstance(events, list):
            raise ValueError("source debt events must be a list")
        store = cls()
        for event in events:
            if not isinstance(event, Mapping):
                raise ValueError("source debt event must be an object")
            raw_interval = event.get("interval")
            if not isinstance(raw_interval, Mapping):
                raise ValueError("source debt event interval is missing")
            try:
                identity_fields = (
                    "source", "since_utc", "until_utc", "slice_id",
                    "compatibility_version",
                )
                if not all(isinstance(raw_interval.get(name), str) for name in identity_fields):
                    raise ValueError("source debt interval identity fields must be strings")
                if not isinstance(event.get("debt_id"), str):
                    raise ValueError("source debt event ID must be a string")
                interval = SourceInterval(
                    source=raw_interval["source"],
                    since_utc=raw_interval["since_utc"],
                    until_utc=raw_interval["until_utc"],
                    slice_id=raw_interval["slice_id"],
                    compatibility_version=raw_interval["compatibility_version"],
                )
                if event.get("debt_id") != interval.debt_id:
                    raise ValueError("source debt event identity does not match its interval")
                kind = event.get("event")
                if kind == "failure":
                    if not isinstance(event.get("failure_class"), str):
                        raise ValueError("source debt failure class must be a string")
                    if not isinstance(event.get("retryable"), bool):
                        raise ValueError("source debt retryable flag must be a boolean")
                    if not isinstance(event.get("resume_state_digest"), str):
                        raise ValueError("source debt resume digest must be a string")
                    if not isinstance(event.get("attempted_at"), str):
                        raise ValueError("source debt attempted time must be a string")
                    store.record_failure(
                        interval,
                        failure_class=event["failure_class"],
                        retryable=event["retryable"],
                        resume_state_digest=event["resume_state_digest"],
                        attempted_at=event["attempted_at"],
                    )
                elif kind == "complete":
                    if not isinstance(event.get("completion_bundle_digest"), str):
                        raise ValueError("source debt completion digest must be a string")
                    if not isinstance(event.get("completed_at"), str):
                        raise ValueError("source debt completion time must be a string")
                    store.record_complete(
                        interval,
                        completion_bundle_digest=event["completion_bundle_digest"],
                        completed_at=event["completed_at"],
                    )
                elif kind == "exhausted":
                    if not isinstance(event.get("terminal_reason"), str):
                        raise ValueError("source debt terminal reason must be a string")
                    store.exhaust(
                        interval.debt_id,
                        terminal_reason=event["terminal_reason"],
                    )
                else:
                    raise ValueError("source debt event kind is unsupported")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("source debt event is invalid") from exc
        store._events = [dict(event) for event in events]
        return store

    def document(self, *, migration_warnings: list[dict[str, str]] | None = None) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "events": list(self._events),
            "migration_warnings": list(migration_warnings or ()),
        }

    def record_failure(
        self,
        interval: SourceInterval,
        *,
        failure_class: str,
        retryable: bool,
        resume_state_digest: str,
        attempted_at: str,
    ) -> DebtItem:
        if not isinstance(failure_class, str) or not failure_class:
            raise ValueError("failure class must be a non-empty string")
        _safe_digest(resume_state_digest, "resume state")
        attempted_at = _utc_timestamp(attempted_at)
        previous = self._items.get(interval.debt_id)
        retries = 1 if previous is None or previous.status == "exhausted" else previous.retry_count + 1
        item = DebtItem(
            interval=interval,
            retry_count=retries,
            retryable=retryable,
            status="active",
            failure_class=failure_class,
            resume_state_digest=resume_state_digest,
            attempted_at=attempted_at,
            next_eligible_at=attempted_at if retryable else None,
        )
        self._items[item.debt_id] = item
        self._events.append({
            "event": "failure",
            "debt_id": item.debt_id,
            "interval": interval.document(),
            "failure_class": failure_class,
            "retryable": retryable,
            "resume_state_digest": resume_state_digest,
            "attempted_at": attempted_at,
        })
        return item

    def record_complete(
        self,
        interval: SourceInterval,
        *,
        completion_bundle_digest: str,
        completed_at: str,
    ) -> DebtItem:
        _safe_digest(completion_bundle_digest, "completion bundle")
        completed_at = _utc_timestamp(completed_at)
        previous = self._items.get(interval.debt_id)
        if previous is None:
            raise ValueError("cannot complete a source interval without matching debt")
        if previous.interval.compatibility_version == LEGACY_COMPATIBILITY_VERSION:
            raise ValueError("legacy debt cannot be resolved as exact coverage")
        item = DebtItem(
            interval=previous.interval,
            retry_count=previous.retry_count,
            retryable=False,
            status="resolved",
            failure_class=previous.failure_class,
            resume_state_digest=previous.resume_state_digest,
            completion_bundle_digest=completion_bundle_digest,
            attempted_at=previous.attempted_at,
            completed_at=completed_at,
        )
        self._items[item.debt_id] = item
        self._events.append({
            "event": "complete",
            "debt_id": item.debt_id,
            "interval": interval.document(),
            "completion_bundle_digest": completion_bundle_digest,
            "completed_at": completed_at,
        })
        return item

    def active(self) -> tuple[DebtItem, ...]:
        return tuple(item for item in self._items.values() if item.status != "resolved")

    def eligible(self, now: str) -> tuple[DebtItem, ...]:
        now = _utc_timestamp(now)
        return tuple(
            item
            for item in self.active()
            if item.status == "active"
            and item.retryable
            and item.next_eligible_at is not None
            and item.next_eligible_at <= now
        )

    def exhaust(self, debt_id: str, *, terminal_reason: str) -> DebtItem:
        if not isinstance(terminal_reason, str) or not terminal_reason:
            raise ValueError("terminal reason must be a non-empty string")
        previous = self._items.get(debt_id)
        if previous is None or previous.status == "resolved":
            raise ValueError("cannot exhaust an unknown or resolved source debt")
        item = DebtItem(
            interval=previous.interval,
            retry_count=previous.retry_count,
            retryable=previous.retryable,
            status="exhausted",
            failure_class=previous.failure_class,
            resume_state_digest=previous.resume_state_digest,
            attempted_at=previous.attempted_at,
            next_eligible_at=None,
            terminal_reason=terminal_reason,
        )
        self._items[debt_id] = item
        self._events.append({
            "event": "exhausted",
            "debt_id": debt_id,
            "interval": item.interval.document(),
            "terminal_reason": terminal_reason,
        })
        return item

    def verify(self) -> None:
        replayed = SourceDebtStore.from_document(self.document())
        if replayed._items != self._items:
            raise ValueError("source debt event replay does not match derived state")


def _day(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _conservative_warning_document(code: str) -> dict[str, Any]:
    """Keep state corruption visible without claiming a recovered exact interval."""
    interval = SourceInterval(
        source="legacy/unknown",
        since_utc="1970-01-01T00:00:00Z",
        until_utc="1970-01-02T00:00:00Z",
        slice_id="legacy-corruption-warning",
        compatibility_version=LEGACY_COMPATIBILITY_VERSION,
    )
    store = SourceDebtStore()
    store.record_failure(
        interval,
        failure_class="legacy_state_invalid",
        retryable=False,
        resume_state_digest="sha256:legacy-state-invalid",
        attempted_at="1970-01-01T00:00:00Z",
    )
    return store.document(migration_warnings=[{"code": code}])


def _migrate_legacy(value: Mapping[str, Any]) -> dict[str, Any]:
    sources = value.get("sources")
    if not isinstance(sources, Mapping):
        return _conservative_warning_document("legacy_sources_invalid")
    store = SourceDebtStore()
    warnings: list[dict[str, str]] = []
    invalid = False
    for raw_source, raw_item in sources.items():
        source = str(raw_source).strip()
        since = _day(raw_item.get("debt_since")) if isinstance(raw_item, Mapping) else None
        if not source or since is None:
            invalid = True
            continue
        day_start = dt.datetime.fromisoformat(since).replace(tzinfo=dt.timezone.utc)
        until = day_start + dt.timedelta(days=1)
        interval = SourceInterval(
            source=source,
            since_utc=day_start.isoformat().replace("+00:00", "Z"),
            until_utc=until.isoformat().replace("+00:00", "Z"),
            slice_id=f"legacy-oldest-day-{since}",
            compatibility_version=LEGACY_COMPATIBILITY_VERSION,
        )
        store.record_failure(
            interval,
            failure_class="legacy_migration",
            retryable=True,
            resume_state_digest="sha256:legacy-state",
            attempted_at="1970-01-01T00:00:00Z",
        )
    if store.active():
        warnings.append({"code": "legacy_source_coverage_migrated"})
    if invalid:
        warnings.append({"code": "legacy_source_coverage_partially_invalid"})
    if invalid and not store.active():
        return _conservative_warning_document("legacy_source_coverage_invalid")
    return store.document(migration_warnings=warnings)


def read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SourceDebtStore().document()
    except (OSError, json.JSONDecodeError):
        return _conservative_warning_document("source_coverage_unreadable")
    if not isinstance(value, Mapping):
        return _conservative_warning_document("source_coverage_invalid")
    if value.get("schema_version") == LEGACY_SCHEMA_VERSION:
        return _migrate_legacy(value)
    if value.get("schema_version") != SCHEMA_VERSION:
        return _conservative_warning_document("source_coverage_schema_unsupported")
    try:
        store = SourceDebtStore.from_document(value)
    except ValueError:
        return _conservative_warning_document("source_coverage_invalid")
    warnings = value.get("migration_warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(warning, Mapping) and isinstance(warning.get("code"), str)
        for warning in warnings
    ):
        return _conservative_warning_document("source_coverage_warnings_invalid")
    return store.document(migration_warnings=[{"code": str(warning["code"])} for warning in warnings])


def bootstrap_from_runs(runs_root: Path, coordinator: str) -> dict[str, Any]:
    """Reconstruct debt from historical immutable run reports on first deploy."""
    ledger: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "sources": {}}
    for report_path in sorted(runs_root.glob("*/run-report.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, Mapping):
            continue
        evidence = report.get("evidence_ledger")
        date_range = report.get("date_range")
        if not isinstance(evidence, Mapping) or not isinstance(date_range, Mapping):
            continue
        completeness = evidence.get("source_completeness")
        since = str(date_range.get("since") or "")
        if not isinstance(completeness, Mapping) or _day(since) is None:
            continue
        ledger = update(
            ledger,
            completeness=completeness,
            interval_since=since,
            interval_until=str(date_range.get("until") or "") or None,
            coordinator=coordinator,
            run_id=str(report.get("run_id") or report_path.parent.name),
            attempted_at=str(report.get("generated_at") or report_path.parent.name),
        )
    return ledger


def write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def is_peer(source: str, coordinator: str) -> bool:
    return source.startswith(PEER_PREFIXES) and source.rsplit("/", 1)[-1] != coordinator


def effective_since(requested_since: str | None, ledger: Mapping[str, Any]) -> str | None:
    candidates = [_day(requested_since)]
    if ledger.get("schema_version") == SCHEMA_VERSION:
        candidates.extend(active_debt(ledger).values())
    else:
        sources = ledger.get("sources")
        if isinstance(sources, Mapping):
            candidates.extend(
                _day(value.get("debt_since"))
                for value in sources.values()
                if isinstance(value, Mapping)
            )
    valid = [value for value in candidates if value]
    return min(valid) if valid else None


def _legacy_active_debt(ledger: Mapping[str, Any]) -> dict[str, str]:
    sources = ledger.get("sources")
    if not isinstance(sources, Mapping):
        return {}
    return {
        str(source): debt
        for source, value in sources.items()
        if isinstance(value, Mapping) and (debt := _day(value.get("debt_since")))
    }


def active_debt(ledger: Mapping[str, Any]) -> dict[str, str]:
    """Legacy adapter: return the oldest unresolved day for each source."""
    if ledger.get("schema_version") != SCHEMA_VERSION:
        return _legacy_active_debt(ledger)
    try:
        items = SourceDebtStore.from_document(ledger).active()
    except ValueError:
        return {"legacy/unknown": "1970-01-01"}
    result: dict[str, str] = {}
    for item in items:
        day = _day(item.interval.since_utc)
        if day:
            result[item.interval.source] = min(day, result.get(item.interval.source, day))
    return result


def update(
    ledger: Mapping[str, Any],
    *,
    completeness: Mapping[str, Any],
    interval_since: str,
    interval_until: str | None,
    coordinator: str,
    run_id: str,
    attempted_at: str,
) -> dict[str, Any]:
    result = {
        "schema_version": LEGACY_SCHEMA_VERSION,
        "updated_at": attempted_at,
        "sources": {
            str(key): dict(value)
            for key, value in (ledger.get("sources") or {}).items()
            if isinstance(value, Mapping)
        },
    }
    since = _day(interval_since)
    until = _day(interval_until)
    if since is None:
        return result
    inventory = completeness.get("sources")
    if not isinstance(inventory, Mapping):
        return result
    for source, raw in inventory.items():
        source = str(source)
        if not is_peer(source, coordinator) or not isinstance(raw, Mapping):
            continue
        previous = result["sources"].get(source, {})
        status = str(raw.get("status") or "partial")
        item = {
            **previous,
            "last_status": status,
            "last_attempt_at": attempted_at,
            "last_run_id": run_id,
        }
        if status in {"complete", "excluded"}:
            debt_since = _day(previous.get("debt_since"))
            if debt_since is None or since <= debt_since:
                item.pop("debt_since", None)
                if until:
                    item["covered_through"] = until
        else:
            debt_since = _day(previous.get("debt_since"))
            item["debt_since"] = min(value for value in (debt_since, since) if value)
            if raw.get("reason"):
                item["reason"] = str(raw["reason"])[:300]
        result["sources"][source] = item
    return result
