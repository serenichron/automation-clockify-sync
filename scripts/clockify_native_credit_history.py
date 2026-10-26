"""Immutable local meeting-credit packets from completed original native posts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from scripts import clockify_source_adoptions as adoptions


SCHEMA = "clockify-native-credit-input/v1"


def _directory(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("native credit output directory is invalid")
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or (path.exists() and not path.is_dir()):
        raise ValueError("native credit output directory must be absolute and canonical")
    return path


def _source_index(originals: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Index native identities only, after original membership was validated."""
    return {f'{proposal["review_activity_key"]}-s{proposal["allocation_segment"]:02d}': proposal
            for proposal in originals
            if isinstance(proposal.get("review_activity_key"), str)
            and type(proposal.get("allocation_segment")) is int}


def validate_source(binding: Mapping[str, Any], *, review_ids: Sequence[str] | None = None,
                    proposals: Sequence[Mapping[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read exact original proposals/ledger and validate unique evidence membership."""
    from scripts.clockify_sheet_publish import stable_review_id
    if not isinstance(binding, Mapping) or set(binding) != {"source_proposals", "source_ledger", "output_directory"}:
        raise ValueError("native credit source binding fields differ")
    _directory(binding["output_directory"])
    cache: dict[tuple[str, str], bytes] = {}
    originals = json.loads(adoptions._capture(binding["source_proposals"], cache))
    ledger = json.loads(adoptions._capture(binding["source_ledger"], cache))
    if not isinstance(originals, list):
        raise ValueError("native credit original proposals are invalid")
    identities = [stable_review_id(proposal) for proposal in originals]
    if len(identities) != len(set(identities)):
        raise ValueError("native credit original proposal identities are duplicated")
    if proposals is not None and list(proposals) != originals:
        raise ValueError("native credit source differs from prepared original proposals")
    selected = identities if review_ids is None else list(review_ids)
    if len(selected) != len(set(selected)) or not set(selected).issubset(identities):
        raise ValueError("native credit plan lacks exact original source membership")
    if selected:
        # _source authenticates every ledger event and the manifest, including
        # unrelated evidence. Reuse that proof only for these captured documents.
        adoptions._source(originals, ledger, selected[0])
        sources = _source_index(originals)
        for identity in selected:
            proposal = sources.get(identity)
            if proposal is None:
                raise adoptions.AdoptionError("source adoption review identity is not unique in original proposals")
            provenance = proposal.get("provenance")
            ids = provenance.get("evidence_ids") if isinstance(provenance, Mapping) else None
            if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
                raise adoptions.AdoptionError("source adoption proposal lacks exact source evidence")
            adoptions.review_corrections.evidence_fingerprint(ids)
            adoptions._validate_source_target(proposal, adoptions._source_events(proposal, ledger), identity)
    return originals, ledger


def _bytes(document: Any) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def validate_plan_source(plan: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bind original time/source; approved native plans retain route corrections."""
    from scripts import clockify_native_sheet_post as native
    originals, ledger = validate_source(plan["native_credit_source"],
        review_ids=[row["review_id"] for row in plan["entries"]],
        proposals=(plan.get("review_batches_binding") or {}).get("source_proposals"))
    sources = _source_index(originals)
    for row in plan["entries"]:
        proposal = sources[row["review_id"]]
        payload = row["payload"]
        if (native._utc(native.legacy._parse(proposal["start"])) != payload["start"]
                or native._utc(native.legacy._parse(proposal["end"])) != payload["end"]
                or proposal["duration_seconds"] != adoptions._seconds(payload)):
            raise ValueError("native credit source payload differs from original interval")
    return originals, ledger


def _write(path: Path, content: bytes) -> dict[str, str]:
    """Install once, reject symlinks or drift, never overwrite an earlier proof."""
    if path.resolve() != path:
        raise ValueError("native credit immutable snapshot path is not canonical")
    if path.exists() or path.is_symlink():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != content:
            raise ValueError("native credit immutable snapshot differs")
    else:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Atomic no-clobber install: crashes expose only complete files.
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                    raise ValueError("native credit immutable snapshot differs")
        finally:
            Path(temporary).unlink(missing_ok=True)
    _fsync_directory(path.parent)
    return {"path": str(path), "sha256": "sha256:" + hashlib.sha256(content).hexdigest()}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def emit_posted_packet(plan: Mapping[str, Any], approval: Mapping[str, Any], events_path: Path,
                       receipt: Mapping[str, Any], receipt_path: Path) -> dict[str, str] | None:
    """Emit v1 only for genuine original recording posts; retry is byte-identical."""
    from scripts import clockify_native_sheet_post as native
    binding = plan.get("native_credit_source")
    if binding is None:
        return None
    originals, ledger = validate_plan_source(plan)
    sources = _source_index(originals)
    approval_digest = native._validate_approval(plan, approval, native.legacy._parse(approval["approved_at"]))
    event_bytes = events_path.read_bytes()
    _, _, confirmed = native._confirmed_by_review(native._decode_events(event_bytes), approval_digest, plan["plan_digest"])
    if (receipt.get("schema_version") != native.RECEIPT_SCHEMA or receipt.get("status") != "complete"
            or receipt.get("plan_digest") != plan["plan_digest"] or receipt.get("approval_digest") != approval_digest
            or receipt.get("event_ledger_sha256") != hashlib.sha256(event_bytes).hexdigest()
            or json.loads(receipt_path.read_bytes()) != receipt):
        raise ValueError("native credit completed receipt binding differs")
    selected = []
    receipt_rows = receipt.get("entries", [])
    for row in plan["entries"]:
        identity = row["review_id"]
        terminal = confirmed.get(identity)
        matches = [item for item in receipt_rows if item.get("review_id") == identity]
        if (terminal is None or len(matches) != 1
                or matches[0].get("clockify_entry_id") != terminal.get("clockify_entry_id")
                or matches[0].get("payload_digest") != row["payload_digest"]
                or matches[0].get("disposition") != terminal.get("disposition")):
            raise ValueError("native credit receipt differs from confirmed original POST")
        if (terminal.get("disposition") not in {"created", "recovered_after_ambiguous_response"}
                or "prior_entry_credit" in row or "sheet_source_credit" in row):
            continue
        proposal = sources[identity]
        try:
            adoptions._native_meeting_identity(adoptions._source_events(proposal, ledger), proposal)
        except adoptions.AdoptionError:
            continue  # Ordinary sessions and ambiguous sources never imply recording equivalence.
        selected.append((identity, terminal["clockify_entry_id"]))
    if not selected:
        return None
    if len(selected) > 512:
        raise ValueError("native credit packet exceeds the 512 original posting limit")
    if len({entry_id for _, entry_id in selected}) != len(selected):
        raise ValueError("native credit repeats an original native entry")
    root = _directory(binding["output_directory"])
    directory = root / plan["plan_digest"]
    _directory(str(directory))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.mkdir(exist_ok=True, mode=0o700)
    _fsync_directory(root.parent)
    _fsync_directory(root)
    artifacts = {"prior_proposals": dict(binding["source_proposals"]), "source_ledger": dict(binding["source_ledger"]),
        "native_plan": _write(directory / "native-plan.json", _bytes(plan)),
        "native_approval": _write(directory / "native-approval.json", _bytes(approval)),
        "native_events": _write(directory / "native-events.jsonl", event_bytes)}
    receipt_snapshot = directory / "native-receipt.json"
    packet_receipt = receipt
    if receipt_snapshot.exists() or receipt_snapshot.is_symlink():
        if receipt_snapshot.resolve() != receipt_snapshot or not receipt_snapshot.is_file():
            raise ValueError("native credit immutable receipt path differs")
        frozen_receipt = json.loads(receipt_snapshot.read_bytes())
        # A later unrelated post may change the period-wide observation, but
        # never any original posting fact. Keep the working receipt fresh and
        # reuse the first complete receipt only for the immutable packet.
        observation = "final_live_snapshot_sha256"
        if (not isinstance(frozen_receipt, dict)
                or not isinstance(frozen_receipt.get(observation), str)
                or re.fullmatch(r"[0-9a-f]{64}", frozen_receipt[observation]) is None
                or {key: value for key, value in frozen_receipt.items() if key != observation}
                    != {key: value for key, value in receipt.items() if key != observation}):
            raise ValueError("native credit immutable receipt binding differs")
        packet_receipt = frozen_receipt
    receipt_handle = _write(receipt_snapshot, _bytes(packet_receipt))
    posted = []
    for identity, entry_id in selected:
        proof = adoptions.verify_prior_native_proof(artifacts, identity, entry_id,
            workspace_id=plan["workspace_id"], member_id=plan["member_id"])
        adoptions._native_meeting_identity(proof["source_events"], proof["prior_proposal"])
        posted.append({"receipt": receipt_handle, "prior_review_id": identity,
                       "clockify_entry_id": entry_id, "artifacts": artifacts})
    return _write(directory / "packet.json", _bytes({"schema_version": SCHEMA, "posted": posted}))
