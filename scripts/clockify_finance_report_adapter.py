#!/usr/bin/env python3
"""Execute one authorized Clockify finance-report publication attempt."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import clockify_publication_gate as publication_gate
from scripts import reconciliation_manifest
from scripts.publication_adapter_contract import (
    PublicationAdapter,
    PublicationAdapterError,
    PublicationReceipt,
    PublicationReceiptStore,
    SharedReportReceipt,
    SlackReceipt,
    execute_authorized_publication,
)


class ClockifyFinanceReportAdapter:
    """Production boundary that intentionally refuses undocumented mutation transports."""

    def update_report(self, authorized: publication_gate.AuthorizedPublication) -> None:
        raise PublicationAdapterError("no documented shared-report mutation transport is configured")

    def read_report(self, authorized: publication_gate.AuthorizedPublication) -> SharedReportReceipt:
        raise PublicationAdapterError("no exact shared-report readback transport is configured")

    def upsert_slack(
        self, authorized: publication_gate.AuthorizedPublication, report_receipt: SharedReportReceipt,
    ) -> SlackReceipt:
        raise PublicationAdapterError("no documented Slack upsert transport is configured")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationAdapterError(f"{field} is required")
    return value.strip()


def _json(path: Path, field: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationAdapterError(f"{field} is unavailable or invalid") from exc
    if not isinstance(value, Mapping):
        raise PublicationAdapterError(f"{field} must be an object")
    return value


def _now(value: datetime | None) -> datetime:
    value = datetime.now(timezone.utc) if value is None else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise PublicationAdapterError("publication clock must include a timezone")
    return value.astimezone(timezone.utc)


def _write_manifest(path: Path, manifest: reconciliation_manifest.ReconciliationManifest) -> None:
    root = path.parent.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        safe = reconciliation_manifest._path_below(root, path)
        reconciliation_manifest._write_canonical(root, safe, manifest.document())
    except reconciliation_manifest.ManifestError as exc:
        raise PublicationAdapterError(f"period manifest output is unsafe: {exc}") from exc


def _result(action: str, manifest: reconciliation_manifest.ReconciliationManifest, *, reason: str | None = None) -> None:
    result: dict[str, object] = {
        "action": action,
        "state": manifest.state,
        "manifest_digest": manifest.manifest_digest,
    }
    if reason:
        result["reason"] = reason
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


def _authorized(value: Mapping[str, object], now: datetime) -> publication_gate.AuthorizedPublication:
    try:
        authorized = publication_gate.AuthorizedPublication.from_document(value)
    except publication_gate.PublicationGateError as exc:
        raise PublicationAdapterError(f"publication authorization is invalid: {exc}") from exc
    expires_at = datetime.fromisoformat(authorized.expires_at.replace("Z", "+00:00"))
    if expires_at <= now:
        raise PublicationAdapterError("publication authorization is expired")
    return authorized


def _authorization_is_bound(
    history: Sequence[reconciliation_manifest.CoordinatorEvent],
    authorized: publication_gate.AuthorizedPublication,
) -> bool:
    expected = {
        "contract_digest": authorized.contract_digest,
        "idempotency_identity": authorized.idempotency_key,
    }
    return any(event.event_type == "publication_authorized" and event.payload == expected for event in history)


def production_adapter(
    authorized: publication_gate.AuthorizedPublication,
    environment: Mapping[str, str] = os.environ,
) -> ClockifyFinanceReportAdapter:
    """Validate an exact target configuration before exposing any production gateway."""
    required = (
        "CLOCKIFY_API_KEY", "CLOCKIFY_FINANCE_REPORT_TARGET",
        "SLACK_BOT_TOKEN", "CLOCKIFY_FINANCE_SLACK_TARGET",
    )
    missing = [name for name in required if not str(environment.get(name) or "").strip()]
    if missing:
        raise PublicationAdapterError("production publication transport credentials are unavailable")
    if environment["CLOCKIFY_FINANCE_REPORT_TARGET"].strip() != authorized.contract.report_target:
        raise PublicationAdapterError("configured Clockify report target does not match authorization")
    if environment["CLOCKIFY_FINANCE_SLACK_TARGET"].strip() != authorized.contract.slack_target:
        raise PublicationAdapterError("configured Slack target does not match authorization")
    raise PublicationAdapterError(
        "fixed Clockify/Slack production transport is not implemented; refusing external publication"
    )


def _append(
    store: reconciliation_manifest.CoordinatorEventStore,
    identity: reconciliation_manifest.PeriodIdentity,
    event_type: str,
    payload: Mapping[str, object],
    now: datetime,
) -> reconciliation_manifest.ReconciliationManifest:
    try:
        store.append(identity, event_type, payload, occurred_at=now)
        return reconciliation_manifest.ReconciliationCoordinator(identity, store).derive()
    except reconciliation_manifest.ManifestError as exc:
        raise PublicationAdapterError(f"could not append coordinator event: {exc}") from exc


def _published_receipt_matches_events(
    store: PublicationReceiptStore, history: Sequence[reconciliation_manifest.CoordinatorEvent],
) -> bool:
    completion = next((event for event in reversed(history) if event.event_type == "publication_complete"), None)
    if completion is None:
        return False
    payload = completion.payload
    for receipt in store.published_receipts():
        if (
            payload.get("contract_digest") == receipt.contract_digest
            and payload.get("idempotency_identity") == receipt.idempotency_identity
            and payload.get("report_receipt") == receipt.report_receipt.document()
            and payload.get("slack_receipt") == receipt.slack_receipt.document()
        ):
            return True
    return False


def _execute(
    args: argparse.Namespace,
    *,
    now: datetime,
    adapter_factory: Callable[[publication_gate.AuthorizedPublication], PublicationAdapter] | None,
    environment: Mapping[str, str],
) -> int:
    try:
        supplied = reconciliation_manifest.ReconciliationManifest.from_document(_json(args.period_manifest, "period manifest"))
        events = reconciliation_manifest.CoordinatorEventStore(args.events)
        manifest = reconciliation_manifest.ReconciliationCoordinator(supplied.identity, events).derive()
    except (PublicationAdapterError, reconciliation_manifest.ManifestError) as exc:
        print(f"publication adapter blocked: {exc}", file=sys.stderr)
        return 2

    def defer(reason: str) -> int:
        nonlocal manifest
        try:
            manifest = _append(events, supplied.identity, "publication_deferred", {"reason": reason}, now)
            _write_manifest(args.period_manifest, manifest)
            _result("publication_deferred", manifest, reason=reason)
            return 0
        except PublicationAdapterError as exc:
            print(f"publication adapter blocked: {exc}", file=sys.stderr)
            return 2

    if manifest.document() != supplied.document():
        return defer("period_manifest_drift")
    if manifest.state == "published":
        try:
            if not _published_receipt_matches_events(PublicationReceiptStore(args.receipts), events.verify(supplied.identity)):
                raise PublicationAdapterError("published manifest does not bind a persisted publication receipt")
        except PublicationAdapterError as exc:
            print(f"publication adapter blocked: {exc}", file=sys.stderr)
            return 2
        _result("published", manifest)
        return 0
    try:
        authorized = _authorized(_json(args.authorized, "publication authorization"), now)
    except PublicationAdapterError:
        return defer("authorization_unready")
    history = events.verify(supplied.identity)
    recoverable = {"publication_deferred", "report_mismatch"}
    if (
        manifest.state != "publication_authorized"
        or not set(manifest.blockers).issubset(recoverable)
        or not _authorization_is_bound(history, authorized)
    ):
        return defer("authorization_unready")
    try:
        adapter = adapter_factory(authorized) if adapter_factory is not None else production_adapter(authorized, environment)
    except PublicationAdapterError:
        return defer("transport_unavailable")

    def append_shared_report(receipt: SharedReportReceipt) -> None:
        nonlocal manifest
        current = events.verify(supplied.identity)
        expected = {
            "contract_digest": authorized.contract_digest,
            "idempotency_identity": authorized.idempotency_key,
            "report_receipt": receipt.document(),
        }
        if any(event.event_type == "shared_report_verified" and event.payload == expected for event in current):
            return
        manifest = _append(events, supplied.identity, "shared_report_verified", expected, now)
        _write_manifest(args.period_manifest, manifest)

    receipt_store = PublicationReceiptStore(args.receipts, on_report_persisted=append_shared_report)
    try:
        report = receipt_store.report_receipt(authorized.idempotency_key)
        if report is not None:
            append_shared_report(report)
        publication = execute_authorized_publication(authorized, adapter, receipt_store, now=now)
    except PublicationAdapterError as exc:
        message = str(exc)
        if "shared report readback does not exactly match" in message:
            try:
                manifest = _append(events, supplied.identity, "report_mismatch", {"reason": "exact_readback_mismatch"}, now)
                _write_manifest(args.period_manifest, manifest)
                _result("report_mismatch", manifest)
                return 1
            except PublicationAdapterError as append_error:
                print(f"publication adapter blocked: {append_error}", file=sys.stderr)
                return 2
        _result("publication_incomplete", manifest, reason="retryable_delivery_failure")
        return 1
    payload = {
        "contract_digest": publication.contract_digest,
        "idempotency_identity": publication.idempotency_identity,
        "report_receipt": publication.report_receipt.document(),
        "slack_receipt": publication.slack_receipt.document(),
    }
    try:
        manifest = _append(events, supplied.identity, "publication_complete", payload, now)
        _write_manifest(args.period_manifest, manifest)
        _result("published", manifest)
        return 0
    except PublicationAdapterError as exc:
        print(f"publication adapter blocked: {exc}", file=sys.stderr)
        return 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("execute")
    execute.add_argument("--period-manifest", type=Path, required=True)
    execute.add_argument("--events", type=Path, required=True)
    execute.add_argument("--authorized", type=Path, required=True)
    execute.add_argument("--receipts", type=Path, required=True)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    now: datetime | None = None,
    adapter_factory: Callable[[publication_gate.AuthorizedPublication], PublicationAdapter] | None = None,
    environment: Mapping[str, str] = os.environ,
) -> int:
    try:
        args = parse_args(argv)
        if args.command != "execute":
            raise PublicationAdapterError("unsupported publication command")
        return _execute(args, now=_now(now), adapter_factory=adapter_factory, environment=environment)
    except (PublicationAdapterError, reconciliation_manifest.ManifestError) as exc:
        print(f"publication adapter blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
