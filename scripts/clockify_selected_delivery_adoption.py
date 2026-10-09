"""Read-only proof of an already published, explicitly selected recovery.

Nothing here schedules a child, writes a provider, discovers a run, or grants
posted credit. Original connector captures and producer paths stay immutable.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from scripts import clockify_checkpoint_snapshot as native
from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_source_adoptions as artifacts
from scripts import clockify_sheet_publish as publisher
from scripts import review_corrections

REQUEST_SCHEMA = 'clockify-historical-selected-adoption-request/v1'
ADOPTION_SCHEMA = 'clockify-historical-selected-adoption/v1'
DELIVERY_SCHEMA = 'clockify-selected-review-delivery/v1'


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _digest(value: Any) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps(value, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _time(value: str) -> dt.datetime:
    timestamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(timestamp.tzinfo is not None, 'selected capture timestamp is not aware')
    return timestamp


def _capture_rows(document: Any, spreadsheet: str, title: str, sheet_id: int,
                  width: int) -> dict[int, list[Any]]:
    _require(isinstance(document, Mapping) and document.get('isError') is not True,
             'selected connector capture is invalid')
    structured = document.get('structuredContent', document)
    _require(structured.get('spreadsheetId') == spreadsheet, 'selected capture spreadsheet differs')
    sheets = [sheet for sheet in structured.get('sheets', []) if (
        sheet.get('properties', {}).get('sheetId') == sheet_id
        and sheet.get('properties', {}).get('title') == title)]
    _require(type(sheet_id) is int and len(sheets) == 1, 'selected capture sheet identity differs')
    rows = {}
    for grid in sheets[0].get('data', []):
        _require(grid.get('startColumn', 0) == 0 and type(grid.get('startRow', 0)) is int,
                 'selected capture grid coordinates differ')
        for offset, raw in enumerate(grid.get('rowData', [])):
            number = grid.get('startRow', 0) + offset + 1
            _require(number not in rows, 'selected capture repeats row coordinates')
            values = []
            for cell in raw.get('values', [])[:width]:
                entered = cell.get('userEnteredValue', {})
                _require(not set(entered) - {'stringValue', 'numberValue', 'boolValue'},
                         'selected capture contains a formula or unsupported value')
                values.append(next(iter(entered.values()), ''))
            rows[number] = values + [''] * (width - len(values))
    return rows


def _by_identity(rows: dict[int, list[Any]]) -> dict[str, tuple[int, list[Any]]]:
    result = {}
    for number, row in rows.items():
        if row[0]:
            _require(row[0] not in result, 'selected capture duplicates an identity')
            result[row[0]] = (number, row)
    return result


def _legacy_aliases(packet: Any, *, source: Path, expected: dict[str, list[Any]],
                    live: dict[str, tuple[int, list[Any]]], spreadsheet: str,
                    title: str, sheet_id: int, load) -> dict[str, list[Any]]:
    _require(packet.get('schema_version') == 'retained-diagnostic-source-provenance-aliases/v1'
        and (packet.get('spreadsheet_id'), packet.get('sheet_title'), packet.get('sheet_id')) == (spreadsheet,title,sheet_id),
        'selected diagnostic alias target or schema differs')
    for path, digest in packet.get('source_bindings', {}).items():
        load({'path': path, 'sha256': digest})
    current_events, _ = monthly._ledger_events(source)
    verified = {}
    for alias in packet.get('aliases', []):
        identity = alias['existing_stable_id']
        _require(identity not in verified and identity in expected and identity in live,
                 'selected diagnostic alias identity is duplicated or unknown')
        _require(alias['current_canonical_diagnostic_row'] == expected[identity]
            and alias['current_canonical_diagnostic_row_sha256'] == _digest(expected[identity]),
            'selected diagnostic alias current projection differs')
        number, cells = live[identity]
        _require(number == alias['existing_row_number'] and cells == alias['existing_live_cells']
            and alias['existing_live_cells_sha256'] == _digest(cells)
            and alias['preserve_existing_cells'] is True, 'selected diagnostic alias retained cells differ')
        old_handles = alias['old_source_artifacts']
        new_handles = alias['current_source_artifacts']
        old = {name: load(handle) for name, handle in old_handles.items()}
        current = {name: load(handle) for name, handle in new_handles.items()}
        for name, handle in new_handles.items():
            _require(Path(handle['path']) == source/name, 'selected alias current source locator differs')
        old_exception = old['ambiguous.json'][int(alias['old_exception_json_pointer'].removeprefix('/'))]
        now = current['ambiguous.json'][int(alias['current_exception_json_pointer'].removeprefix('/'))]
        _require(old_exception == alias['old_exception'] and now == alias['current_exception']
            and alias['old_exception_sha256'] == _digest(old_exception)
            and alias['current_exception_sha256'] == _digest(now), 'selected alias exception binding differs')
        kind, ids = now['exception_kind'], sorted(set(now['evidence_ids']))
        typed = {'kind': kind, 'evidence_ids': ids}
        stable = 'uev-' + _digest(typed).removeprefix('sha256:')[:24]
        _require(typed == alias['canonical_diagnostic_identity'] and stable == identity
            and old_exception['exception_kind'] == kind and sorted(set(old_exception['evidence_ids'])) == ids
            and cells[3] == kind, 'selected alias exact evidence identity differs')
        old_root = Path(old_handles['evidence/evidence-ledger.json']['path']).parent.parent
        old_events, _ = monthly._ledger_events(old_root)
        events = [old_events[event_id] for event_id in ids]
        _require(all(event == current_events[event_id] for event_id,event in zip(ids,events,strict=True)),
                 'selected alias canonical source atoms differ')
        _require([atom['evidence_id'] for atom in alias['source_atoms']] == ids
            and all(atom['canonical_event_sha256'] == _digest(event)
                    for atom,event in zip(alias['source_atoms'],events,strict=True)),
                    'selected alias source atom digest differs')
        activities = {a['activity_id']:a for a in old['semantic-analysis.json'].get('activities',[])}
        activity = activities.get(old_exception.get('activity_id'), {})
        titles = list(dict.fromkeys(str(e.get('attributes',{}).get('title') or '').strip()
                      for e in events if e.get('attributes',{}).get('title')))
        summary = str(activity.get('rendered_description') or '; '.join(titles) or old_exception['reason'])[:600]
        zone = ZoneInfo(old['evidence/evidence-ledger.json']['manifest'].get('timezone') or 'Europe/Bucharest')
        days = set()
        for event in events:
            observed = dt.datetime.fromisoformat(event['observed_at'].replace('Z', '+00:00'))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=zone)
            days.add(observed.astimezone(zone).date().isoformat())
        days = sorted(days)
        _require(cells[6] == summary and cells[1] == ', '.join(days)
            and cells[2] == '; '.join(sorted({e['source_type'] for e in events})),
            'selected alias historical summary or date differs')
        old_digest = old_handles['ambiguous.json']['sha256']
        ledger_digest = old_handles['evidence/evidence-ledger.json']['sha256']
        _require(cells[11] == old_digest or cells[11] == (
            f'run {old_root.name}; ambiguous {old_digest}; evidence {ledger_digest}'),
            'selected alias historical lineage differs')
        verified[identity] = cells
    return verified


def _pending_acceptance(actual: Any, expected: Mapping[str, Any], raw) -> dict[str, Any]:
    """Authenticate original authority without pretending newer runtime bytes existed."""
    from scripts import clockify_pending_review_selection as pending
    roles = {'consumer', 'pipeline', 'allocator'}
    _require(isinstance(actual, Mapping)
        and actual.get('schema_version') == 'pending-review-selection-acceptance/v1'
        and isinstance(actual.get('runtime_artifacts'), Mapping)
        and set(actual['runtime_artifacts']) == roles,
        'native pending historical acceptance inventory differs')
    _require(actual.get('acceptance_sha256') == pending.digest({
        key:value for key,value in actual.items() if key != 'acceptance_sha256'}),
        'native pending historical acceptance digest differs')
    for handle in actual['runtime_artifacts'].values():
        raw(handle)
    ignored = {'runtime_artifacts', 'acceptance_sha256'}
    _require(all(key in expected and value == expected[key]
        for key,value in actual.items() if key not in ignored),
        'native pending historical semantic proof differs')
    added = set(expected) - set(actual)
    _require(added <= {'fixed_recording_checks','fixed_recording_rows','saved_credit_seconds'},
        'native pending unrecognized current validation field')
    return {key:expected[key] for key in sorted(added)}


def _pending_editorial(projection: Any, *, source: Path, expected: dict[str,list[Any]],
                      current: dict[str,tuple[int,list[Any]]], by_review: Mapping[str,Any],
                      source_handle: Mapping[str,Any], spreadsheet: str, title: str,
                      sheet_id: int, published_at: dt.datetime, raw, load) -> list[str]:
    """One explicit existing I-only chain; never a description preservation opt-out."""
    from scripts import work_accounting_pipeline as pipeline
    changed = sorted(identity for identity,row in expected.items()
                     if current[identity][1][8] != row[8])
    if projection is None:
        _require(not changed, 'native pending current description lacks editorial proof')
        return []
    _require(isinstance(projection,Mapping) and set(projection) == {
        'schema_version','review_ids','receipt','source_binding','decisions','review_snapshot','source_proposals'}
        and projection['schema_version'] == 'selected-delivery-editorial-projection/v1'
        and projection['review_ids'] == changed and changed,
        'native pending editorial projection target differs')
    _require(projection['source_proposals'] == source_handle
        and Path(projection['review_snapshot']['path']) == source/'review-snapshot.json',
        'native pending editorial original source locator differs')
    load(projection['source_proposals'])
    snapshot = load(projection['review_snapshot'])
    receipt, binding = load(projection['receipt']), load(projection['source_binding'])
    receipt_sha = projection['receipt']['sha256'].removeprefix('sha256:')
    _require(receipt.get('schema') == 'clockify-description-editorial-correction-v1'
        and (receipt.get('spreadsheet_id'),receipt.get('sheet'),receipt.get('sheet_id')) == (spreadsheet,title,sheet_id)
        and receipt.get('scope') == 'Column I userEnteredValue only; pending/unposted rows'
        and receipt.get('verified',{}).get('non_description_cells_unchanged') is True
        and _time(receipt['utc']) >= published_at
        and binding.get('schema') == 'clockify-editorial-source-binding-input-v1'
        and binding.get('sheet_correction_receipt') == projection['receipt']['path']
        and binding.get('receipt_sha256') == receipt_sha,
        'native pending editorial receipt or source binding differs')
    log_bytes = raw(projection['decisions'])
    _require(log_bytes.startswith((source/'review-corrections.jsonl').read_bytes()),
        'native pending editorial decision predecessor differs')
    decisions = review_corrections.load_decisions(Path(projection['decisions']['path']))
    for identity in changed:
        before, after, proposal = expected[identity], current[identity][1], by_review[identity]
        entries = [e for e in binding.get('entries',[]) if e.get('id') == identity]
        corrections = [e for e in receipt.get('corrections',[]) if e.get('id') == identity]
        _require(len(entries) == len(corrections) == 1, 'native pending editorial identity is ambiguous')
        entry, correction = entries[0], corrections[0]
        _require(entry.get('activity_id') == proposal['activity_id']
            and entry.get('source_run_basename') == source.name
            and entry.get('before') == correction.get('before') == before[8]
            and entry.get('description') == correction.get('description') == after[8]
            and type(entry.get('row')) is int and entry['row'] > 1 and entry['row'] == correction.get('row'),
            'native pending editorial exact source target or description differs')
        preimage = entry.get('original_row_values')
        _require(isinstance(preimage,list) and len(preimage) == 15,
            'native pending editorial original row preimage differs')
        for index in range(15):
            if index in {1,2}:
                same = pipeline._parse_dt(preimage[index]) == pipeline._parse_dt(before[index])
            elif index in {3,10} and isinstance(preimage[index],str):
                # Only the witnessed integer display's optional lone trailing dot.
                same = (re.fullmatch(r'[0-9]+\.?',preimage[index]) is not None
                        and int(preimage[index].removesuffix('.')) == before[index])
            else:
                same = publisher._same_cell(preimage[index],before[index])
            _require(same, 'native pending editorial original native preimage differs')
        items = [item for category in snapshot['categories'].values() for item in category
                 if item.get('activity_id') == proposal['activity_id']]
        target = review_corrections.proposal_target(proposal)
        matched = [d for d in decisions if (d['activity_id'],d['evidence_fingerprint']) == target]
        _require(len(items) == len(matched) == 1, 'native pending editorial evidence target is ambiguous')
        decision = review_corrections.validate_decision(matched[0],item=items[0])
        _require(decision['decision'] == 'modify' and decision['correction_categories'] == ['wording']
            and decision['field_patch'] == {'description':{'op':'replace','value':after[8]}}
            and receipt_sha in decision['rationale']
            and _time(decision['reviewed_at']) == _time(binding['utc']) >= _time(receipt['utc']),
            'native pending editorial ordinary description decision differs')
    return changed


def _native_pending_delivery(config, source_stage, replay_stage, proofs, documents, *, title, raw, load):
    """Existing incremental publication plus retained pending, not posted holds."""
    from scripts import clockify_pending_review_selection as pending
    source = Path(source_stage['run_dir'])
    spreadsheet = str(config['spreadsheet_id'])
    selection, packet, receipt, live = [documents[name] for name in (
        'selection','publication_packet','publication_receipt','live_readback')]
    current_source = selection['sources'][selection['current_source']]
    source_handles = current_source['artifacts']
    _require(current_source['basis'] == 'completed-review-run'
        and Path(source_handles['proposals']['path']) == source/'proposals.json'
        and source_handles['proposals']['sha256'] == source_stage['proposals_digest']
        and Path(source_handles['replay']['path']) == Path(replay_stage['run_dir'])/'replay-integrity.json',
        'native pending completed source or replay binding differs')
    proposals = load(source_handles['proposals'])
    routing = load(source_handles['routing'])
    verified = pending.verify(bindings_path=Path(proofs['selection']['path']), source_dir=source,
        proposals=proposals, spreadsheet_id=spreadsheet, sheet_title=title, run_id=source.name,
        project_allowlist=publisher.project_allowlist(routing))
    _require(all(record['disposition'] == 'retain' for record in verified['prior']),
        'native pending delivery requires retained-only predecessors')
    accepted = {row[0]:row for row in verified['rows']}
    new_ids = verified['new_ids']
    retained = [record['review_id'] for record in verified['prior']]
    by_review = {publisher.stable_review_id(p):p for p in proposals}
    _require(len(accepted) == len(verified['rows']) == len(proposals)
        and set(accepted) == set(by_review) == set(source_stage['review_ids'])
        and not set(new_ids)&set(retained) and set(new_ids)|set(retained) == set(accepted),
        'native pending delivery partition is not disjoint and exhaustive')
    current_checks = _pending_acceptance(receipt.get('pending_selection'),verified['receipt'],raw)
    new_rows = [accepted[identity] for identity in new_ids]
    _require(packet.get('schema_version') == 'native-prospective-incremental-publication/v1'
        and packet.get('simulation_not_actual_publication') is True
        and packet.get('source') == str(source) and packet.get('replay') == replay_stage['run_dir']
        and packet.get('consumer_profile') == proofs['selection'] and packet.get('new_rows') == new_rows
        and (packet.get('spreadsheet_id'),packet.get('sheet_title')) == (spreadsheet,title),
        'native pending original planning source binding differs')
    _require(receipt.get('status') == 'verified' and receipt.get('clockify_writes') == 0
        and receipt.get('new_rows') == len(new_rows)
        and receipt.get('new_minutes') == sum(row[3] for row in new_rows)
        and receipt.get('publication_receipt') == publisher._publication_receipt(
            spreadsheet_id=spreadsheet,sheet_title=title,rows=new_rows),
        'native pending actual incremental publication receipt differs')
    published_at = _time(receipt['utc'])
    target_id = packet['sheet_id']
    original = _capture_rows(receipt['readback'],spreadsheet,title,target_id,15)
    current_capture = live.get('readback',live.get('identity_and_portfolio_readback',live))
    current = _capture_rows(current_capture,spreadsheet,title,target_id,15)
    observed_at = live.get('captured_utc',live.get('observed_at_utc'))
    if observed_at is not None:
        _require(_time(observed_at) >= published_at, 'native pending current capture predates publication')
    _require(original.get(1) == publisher.HEADER and current.get(1) == publisher.HEADER,
        'native pending original or current captured header differs')
    match = re.fullmatch(r'A([1-9][0-9]*):O([1-9][0-9]*)',receipt['range'])
    _require(match is not None, 'native pending incremental publication range differs')
    numbers = list(range(int(match[1]),int(match[2])+1))
    _require(len(numbers) == len(new_rows) and [original.get(n) for n in numbers] == new_rows,
        'native pending exact original incremental readback differs')
    original_ids, current_ids = _by_identity(original), _by_identity(current)
    _require(set(accepted) <= set(original_ids) and set(accepted) <= set(current_ids)
        and all(original_ids[identity][1] == row for identity,row in accepted.items()),
        'native pending original accepted or current review identities differ')
    for identity,row in accepted.items():
        indexes = range(15) if identity in retained else [i for i in range(15) if i != 8]
        _require(all(publisher._same_cell(row[i],current_ids[identity][1][i]) for i in indexes),
            'native pending current retained or selected native cells differ')
        _require(row[9] == 'pending' and row[13] == 'unposted', 'native pending row is not pending/unposted')
    editorial_ids = _pending_editorial(documents.get('editorial_projection'),source=source,
        expected={identity:accepted[identity] for identity in new_ids},current=current_ids,by_review=by_review,
        source_handle=source_handles['proposals'],spreadsheet=spreadsheet,title=title,sheet_id=target_id,
        published_at=published_at,raw=raw,load=load)
    manifest = Path(proofs['native_checkpoint_manifest']['path'])
    _require(Path(proofs['native_checkpoint_page']['path']) == manifest.parent/'pages/000001.json',
        'native pending checkpoint page locator differs')
    interval = json.loads((source/'completion-bundle.json').read_bytes())
    since,until = _time(interval['since_utc']),_time(interval['until_utc'])
    identity,request = native._request(str(config['workspace_id']),str(config['member_id']),since,until)
    native._validate(manifest.parent,{'manifest.json':raw(proofs['native_checkpoint_manifest']),
        'pages/000001.json':raw(proofs['native_checkpoint_page'])},raw(proofs['native_evidence']),
        identity=identity,request=request,since=since,until=until)
    diagnostics = monthly.project_rows(source)
    diagnostic_delivery = {'status':'diagnostic_proof_missing' if diagnostics else 'complete',
        'expected_count':len(diagnostics),'review_ids':[],'historical_alias_ids':[],
        'missing':['native_pending_diagnostic_delivery'] if diagnostics else []}
    if diagnostics and {'unresolved_packet','unresolved_receipt','header_readback'} <= set(proofs):
        from scripts import clockify_pending_diagnostic_availability as availability
        diagnostic_delivery = availability.verify(config,source,title,proofs,documents)
    return {'selected_review_ids':new_ids,'selected_minutes':sum(row[3] for row in new_rows),
        'retained_pending_review_ids':retained,'retained_pending_minutes':sum(accepted[i][3] for i in retained),
        'held_review_ids':[],'held_minutes':0,'proofs':dict(proofs),
        'historical_pending_acceptance_sha256':receipt['pending_selection']['acceptance_sha256'],
        'current_additive_native_checks':current_checks,'editorial_projection_review_ids':editorial_ids,
        'current_capture_basis':'byte-bound native grid; no unstated wall-clock freshness claimed',
        'adoption_provider_writes':0,'posted_credits_created':0,
        'diagnostics':diagnostic_delivery}


def validate(config: Mapping[str, Any], source_stage: Mapping[str, Any],
             replay_stage: Mapping[str, Any], proofs: Mapping[str, Any], *, title: str) -> dict[str, Any]:
    """Independently verify original review delivery and diagnostic representation."""
    if isinstance(proofs,Mapping) and set(proofs)=={'selection','live_readback'}:
        selection=json.loads(artifacts._capture(proofs['selection'],{}))
        if selection.get('schema_version')=='mixed-review-availability/v1':
            from scripts import clockify_mixed_review_availability as mixed
            return mixed.verify(config,source_stage,replay_stage,proofs,title=title)
        if selection.get('schema_version')=='native-pending-review-availability/v1':
            from scripts import clockify_native_pending_review_availability as available
            return available.verify(config,source_stage,replay_stage,proofs,title=title)
    core = {'publication_packet','publication_receipt','selection','live_readback',
            'native_evidence','native_checkpoint_manifest','native_checkpoint_page'}
    optional = {'unresolved_packet','unresolved_receipt','diagnostic_aliases','header_readback','editorial_projection'}
    _require(isinstance(proofs,Mapping) and core <= set(proofs) and not set(proofs)-core-optional,
             'selected delivery proof handles are invalid')
    cache = {}
    def raw(handle):
        return artifacts._capture(handle, cache)
    def load(handle):
        return json.loads(raw(handle))
    documents = {name:load(handle) for name,handle in proofs.items()}
    packet, receipt, selection, live = [documents[name] for name in ('publication_packet','publication_receipt','selection','live_readback')]
    if selection.get('schema_version') == 'pending-review-selection/v1':
        return _native_pending_delivery(config,source_stage,replay_stage,proofs,documents,title=title,raw=raw,load=load)
    _require('editorial_projection' not in proofs, 'selected Sep25 delivery does not accept native editorial projection')
    source = Path(source_stage['run_dir'])
    proposals = json.loads((source/'proposals.json').read_bytes())
    spreadsheet = str(config['spreadsheet_id'])
    _require(packet.get('schema_version') == 'verified-review-publication-supplement/v2'
        and packet.get('source_run') == str(source) and packet.get('replay_run') == replay_stage['run_dir']
        and packet.get('duration_reduced_for_overlap') is False, 'selected publication source binding differs')
    _require(selection.get('schema_version') == 'sep25-repaired-publication-selection/v1'
        and selection.get('source_run') == str(source) and selection.get('replay_run') == replay_stage['run_dir']
        and selection.get('source_proposals_sha256') == source_stage['proposals_digest'].removeprefix('sha256:')
        and packet.get('selection_sha256') == proofs['selection']['sha256'].removeprefix('sha256:')
        and packet['held'] == selection.get('held'), 'selected original selection proof differs')
    _require(receipt.get('schema_version') == 'verified-sheet-publication-receipt/v1'
        and receipt.get('spreadsheet_id') == spreadsheet
        and receipt.get('packet_sha256') == proofs['publication_packet']['sha256'].removeprefix('sha256:'),
        'selected original publication receipt differs')
    published_at = dt.datetime.strptime(receipt['utc'], '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=dt.timezone.utc)
    _require(_time(live['captured_utc']) >= published_at, 'selected current capture predates publication')
    target_id = receipt['sheet_id']
    captured = _capture_rows(receipt['readback'],spreadsheet,title,target_id,15)
    current = _capture_rows(live['identity_and_portfolio_readback'],spreadsheet,title,target_id,15)
    match = re.fullmatch(r'A([1-9][0-9]*):O([1-9][0-9]*)', receipt['range'])
    _require(match is not None, 'selected publication range is invalid')
    numbers = list(range(int(match[1]),int(match[2])+1))
    rows = packet['rows']
    _require(len(rows) == receipt['rows'] == len(numbers) and rows
        and [captured.get(n) for n in numbers] == rows and [current.get(n) for n in numbers] == rows,
        'selected exact published or current rows differ')
    _require(len(_by_identity(current)) >= len(rows), 'selected current identity capture is invalid')
    by_review = {publisher.stable_review_id(p):p for p in proposals}
    _require(len(by_review) == len(proposals), 'selected source identities are duplicated')
    selected = [row[0] for row in rows]
    _require(len(set(selected)) == len(selected) and set(selected) <= set(by_review), 'selected row identity is duplicated or unknown')
    projects = publisher.project_allowlist(json.loads((source/'routing.json').read_bytes()))
    for row in rows:
        expected = publisher.proposal_row(by_review[row[0]],source.name,project_allowlist=projects)
        _require(len(row) == 15 and all(row[i] == expected[i] for i in [*range(9),10,11]),
                 'selected machine row differs from exact source target')
        _require(row[9] == 'pending' and row[13] == 'unposted', 'selected row is not pending/unposted review')
    by_target = {(p['activity_id'],p['start'],p['end'],p['duration_minutes']):p for p in proposals}
    by_ordinal = {p['id']:p for p in proposals}
    _require(len(by_target) == len(proposals) and len(by_ordinal) == len(proposals), 'selected source target binding is ambiguous')
    remaining = selection.get('remaining', [])
    _require(len(remaining) == len(selected) and {
        publisher.stable_review_id(by_ordinal[item['proposal_id']]) for item in remaining} == set(selected)
        and all((item['activity_id'],item['minutes']) == (by_ordinal[item['proposal_id']]['activity_id'],by_ordinal[item['proposal_id']]['duration_minutes']) for item in remaining),
        'selected remaining source target differs')
    manifest = Path(proofs['native_checkpoint_manifest']['path'])
    page = Path(proofs['native_checkpoint_page']['path'])
    _require(page == manifest.parent/'pages/000001.json', 'selected native page locator differs')
    interval = json.loads((source/'completion-bundle.json').read_bytes())
    since,until = _time(interval['since_utc']),_time(interval['until_utc'])
    identity,request = native._request(str(config['workspace_id']),str(config['member_id']),since,until)
    entries,_,_ = native._validate(manifest.parent,{'manifest.json':raw(proofs['native_checkpoint_manifest']),
        'pages/000001.json':raw(proofs['native_checkpoint_page'])},raw(proofs['native_evidence']),
        identity=identity,request=request,since=since,until=until)
    _require(packet.get('fresh_clockify_sha256') == proofs['native_evidence']['sha256'].removeprefix('sha256:')
        and packet.get('native_page_sha256') == proofs['native_checkpoint_page']['sha256'].removeprefix('sha256:'),
        'selected native observation binding differs')
    native_by_id = {entry['id']:entry for entry in entries}
    held = []
    for hold in packet['held']:
        p = by_ordinal[hold['proposal_id']]
        decision = hold['native_decision']
        entry = native_by_id[decision['native_entry_id']]
        _require(hold['decision'] == decision['recommendation'] == 'hold_represented'
            and hold['activity_id'] == decision['activity_id'] == p['activity_id']
            and decision['evidence_fingerprint'] == review_corrections.proposal_target(p)[1]
            and hold['minutes'] == decision['source_minutes'] == p['duration_minutes']
            and decision['represented_minutes'] >= p['duration_minutes']
            and decision['automatic_credit_created'] is False
            and isinstance(decision.get('rationale'),str) and decision['rationale'].strip(), 'selected reviewed hold target differs')
        _require(entries[decision['native_original_index']]['id'] == entry['id']
            and hashlib.sha256(entry['id'].encode()).hexdigest() == decision['native_id_sha256']
            and hashlib.sha256(entry['description'].encode()).hexdigest() == decision['native_description_sha256']
            and entry['timeInterval']['end'] is not None
            and _time(entry['timeInterval']['start']) <= _time(p['start']) < _time(p['end']) <= _time(entry['timeInterval']['end'])
            and isinstance(entry['projectId'],str) and p['clockify_project_suffix'] and entry['projectId'].endswith(p['clockify_project_suffix'])
            and sorted(tag[-8:] for tag in entry['tagIds']) == sorted(p['tag_suffixes']), 'selected held native representation differs')
        held.append(publisher.stable_review_id(p))
    _require(len(set(held)) == len(held) and not set(selected)&set(held)
        and set(selected)|set(held) == set(by_review), 'selected delivery partition is not disjoint and exhaustive')
    _require(receipt['minutes'] == sum(by_review[i]['duration_minutes'] for i in selected), 'selected published minutes differ')
    result = {'selected_review_ids':selected,'held_review_ids':held,
        'selected_minutes':sum(by_review[i]['duration_minutes'] for i in selected),
        'held_minutes':sum(by_review[i]['duration_minutes'] for i in held),
        'proofs':dict(proofs), 'adoption_provider_writes':0,'posted_credits_created':0,
        'diagnostics':{'status':'complete','review_ids':[],'historical_alias_ids':[]}}
    expected_diagnostics = monthly.project_rows(source)
    if expected_diagnostics:
        missing = {'unresolved_packet','unresolved_receipt','header_readback'}-set(proofs)
        if missing or 'diagnostic_readback' not in live:
            result['diagnostics'] = {'status':'diagnostic_proof_missing','missing':sorted(missing or {'diagnostic_readback'})}
            return result
        unresolved = documents['unresolved_packet']; ur = documents['unresolved_receipt']
        _require(unresolved.get('rows') == expected_diagnostics and unresolved.get('source_run') == str(source)
            and ur.get('packet_sha256') == proofs['unresolved_packet']['sha256'].removeprefix('sha256:')
            and ur.get('spreadsheet_id') == spreadsheet, 'selected diagnostic original source proof differs')
        diagnostic_title = monthly.title_for_review(title); diagnostic_id=ur['sheet_id']
        header = _capture_rows(documents['header_readback'],spreadsheet,diagnostic_title,diagnostic_id,12)
        _require(header.get(1) in (monthly.HEADER,monthly.LEGACY_HEADER),
                 'selected diagnostic captured header differs')
        now = _by_identity(_capture_rows(live['diagnostic_readback'],spreadsheet,diagnostic_title,diagnostic_id,12))
        original = _by_identity(_capture_rows(ur['readback'],spreadsheet,diagnostic_title,diagnostic_id,12))
        expected = {row[0]:row for row in expected_diagnostics}
        _require(set(expected) <= set(now), 'selected live diagnostic identities are missing')
        aliases = _legacy_aliases(documents['diagnostic_aliases'],source=source,expected=expected,live=now,
            spreadsheet=spreadsheet,title=diagnostic_title,sheet_id=diagnostic_id,load=load) if 'diagnostic_aliases' in documents else {}
        unproven=[]
        for identity,row in expected.items():
            actual=now[identity][1]
            if identity in aliases:
                continue
            legacy=monthly.rows_for_layout([row],monthly.LEGACY_LAYOUT)[0]
            if identity not in original or original[identity][1] != actual or actual not in (row,legacy):
                unproven.append(identity)
        result['diagnostics'] = {'status':'diagnostic_proof_missing' if unproven else 'complete',
            'review_ids':list(expected),'historical_alias_ids':sorted(aliases),'missing':unproven,
            'original_captured_layout':'literal source canonical or legacy cells; no URL normalization'}
    return result
