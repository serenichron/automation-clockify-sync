#!/usr/bin/env python3
"""Read-only, exact full-period Clockify readback and reconciliation gates."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = "clockify-period-readback/v1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class ClockifyReadbackError(ValueError):
    """A readback is not safe to use as proof of a complete period."""


class ClockifyReadbackGateway(Protocol):
    def read_period(
        self, *, workspace_id: str, member_id: str,
        start: datetime, end: datetime, filters: Mapping[str, Any],
    ) -> ClockifyPeriodReadback: ...

    def read_report_result(
        self, *, report_id: str, workspace_id: str, member_id: str,
        start: datetime, end: datetime, filters: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_json(value: Any) -> str:
    """Stable JSON representation used by every readback digest."""
    return _canonical(value)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClockifyReadbackError(f"{field} is required")
    return value.strip()


def _timestamp(value: Any, field: str) -> datetime:
    raw = _text(value, field)
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClockifyReadbackError(f"{field} must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ClockifyReadbackError(f"{field} must include a timezone offset")
    return result


def _utc(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    result = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        result += f".{value.microsecond:06d}"
    return result + "Z"


def _period(value: Any, field: str, zone: ZoneInfo) -> datetime:
    parsed = _timestamp(value, field)
    return parsed.astimezone(zone)


def _as_bool(value: Any, field: str, default: bool = True) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ClockifyReadbackError(f"{field} must be boolean")
    return value


def _duration(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ClockifyReadbackError(f"{field} must be an exact duration")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, float) and value.is_integer():
        seconds = int(value)
    elif isinstance(value, str):
        raw = value.strip()
        if raw.startswith("PT"):
            match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", raw)
            if not match:
                raise ClockifyReadbackError(f"{field} must be an exact duration")
            hours, minutes, secs = match.groups()
            decimal_seconds = Decimal(secs or "0") + Decimal(minutes or "0") * 60 + Decimal(hours or "0") * 3600
            if decimal_seconds != decimal_seconds.to_integral_value():
                raise ClockifyReadbackError(f"{field} must resolve to whole seconds")
            seconds = int(decimal_seconds)
        else:
            try:
                decimal_seconds = Decimal(raw)
            except InvalidOperation as exc:
                raise ClockifyReadbackError(f"{field} must be an exact duration") from exc
            if decimal_seconds != decimal_seconds.to_integral_value():
                raise ClockifyReadbackError(f"{field} must resolve to whole seconds")
            seconds = int(decimal_seconds)
    else:
        raise ClockifyReadbackError(f"{field} must be an exact duration")
    if seconds < 0:
        raise ClockifyReadbackError(f"{field} must not be negative")
    return seconds


def _entry_interval(entry: Mapping[str, Any]) -> tuple[datetime | None, datetime | None, int]:
    interval = entry.get("timeInterval") or entry.get("time_interval") or {}
    if not isinstance(interval, Mapping):
        raise ClockifyReadbackError("entry time interval must be an object")
    start_raw = interval.get("start", entry.get("start"))
    end_raw = interval.get("end", entry.get("end"))
    duration_raw = entry.get("duration_seconds", interval.get("duration"))
    start = _timestamp(start_raw, "entry start") if start_raw else None
    end = _timestamp(end_raw, "entry end") if end_raw else None
    duration = _duration(duration_raw, "entry duration") if duration_raw is not None else None
    if duration is None and start is not None and end is not None:
        duration = int((end - start).total_seconds())
    if duration is None:
        raise ClockifyReadbackError("entry duration is required")
    if start is not None and end is not None and int((end - start).total_seconds()) != duration:
        raise ClockifyReadbackError("entry duration contradicts its timestamps")
    if start is not None and end is None:
        end = start + timedelta(seconds=duration)
    if start is not None and end is not None and end <= start and duration:
        raise ClockifyReadbackError("entry interval must end after it starts")
    return start, end, duration


def _cost(entry: Mapping[str, Any]) -> tuple[str, Decimal]:
    cost = entry.get("cost") or entry.get("amount")
    if not isinstance(cost, Mapping):
        raise ClockifyReadbackError("entry cost must include a native currency code")
    currency = cost.get("currency") or cost.get("currencyCode") or cost.get("currency_code")
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency.strip().upper()):
        raise ClockifyReadbackError("entry cost is missing a native currency code")
    amount = cost.get("amount", cost.get("value"))
    try:
        result = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ClockifyReadbackError("entry cost amount is invalid") from exc
    if not result.is_finite():
        raise ClockifyReadbackError("entry cost amount is invalid")
    return currency.strip().upper(), result


def _filters(payload: Mapping[str, Any], include_running: bool, include_deleted: bool) -> dict[str, Any]:
    raw = payload.get("filters") or payload.get("filter") or {}
    if not isinstance(raw, Mapping):
        raise ClockifyReadbackError("filters must be an object")
    result = dict(raw)
    private_names = {"api_key", "apikey", "credential", "token", "secret", "password"}
    if _contains_private(result):
        raise ClockifyReadbackError("filters contain prohibited credential fields")
    result.setdefault("include_running", include_running)
    result.setdefault("include_deleted", include_deleted)
    return result


def _contains_private(value: Any) -> bool:
    private_names = {"api_key", "apikey", "credential", "token", "secret", "password"}
    if isinstance(value, Mapping):
        return any(str(key).lower() in private_names or _contains_private(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_private(item) for item in value)
    return False


def _scope_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _scope_id(payload: Mapping[str, Any], field: str) -> str:
    direct = _scope_value(payload, field, field.replace("_", ""), {
        "workspace_id": "workspaceId", "member_id": "memberId"
    }.get(field, ""))
    if direct is None:
        nested = payload.get("workspace" if field == "workspace_id" else "user")
        if isinstance(nested, Mapping):
            direct = nested.get("id")
    return _text(direct, field)


def _member_id(payload: Mapping[str, Any]) -> str:
    direct = _scope_value(payload, "member_id", "memberId", "user_id", "userId")
    if direct is None and isinstance(payload.get("user"), Mapping):
        direct = payload["user"].get("id")
    return _text(direct, "member_id")


def _period_values(payload: Mapping[str, Any]) -> tuple[Any, Any]:
    return (
        _scope_value(payload, "period_start", "periodStart", "date_range_start", "dateRangeStart", "start"),
        _scope_value(payload, "period_end", "periodEnd", "date_range_end", "dateRangeEnd", "end"),
    )


def _refresh_value(payload: Mapping[str, Any]) -> Any:
    return _scope_value(
        payload, "refreshed_at", "refreshedAt", "read_at", "readAt",
        "captured_at", "generated_at", "updated_at",
    )


def _inclusion_values(payload: Mapping[str, Any]) -> tuple[bool, bool]:
    filters = payload.get("filters") or payload.get("filter") or {}
    if not isinstance(filters, Mapping):
        filters = {}
    running = _scope_value(payload, "include_running", "includeRunning")
    deleted = _scope_value(payload, "include_deleted", "includeDeleted")
    return (
        _as_bool(running if running is not None else filters.get("include_running", filters.get("includeRunning")), "include_running", False),
        _as_bool(deleted if deleted is not None else filters.get("include_deleted", filters.get("includeDeleted")), "include_deleted", False),
    )


@dataclass(frozen=True)
class ClockifyPeriodReadback:
    workspace_id: str
    member_id: str
    timezone: str
    period_start: datetime
    period_end: datetime
    filters: Mapping[str, Any]
    refreshed_at: datetime
    include_running: bool
    include_deleted: bool
    entry_ids: tuple[str, ...]
    entry_count: int
    duration_seconds: int
    native_costs: Mapping[str, Decimal]
    digest: str
    entry_durations: Mapping[str, int] | None = None
    schema_version: str = SCHEMA_VERSION
    evidence_kind: str = "ledger"
    request_receipt: Mapping[str, Any] | None = None
    raw_response: Mapping[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_kind": self.evidence_kind,
            "workspace_id": self.workspace_id,
            "member_id": self.member_id,
            "timezone": self.timezone,
            "period_start": _utc(self.period_start),
            "period_end": _utc(self.period_end),
            "filters": dict(self.filters),
            "refreshed_at": _utc(self.refreshed_at),
            "include_running": self.include_running,
            "include_deleted": self.include_deleted,
            "entry_ids": list(self.entry_ids),
            "entry_count": self.entry_count,
            "duration_seconds": self.duration_seconds,
            "entry_durations": {
                key: self.entry_durations[key] for key in self.entry_ids
            } if self.entry_durations else {},
            "native_costs": {key: str(value) for key, value in sorted(self.native_costs.items())},
            **({"request_receipt": dict(self.request_receipt)} if self.request_receipt else {}),
            **({"raw_response": dict(self.raw_response)} if self.raw_response else {}),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.document(), "digest": self.digest}

    @property
    def canonical_digest(self) -> str:
        return self.digest


def _normalize_summary(payload: Mapping[str, Any], zone: ZoneInfo) -> ClockifyPeriodReadback:
    workspace = _scope_id(payload, "workspace_id")
    member = _member_id(payload)
    period_start_raw, period_end_raw = _period_values(payload)
    period_start = _period(period_start_raw, "period_start", zone)
    period_end = _period(period_end_raw, "period_end", zone)
    if period_start >= period_end:
        raise ClockifyReadbackError("period must be a half-open interval")
    refreshed = _timestamp(_refresh_value(payload), "refreshed_at")
    include_running, include_deleted = _inclusion_values(payload)
    filters = _filters(payload, include_running, include_deleted)
    ids_supplied = "entry_ids" in payload or "entryIds" in payload
    ids_raw = payload.get("entry_ids", payload.get("entryIds", []))
    if not isinstance(ids_raw, list) or any(not isinstance(item, str) or not item for item in ids_raw):
        raise ClockifyReadbackError("entry_ids must be a list of non-empty strings")
    ids = tuple(ids_raw)
    if len(set(ids)) != len(ids):
        raise ClockifyReadbackError("entry IDs must be unique")
    count_raw = payload.get("entry_count", payload.get("entryCount", len(ids)))
    seconds_raw = payload.get("duration_seconds", payload.get("durationSeconds", payload.get("totalTime")))
    if not isinstance(count_raw, int) or isinstance(count_raw, bool) or count_raw < 0 or (ids_supplied and ids and count_raw != len(ids)):
        raise ClockifyReadbackError("entry count does not match entry IDs")
    if not isinstance(seconds_raw, int) or isinstance(seconds_raw, bool) or seconds_raw < 0:
        raise ClockifyReadbackError("duration_seconds must be a non-negative integer")
    costs_raw = payload.get("native_costs", payload.get("nativeCosts"))
    if costs_raw is None:
        totals = payload.get("totals")
        total = totals[0] if isinstance(totals, list) and totals and isinstance(totals[0], Mapping) else payload
        costs_raw = {}
        if isinstance(total, Mapping):
            for amount_group in total.get("amounts", []):
                if not isinstance(amount_group, Mapping):
                    continue
                currency_entries = amount_group.get("amountByCurrency", [])
                if isinstance(currency_entries, list):
                    for item in currency_entries:
                        if isinstance(item, Mapping) and item.get("currency") is not None and item.get("amount") is not None:
                            currency = str(item["currency"]).upper()
                            costs_raw[currency] = str(Decimal(costs_raw.get(currency, "0")) + Decimal(str(item["amount"])) / Decimal("100"))
                elif amount_group.get("currency") is not None and amount_group.get("amount") is not None:
                    costs_raw[str(amount_group["currency"]).upper()] = str(Decimal(str(amount_group["amount"])) / Decimal("100"))
    if not isinstance(costs_raw, Mapping):
        raise ClockifyReadbackError("native_costs must be an object")
    costs: dict[str, Decimal] = {}
    for currency, amount in costs_raw.items():
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency.upper()):
            raise ClockifyReadbackError("native_costs contains an invalid currency code")
        try:
            parsed_amount = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ClockifyReadbackError("native_costs contains an invalid amount") from exc
        if not parsed_amount.is_finite():
            raise ClockifyReadbackError("native_costs contains an invalid amount")
        costs[currency.upper()] = parsed_amount
    durations_raw = payload.get("entry_durations", payload.get("entryDurations", {}))
    if not isinstance(durations_raw, Mapping):
        raise ClockifyReadbackError("entry_durations must be an object")
    entry_durations: dict[str, int] = {}
    for entry_id, seconds in durations_raw.items():
        if not isinstance(entry_id, str) or not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
            raise ClockifyReadbackError("entry_durations must contain exact non-negative seconds")
        entry_durations[entry_id] = seconds
    if entry_durations and set(entry_durations) != set(ids):
        raise ClockifyReadbackError("entry_durations must cover every entry ID")
    explicit_kind = "evidence_kind" in payload
    evidence_kind = payload.get("evidence_kind", "report" if "request_receipt" in payload or "entries" not in payload else "ledger")
    if evidence_kind not in {"ledger", "report"}:
        raise ClockifyReadbackError("evidence_kind must be ledger or report")
    if ids_supplied and not ids and count_raw != 0 and evidence_kind != "report":
        raise ClockifyReadbackError("entry count does not match entry IDs")
    receipt = payload.get("request_receipt")
    raw_response = payload.get("raw_response")
    if explicit_kind and evidence_kind == "ledger":
        if not ids or not entry_durations or set(entry_durations) != set(ids) or sum(entry_durations.values()) != seconds_raw or count_raw != len(ids):
            raise ClockifyReadbackError("ledger entry_durations must cover every entry and sum to duration_seconds")
    elif explicit_kind and evidence_kind == "report":
        if not isinstance(receipt, Mapping):
            raise ClockifyReadbackError("report request receipt is required")
        if _text(receipt.get("workspace_id"), "report receipt workspace_id") != workspace or _text(receipt.get("member_id"), "report receipt member_id") != member or not _text(receipt.get("shared_report_id"), "report receipt shared_report_id"):
            raise ClockifyReadbackError("report request receipt scope mismatch")
        if _timestamp(receipt.get("period_start"), "report receipt period_start").astimezone(timezone.utc) != period_start.astimezone(timezone.utc) or _timestamp(receipt.get("period_end"), "report receipt period_end").astimezone(timezone.utc) != period_end.astimezone(timezone.utc):
            raise ClockifyReadbackError("report request receipt period mismatch")
        if _canonical(receipt.get("filters")) != _canonical(filters):
            raise ClockifyReadbackError("report request receipt filter mismatch")
        raw_digest = receipt.get("raw_response_digest")
        if not isinstance(raw_digest, str) or not _DIGEST.fullmatch(raw_digest):
            raise ClockifyReadbackError("report request receipt raw response digest is required")
        if not isinstance(raw_response, Mapping):
            raise ClockifyReadbackError("report raw response is required")
        if _contains_private(raw_response):
            raise ClockifyReadbackError("report raw response contains prohibited credential fields")
        if _digest(raw_response) != raw_digest:
            raise ClockifyReadbackError("report raw response digest does not match receipt")
        if not costs or any(not amount.is_finite() for amount in costs.values()):
            raise ClockifyReadbackError("report native costs are required")
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "evidence_kind": evidence_kind,
        "workspace_id": workspace, "member_id": member, "timezone": zone.key,
        "period_start": _utc(period_start), "period_end": _utc(period_end),
        "filters": filters, "refreshed_at": _utc(refreshed),
        "include_running": include_running, "include_deleted": include_deleted,
        "entry_ids": list(ids), "entry_count": count_raw, "duration_seconds": seconds_raw,
        "entry_durations": {key: entry_durations[key] for key in ids} if entry_durations else {},
        "native_costs": {key: str(value) for key, value in sorted(costs.items())},
    }
    if receipt:
        unsigned["request_receipt"] = dict(receipt)
    if isinstance(raw_response, Mapping):
        unsigned["raw_response"] = dict(raw_response)
    supplied_digest = payload.get("digest")
    digest = _digest(unsigned)
    if supplied_digest is not None and supplied_digest != digest:
        raise ClockifyReadbackError("readback digest does not match its contents")
    return ClockifyPeriodReadback(
        workspace, member, zone.key, period_start, period_end, filters, refreshed,
        include_running, include_deleted, ids, count_raw, seconds_raw, costs, digest,
        entry_durations, SCHEMA_VERSION, evidence_kind, dict(receipt) if isinstance(receipt, Mapping) else None,
        dict(raw_response) if isinstance(raw_response, Mapping) else None,
    )


def normalize_readback(
    payload: ClockifyPeriodReadback | Mapping[str, Any],
    *, timezone_name: str | None = None,
) -> ClockifyPeriodReadback:
    """Normalize either raw Clockify entries or an already summarized readback."""
    if isinstance(payload, ClockifyPeriodReadback):
        return payload
    if not isinstance(payload, Mapping):
        raise ClockifyReadbackError("readback must be an object")
    try:
        zone = ZoneInfo(timezone_name or str(payload.get("timezone", "Europe/Bucharest")))
    except ZoneInfoNotFoundError as exc:
        raise ClockifyReadbackError("timezone is unsupported") from exc
    entries = payload.get("entries")
    if entries is None:
        return _normalize_summary(payload, zone)
    if not isinstance(entries, list):
        raise ClockifyReadbackError("entries must be a list")
    workspace = _scope_id(payload, "workspace_id")
    member = _member_id(payload)
    period_start_raw, period_end_raw = _period_values(payload)
    period_start = _period(period_start_raw, "period_start", zone)
    period_end = _period(period_end_raw, "period_end", zone)
    if period_start >= period_end:
        raise ClockifyReadbackError("period must be a half-open interval")
    refreshed = _timestamp(_refresh_value(payload), "refreshed_at")
    include_running, include_deleted = _inclusion_values(payload)
    filters = _filters(payload, include_running, include_deleted)
    ids: list[str] = []
    entry_durations: dict[str, int] = {}
    total = 0
    costs: dict[str, Decimal] = {}
    explicit_costs = payload.get("native_costs", payload.get("nativeCosts"))
    if explicit_costs is not None:
        if not isinstance(explicit_costs, Mapping):
            raise ClockifyReadbackError("native_costs must be an object")
        for currency, amount in explicit_costs.items():
            if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency.upper()):
                raise ClockifyReadbackError("native_costs contains an invalid currency code")
            try:
                parsed_amount = Decimal(str(amount))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ClockifyReadbackError("native_costs contains an invalid amount") from exc
            if not parsed_amount.is_finite():
                raise ClockifyReadbackError("native_costs contains an invalid amount")
            costs[currency.upper()] = parsed_amount
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ClockifyReadbackError("each entry must be an object")
        entry_id = _text(entry.get("id", entry.get("entry_id")), "entry id")
        entry_workspace = entry.get("workspaceId", entry.get("workspace_id"))
        if entry_workspace is not None and str(entry_workspace) != workspace:
            raise ClockifyReadbackError("workspace mismatch in entry")
        entry_member = entry.get("userId", entry.get("member_id", entry.get("memberId")))
        if entry_member is not None and str(entry_member) != member:
            raise ClockifyReadbackError("member mismatch in entry")
        deleted = bool(entry.get("deleted", False))
        interval = entry.get("timeInterval", entry.get("time_interval", {}))
        running = bool(entry.get("running", False)) or str(entry.get("status", "")).upper() == "RUNNING" or (interval.get("end") is None if isinstance(interval, Mapping) else False)
        if deleted and not include_deleted or running and not include_running:
            continue
        start, end, seconds = _entry_interval(entry)
        if start is None or end is None:
            raise ClockifyReadbackError("entry interval timestamps are required")
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        if local_end <= period_start or local_start >= period_end:
            continue
        if local_start < period_start or local_end > period_end:
            raise ClockifyReadbackError("entry crosses the half-open period")
        if entry_id in ids:
            raise ClockifyReadbackError("entry IDs must be unique")
        ids.append(entry_id)
        entry_durations[entry_id] = seconds
        total += seconds
    summary = {
        "schema_version": SCHEMA_VERSION, "evidence_kind": "ledger", "workspace_id": workspace, "member_id": member,
        "timezone": zone.key, "period_start": _utc(period_start), "period_end": _utc(period_end),
        "filters": filters, "refreshed_at": _utc(refreshed),
        "include_running": include_running, "include_deleted": include_deleted,
        "entry_ids": ids, "entry_count": len(ids), "duration_seconds": total,
        "entry_durations": entry_durations,
        "native_costs": {key: str(value) for key, value in sorted(costs.items())},
    }
    supplied_count = payload.get("entry_count", payload.get("entryCount"))
    if supplied_count is not None and supplied_count != len(ids):
        raise ClockifyReadbackError("entry count does not match normalized entries")
    supplied_seconds = payload.get("duration_seconds", payload.get("durationSeconds"))
    if supplied_seconds is not None and supplied_seconds != total:
        raise ClockifyReadbackError("duration_seconds does not match normalized entries")
    return ClockifyPeriodReadback(
        workspace, member, zone.key, period_start, period_end, filters, refreshed,
        include_running, include_deleted, tuple(ids), len(ids), total, costs, _digest(summary),
        entry_durations,
    )


def _receipt_ids(post_receipt: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    keys = ("created", "already_existing", "recovered_after_ambiguous_response", "recovered")
    for key in keys:
        value = post_receipt.get(key, [])
        if not isinstance(value, list):
            raise ClockifyReadbackError(f"post receipt {key} must be a list")
        for item in value:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, Mapping):
                entry_id = item.get("clockify_entry_id", item.get("entry_id", item.get("id")))
                found.append(_text(entry_id, "post receipt entry ID"))
            else:
                raise ClockifyReadbackError("post receipt entry must identify a Clockify entry")
    terminal_entries = post_receipt.get("entries")
    if terminal_entries is not None:
        if not isinstance(terminal_entries, list):
            raise ClockifyReadbackError("post receipt entries must be a list")
        for item in terminal_entries:
            if not isinstance(item, Mapping):
                raise ClockifyReadbackError("post receipt entry must identify a Clockify entry")
            disposition = item.get("disposition")
            if disposition in {"created", "already_existing", "recovered_after_ambiguous_response"}:
                entry_id = item.get("clockify_entry_id", item.get("entry_id", item.get("id")))
                found.append(_text(entry_id, "post receipt entry ID"))
    return found


def verify_readback(
    api_readback: ClockifyPeriodReadback | Mapping[str, Any],
    shared_report: ClockifyPeriodReadback | Mapping[str, Any],
    post_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require exact scope, freshness, entries, duration, and native costs."""
    api = normalize_readback(api_readback)
    report = normalize_readback(shared_report)
    if api.evidence_kind != "ledger" or report.evidence_kind != "report":
        raise ClockifyReadbackError("evidence kind roles must be ledger and report")
    if (api.workspace_id, api.member_id) != (report.workspace_id, report.member_id):
        if api.member_id != report.member_id:
            raise ClockifyReadbackError("member mismatch between readbacks")
        raise ClockifyReadbackError("workspace mismatch between readbacks")
    if api.timezone != report.timezone or api.period_start.astimezone(timezone.utc) != report.period_start.astimezone(timezone.utc) or api.period_end.astimezone(timezone.utc) != report.period_end.astimezone(timezone.utc):
        raise ClockifyReadbackError("period mismatch between readbacks")
    if _canonical(dict(api.filters)) != _canonical(dict(report.filters)) or api.include_running != report.include_running or api.include_deleted != report.include_deleted:
        raise ClockifyReadbackError("filter mismatch between readbacks")
    if report.refreshed_at < api.refreshed_at:
        raise ClockifyReadbackError("stale report refresh")
    if report.entry_ids and set(api.entry_ids) != set(report.entry_ids):
        raise ClockifyReadbackError("entry IDs mismatch between readbacks")
    if api.entry_count != report.entry_count:
        raise ClockifyReadbackError("entry count mismatch between readbacks")
    delta = api.duration_seconds - report.duration_seconds
    if delta:
        raise ClockifyReadbackError(f"duration mismatch: {abs(delta)} seconds")
    if api.native_costs and report.native_costs and dict(api.native_costs) != dict(report.native_costs):
        raise ClockifyReadbackError("native currency totals mismatch")
    if post_receipt is not None:
        ids = _receipt_ids(post_receipt)
        for entry_id in ids:
            if api.entry_ids.count(entry_id) != 1:
                raise ClockifyReadbackError("post receipt IDs must appear exactly once in final ledger")
        if len(ids) != len(set(ids)):
            raise ClockifyReadbackError("post receipt IDs must appear exactly once")
    return {
        "status": "verified", "api_digest": api.digest, "report_digest": report.digest,
        "entry_count": api.entry_count, "duration_seconds": api.duration_seconds,
        "native_costs": {key: str(value) for key, value in sorted(report.native_costs.items())},
    }


class ClockifyApiGateway:
    """Read-only Clockify time-entry and Reports API adapter."""

    def __init__(self, api_key: str, base_url: str = "https://api.clockify.me/api/v1", *, reports_base_url: str = "https://reports.api.clockify.me/v1"):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._reports_base_url = reports_base_url.rstrip("/")

    def _get(self, path: str) -> Any:
        request = Request(self._base_url + path, headers={"X-Api-Key": self._api_key, "Content-Type": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            raise ClockifyReadbackError("Clockify read-only request failed") from exc

    def _post_report(self, path: str, payload: Mapping[str, Any]) -> Any:
        request = Request(
            self._reports_base_url + path,
            data=_canonical(payload).encode("utf-8"),
            headers={"X-Api-Key": self._api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            raise ClockifyReadbackError("Clockify report read-only request failed") from exc

    def read_period(self, *, workspace_id: str, member_id: str, start: datetime, end: datetime, filters: Mapping[str, Any]) -> ClockifyPeriodReadback:
        if any(filters.get(key, False) is True for key in ("include_running", "include_deleted")):
            raise ClockifyReadbackError("true running/deleted inclusion filters are not supported by this gateway")
        page_size = 200
        entries: list[Mapping[str, Any]] = []
        seen_ids: set[str] = set()
        seen_pages: set[str] = set()
        page = 1
        while True:
            query = urlencode({
                "start": start.isoformat(), "end": end.isoformat(),
                "page-size": page_size, "page": page,
                "include-running": "false", "include-deleted": "false",
            })
            response = self._get(f"/workspaces/{workspace_id}/user/{member_id}/time-entries?{query}")
            if isinstance(response, Mapping):
                response = response.get("entries")
            if not isinstance(response, list):
                raise ClockifyReadbackError("Clockify period response must be an array")
            if len(response) > page_size:
                raise ClockifyReadbackError("Clockify page exceeds bounded page size")
            page_ids = []
            for item in response:
                if not isinstance(item, Mapping):
                    raise ClockifyReadbackError("Clockify period entry must be an object")
                entry_id = _text(item.get("id", item.get("entry_id")), "entry id")
                page_ids.append(entry_id)
                if entry_id in seen_ids:
                    raise ClockifyReadbackError("duplicate Clockify entry ID across pages")
                seen_ids.add(entry_id)
            page_digest = _digest(page_ids)
            if page_digest in seen_pages:
                raise ClockifyReadbackError("duplicate Clockify page")
            seen_pages.add(page_digest)
            entries.extend(response)
            if not response:
                break
            page += 1
            if page > 10000:
                raise ClockifyReadbackError("Clockify pagination safety limit exceeded")
        return normalize_readback({
            "workspace_id": workspace_id, "member_id": member_id, "timezone": str(filters.get("timezone", "Europe/Bucharest")),
            "period_start": start.isoformat(), "period_end": end.isoformat(), "filters": dict(filters),
            "include_running": False, "include_deleted": False,
            "refreshed_at": datetime.now(timezone.utc).isoformat(), "entries": entries,
        })

    def read_report_result(self, *, report_id: str, workspace_id: str, member_id: str, start: datetime, end: datetime, filters: Mapping[str, Any]) -> Mapping[str, Any]:
        users = {"ids": [member_id], "contains": "CONTAINS", "status": "ACTIVE"}
        payload = {
            "dateRangeStart": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "dateRangeEnd": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "summaryFilter": {"groups": ["USER"]}, "amounts": ["COST"], "users": users,
            "sharedReportId": report_id,
        }
        result = self._post_report(f"/workspaces/{workspace_id}/reports/summary", payload)
        if not isinstance(result, Mapping) or not result.get("totals"):
            raise ClockifyReadbackError("shared report result is missing results")
        if _contains_private(result):
            raise ClockifyReadbackError("report result contains prohibited credential fields")
        costs: dict[str, str] = {}
        for total in result.get("totals", []):
            if not isinstance(total, Mapping):
                continue
            for group in total.get("amounts", []):
                if not isinstance(group, Mapping):
                    continue
                for item in group.get("amountByCurrency", []):
                    if isinstance(item, Mapping) and item.get("currency") and item.get("amount") is not None:
                        currency = str(item["currency"]).upper()
                        costs[currency] = format(Decimal(costs.get(currency, "0")) + Decimal(str(item["amount"])) / Decimal("100"), ".2f")
        if not costs:
            raise ClockifyReadbackError("shared report result is missing native costs")
        total = result["totals"][0]
        if not isinstance(total, Mapping):
            raise ClockifyReadbackError("shared report result is missing totals metrics")
        entry_count = total.get("entriesCount", result.get("entriesCount"))
        duration_seconds = total.get("totalTime", result.get("totalTime"))
        if not isinstance(entry_count, int) or not isinstance(duration_seconds, int):
            raise ClockifyReadbackError("shared report result is missing totals metrics")
        projection = {"totals": [{"entriesCount": entry_count, "totalTime": duration_seconds,
                      "amounts": [{"amountByCurrency": [
                          {"currency": currency, "amount": format(Decimal(value) * Decimal("100"), "f")}
                          for currency, value in sorted(costs.items())
                      ]}]}]}
        receipt = {"workspace_id": workspace_id, "member_id": member_id,
                   "period_start": _utc(start), "period_end": _utc(end),
                   "filters": dict(filters), "shared_report_id": report_id,
                   "raw_response_digest": _digest(projection)}
        return {"evidence_kind": "report", "workspace_id": workspace_id, "member_id": member_id,
                "timezone": str(filters.get("timezone", "Europe/Bucharest")),
                "period_start": _utc(start), "period_end": _utc(end), "filters": dict(filters),
                "include_running": False, "include_deleted": False,
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "entry_count": entry_count, "duration_seconds": duration_seconds,
                "native_costs": costs, "request_receipt": receipt, "raw_response": projection}


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClockifyReadbackError(f"cannot read JSON artifact: {path}") from exc


def _write(path: Path, value: Mapping[str, Any], *, root: Path | None = None) -> None:
    _write_safe(path, value, root=root)


def _write_safe(path: Path, value: Mapping[str, Any], *, root: Path | None = None) -> None:
    destination = Path(path)
    base = (root or Path.cwd()).resolve()
    lexical = destination if destination.is_absolute() else Path.cwd() / destination
    if Path(root or Path.cwd()).is_symlink():
        raise ClockifyReadbackError("output root is a symlink")
    try:
        lexical.relative_to(base)
    except ValueError as exc:
        raise ClockifyReadbackError("output path must remain below output root") from exc
    cursor = base
    for component in lexical.relative_to(base).parts[:-1]:
        cursor /= component
        if cursor.is_symlink():
            raise ClockifyReadbackError("output path contains a symlink")
    if lexical.is_symlink():
        raise ClockifyReadbackError("output path is a symlink")
    try:
        resolved = destination.resolve(strict=False)
        resolved.relative_to(base)
    except ValueError as exc:
        raise ClockifyReadbackError("output path must remain below output root") from exc
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.is_symlink():
        raise ClockifyReadbackError("output path is a symlink")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=resolved.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(_canonical(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, resolved)
    descriptor = os.open(resolved.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _period_args(args: argparse.Namespace, routing: Mapping[str, Any]) -> tuple[str, str, str, datetime, datetime]:
    timezone_name = _text(args.timezone, "timezone")
    if str(routing.get("timezone", timezone_name)) != timezone_name:
        raise ClockifyReadbackError("timezone does not match routing")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ClockifyReadbackError("timezone is unsupported") from exc
    start, end = _period(args.since, "since", zone), _period(args.until, "until", zone)
    if start >= end:
        raise ClockifyReadbackError("period must be a half-open interval")
    workspace_value = routing.get("workspace_id", os.environ.get("CLOCKIFY_WORKSPACE_ID"))
    workspace = str(workspace_value).strip() if workspace_value else ""
    member = _text(routing.get("clockify_user_id", routing.get("member_id")), "member_id")
    return timezone_name, workspace, member, start, end


def _capture(args: argparse.Namespace) -> int:
    routing = _json(args.routing)
    if not isinstance(routing, Mapping):
        raise ClockifyReadbackError("routing must be an object")
    timezone_name, workspace, member, start, end = _period_args(args, routing)
    filters = {"workspace_id": workspace, "member_id": member, "timezone": timezone_name, "include_running": False, "include_deleted": False}
    if args.api_fixture:
        api = normalize_readback(_json(args.api_fixture), timezone_name=timezone_name)
        if not workspace:
            workspace = api.workspace_id
            filters["workspace_id"] = workspace
    else:
        api_key = os.environ.get("CLOCKIFY_API_KEY")
        if not api_key:
            raise ClockifyReadbackError("Clockify API key is required for live read-only capture")
        api = ClockifyApiGateway(api_key).read_period(workspace_id=workspace, member_id=member, start=start, end=end, filters=filters)
    if (api.workspace_id, api.member_id, api.timezone) != (workspace, member, timezone_name):
        raise ClockifyReadbackError("captured API scope does not match requested period")
    if api.period_start.astimezone(timezone.utc) != start.astimezone(timezone.utc) or api.period_end.astimezone(timezone.utc) != end.astimezone(timezone.utc):
        raise ClockifyReadbackError("captured API period does not match requested period")
    if _canonical(dict(api.filters)) != _canonical(filters) or api.include_running is not False or api.include_deleted is not False:
        raise ClockifyReadbackError("captured API filters do not match requested filters")
    if args.report_fixture:
        report = normalize_readback(_json(args.report_fixture), timezone_name=timezone_name)
    elif args.api_fixture:
        raise ClockifyReadbackError("report result fixture is required for fixture capture")
    else:
        api_key = os.environ.get("CLOCKIFY_API_KEY")
        if not api_key:
            raise ClockifyReadbackError("Clockify API key is required for live read-only capture")
        gateway = ClockifyApiGateway(api_key)
        raw_report = gateway.read_report_result(
            report_id=args.shared_report_id, workspace_id=workspace, member_id=member,
            start=start, end=end, filters=filters,
        )
        report = normalize_readback(raw_report, timezone_name=timezone_name)
    if (report.workspace_id, report.member_id, report.timezone) != (workspace, member, timezone_name):
        raise ClockifyReadbackError("captured report scope does not match requested period")
    if report.evidence_kind != "report" or not report.request_receipt or report.request_receipt.get("shared_report_id") != args.shared_report_id:
        raise ClockifyReadbackError("captured report request receipt does not match requested report")
    if report.period_start.astimezone(timezone.utc) != start.astimezone(timezone.utc) or report.period_end.astimezone(timezone.utc) != end.astimezone(timezone.utc):
        raise ClockifyReadbackError("captured report period does not match requested period")
    if _canonical(dict(report.filters)) != _canonical(filters) or report.include_running is not False or report.include_deleted is not False:
        raise ClockifyReadbackError("captured report filters do not match requested filters")
    _write(args.api_output, api.to_dict(), root=args.output_root)
    _write(args.report_output, report.to_dict(), root=args.output_root)
    print(_canonical({"status": "captured", "api_digest": api.digest, "report_digest": report.digest, "read_only": True}))
    return 0


def _reconcile(args: argparse.Namespace) -> int:
    api = normalize_readback(_json(args.api))
    report = normalize_readback(_json(args.shared_report))
    gate_error: str | None = None
    try:
        verify_readback(api, report)
    except ClockifyReadbackError as exc:
        gate_error = str(exc)
    api_only = sorted(set(api.entry_ids) - set(report.entry_ids)) if report.entry_ids else []
    report_only = sorted(set(report.entry_ids) - set(api.entry_ids)) if report.entry_ids else []
    delta = api.duration_seconds - report.duration_seconds
    result: dict[str, Any] = {
        "schema_version": "clockify-reconciliation/v1", "status": "verified" if gate_error is None else "readback_mismatch",
        "api_digest": api.digest, "report_digest": report.digest, "duration_delta_seconds": delta,
        "api_only_entry_ids": api_only, "report_only_entry_ids": report_only,
        "filter_match": _canonical(dict(api.filters)) == _canonical(dict(report.filters)),
        "period_match": api.period_start.astimezone(timezone.utc) == report.period_start.astimezone(timezone.utc) and api.period_end.astimezone(timezone.utc) == report.period_end.astimezone(timezone.utc),
        "refresh_ordered": report.refreshed_at >= api.refreshed_at,
        "read_only": True,
        "verification_error": gate_error,
    }
    if api_only and sum(_entry_seconds_from_id(api, entry_id) for entry_id in api_only) == delta:
        result["deterministic_cause"] = "api_only_entries"
    elif report_only and sum(_entry_seconds_from_id(report, entry_id) for entry_id in report_only) == -delta:
        result["deterministic_cause"] = "report_only_entries"
    else:
        result["deterministic_cause"] = None
    _write(args.output, result, root=args.output_root)
    print(_canonical(result))
    return 0 if result["status"] == "verified" else 2


def _entry_seconds_from_id(readback: ClockifyPeriodReadback, entry_id: str) -> int:
    if not readback.entry_durations:
        return 0
    return int(readback.entry_durations.get(entry_id, 0))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="capture two read-only period snapshots")
    capture.add_argument("--routing", type=Path, required=True)
    capture.add_argument("--timezone", required=True)
    capture.add_argument("--since", required=True)
    capture.add_argument("--until", required=True)
    capture.add_argument("--shared-report-id", required=True)
    capture.add_argument("--api-output", type=Path, required=True)
    capture.add_argument("--report-output", type=Path, required=True)
    capture.add_argument("--output-root", type=Path)
    capture.add_argument("--api-fixture", type=Path)
    capture.add_argument("--report-fixture", type=Path)
    reconcile = sub.add_parser("reconcile", help="reconcile two captured snapshots")
    reconcile.add_argument("--api", type=Path, required=True)
    reconcile.add_argument("--shared-report", type=Path, required=True)
    reconcile.add_argument("--output", type=Path, required=True)
    reconcile.add_argument("--output-root", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return _capture(args) if args.command == "capture" else _reconcile(args)
    except (ClockifyReadbackError, OSError) as exc:
        print(f"clockify readback: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
