"""Current factual-attendance review availability, never financial equivalence.

Historical accounting is a recording/activity identity witness, NOT a claim that
its allocation, publication or approval history equals today's physical rows.
Only explicitly pinned existing artifacts are read; no provider is invoked.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from scripts import clockify_checkpoint_snapshot as snapshots
from scripts import clockify_meeting_publication_alias as meeting_alias
from scripts import clockify_native_pending_review_availability as completed
from scripts import clockify_pending_review_selection as pending
from scripts import clockify_review_batch_publication as batches
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_source_adoptions as artifacts
from scripts import clockify_sync_collect as collector
from scripts import collector_checkpoints as checkpoints
from scripts import work_accounting_pipeline as pipeline

SCHEMA = 'recorded-attendance-review-availability/v1'
SCOPE = {'review_availability_only': True, 'financial_coverage_claimed': False,
         'financial_equivalence_claimed': False, 'additional_credited_seconds': 0,
         'diagnostic_resolution_claimed': False, 'source_debt_resolution_claimed': False}


def require(condition, message):
    if not condition:
        raise ValueError('recorded attendance availability: ' + message)


def availability_scope(document):
    require(all(type(document.get(k)) is type(v) and document[k] == v
                for k, v in SCOPE.items()), 'availability-only boundary differs')


def instant(value):
    raw_whole_seconds(value)
    parsed = pending._time(value)
    require(parsed.microsecond == 0, 'recording interval requires exact seconds')
    return parsed.astimezone(dt.UTC)


def raw_whole_seconds(value):
    # datetime.fromisoformat silently truncates beyond six fractional digits.
    # Inspect the original spelling before any parser can discard precision.
    require(isinstance(value, str) and all(not set(digits) - {'0'}
        for digits in re.findall(r'[.,]([0-9]+)', value)),
        'recording interval requires exact seconds')


def exact_partition(start, end, intervals):
    """Conserve exact intervals, not just totals that could hide gap/overlap."""
    lo, hi = instant(start), instant(end)
    require(hi > lo and intervals, 'recording partition is empty or reversed')
    cursor = lo
    for a, b in sorted((instant(a), instant(b)) for a, b in intervals):
        require(a == cursor and lo <= a < b <= hi, 'recording partition has gap or overlap')
        cursor = b
    require(cursor == hi, 'recording partition has final gap')
    return int((hi - lo).total_seconds())


def recording_checkpoint(proof, cache):
    require(set(proof) == {'manifest', 'pages'}, 'checkpoint inventory differs')
    raw = lambda h: artifacts._capture(h, cache)
    manifest = json.loads(raw(proof['manifest']))
    directory = Path(proof['manifest']['path']).parent
    require(Path(proof['manifest']['path']).name == 'manifest.json', 'checkpoint manifest locator differs')
    identity = checkpoints._identity_from_document(manifest['identity'])
    require(identity.source == 'fathom' and identity == collector._fathom_checkpoint_identity(
        instant(identity.since_utc), instant(identity.until_utc)), 'Fathom checkpoint request identity differs')
    expected = [directory / item['path'] for item in manifest['pages']]
    require([Path(h['path']) for h in proof['pages']] == expected, 'checkpoint page inventory differs')
    files = {'manifest.json': raw(proof['manifest'])}
    files.update({str(Path(h['path']).relative_to(directory)): raw(h) for h in proof['pages']})
    store = snapshots._CapturedStore(directory, files)
    state = store.open(identity)
    collector._fathom_checkpoint_items(store, state)
    # iter_pages deliberately freezes nested objects. Return ordinary decoded
    # original JSON only after its complete page/provider chain is validated.
    items = [item for page in state.pages for item in collector._fathom_page_response(
        json.loads(files[page['path']])['payload'])[0]]
    ids = [str(item.get('recording_id') or item.get('id') or '') for item in items]
    require(all(ids) and len(set(ids)) == len(ids), 'checkpoint recording identity repeats or is missing')
    return items


def utc(value):
    return instant(value).isoformat().replace('+00:00', 'Z')


def recording_identity(proposal, ledger, item):
    """Bind an unchanged sealed recording to its exact original provider item.

    Only exact timestamps or the historical collector's precise minute renderer
    are recognized; there is no tolerance, cross-source rewriting or epsilon.
    """
    events = artifacts._source_events(proposal, ledger)
    artifacts._validate_source_target(proposal, events, publisher.stable_review_id(proposal))
    identity = artifacts._native_meeting_identity(events, proposal)
    require(identity == ('fathom', str(item.get('recording_id') or item.get('id')),
                         item.get('share_url') or item.get('url')), 'provider recording identity differs')
    recordings, errors = pipeline._recording_events(events, ledger['manifest'])
    require(not errors and len(recordings) == 1, 'canonical recording source is ambiguous')
    recording = recordings[0]
    require(recording['meeting'].canonical_id == proposal['provenance']['canonical_meeting_id']
        and set(recording['source_evidence_ids']) == set(proposal['provenance']['evidence_ids']),
        'canonical recording citation membership differs')
    title = item.get('title') or item.get('meeting_title')
    require(isinstance(title, str) and title.strip()
        and recording['meeting'].title == title, 'provider recording title differs')
    lo, hi = instant(item['recording_start_time']), instant(item['recording_end_time'])
    require(hi > lo, 'provider precise recording interval is invalid')
    representative = next(e for e in events if e['source_type'] == 'fathom')
    own_lo, own_hi = pipeline._canonical_meeting_span(recording['meeting'], representative)
    require((instant(proposal['start']), instant(proposal['end'])) == (own_lo, own_hi),
        'original identity witness is not its full sealed recording')
    rendered = tuple(pipeline._parse_dt(collector.local_dt_string(t)) for t in (lo, hi))
    require((own_lo, own_hi) in ((lo, hi), rendered), 'historical minute recording projection differs')
    return utc(item['recording_start_time']), utc(item['recording_end_time'])


def pending_intervals(proposal, review_ids, current):
    """Physical rows may split an old activity, without inventing old proposals."""
    require(isinstance(review_ids, list) and review_ids and len(set(review_ids)) == len(review_ids),
        'represented pending row inventory is invalid')
    result = []
    for segment, rid in enumerate(review_ids, 1):
        require(rid == f'{proposal["review_activity_key"]}-s{segment:02d}' and rid in current,
            'represented row is not an original activity segment')
        number, row = current[rid]
        require(type(number) is int and number > 1 and len(row) == 15 and row[0] == rid
            and row[6] == proposal['activity_id'] and row[9] == 'pending' and row[13] == 'unposted',
            'physical pending row identity, activity or human disposition differs')
        raw_whole_seconds(row[1])
        raw_whole_seconds(row[2])
        start, end = pipeline._parse_dt(row[1]), pipeline._parse_dt(row[2])
        require(start is not None and end is not None and end > start
            and not start.microsecond and not end.microsecond,
            'physical pending interval is invalid')
        seconds = int((end - start).total_seconds())
        require(type(row[3]) in (int, float) and row[3] == seconds / 60,
            'physical pending duration differs from exact interval')
        result.append((utc(start.isoformat()), utc(end.isoformat())))
    return result


def manual_intersection(proof, proposal, item, pending_intervals_, config, cache):
    """One saved explicit Daily same-meeting assessment, never POST authority.

    A positive *same_meeting_credit_seconds* in the original diagnostic is the
    explicit factual pairing authority. An overlapping interval alone is not.
    The original complete inventory wire and receipt remain immutable; today's
    exact-entry GET is a different observation, not a fabricated month receipt.
    """
    from scripts import clockify_native_sheet_post as native
    require(set(proof) == {'assessment', 'wire', 'read_receipt', 'current'},
        'saved manual assessment proof inventory differs')
    load = lambda h: json.loads(artifacts._capture(h, cache))
    assessment, wire, receipt, current = (load(proof[k]) for k in ('assessment', 'wire', 'read_receipt', 'current'))
    require(assessment.get('schema', assessment.get('schema_version')) == 'october-unresolved-review-packet/v1',
        'original manual assessment type differs')
    daily = assessment['meeting_duplicate_assessment']['daily']
    require(str(daily['recording_id']) == str(item.get('recording_id') or item.get('id'))
        and daily['canonical_id'] == proposal['provenance']['canonical_meeting_id']
        and type(daily['same_meeting_credit_seconds']) is int and daily['same_meeting_credit_seconds'] > 0,
        'saved explicit same-meeting identity or authority differs')
    target = (config['workspace_id'], config['member_id'])
    require((wire.get('workspace_id'), wire.get('user_id')) == target
        and (receipt.get('workspace_id'), receipt.get('user_id')) == target
        and receipt.get('schema', receipt.get('schema_version')) == 'clockify-complete-month-read/v1'
        and receipt.get('clockify_writes') == 0 and receipt.get('complete') is True
        and receipt.get('intervals_closed_complete') is True and receipt.get('invalid_interval_count') == 0
        and receipt.get('open_timer_count') == 0 and receipt.get('terminal_empty_page') is True
        and receipt.get('wire_evidence_sha256', '').removeprefix('sha256:') == proof['wire']['sha256'][7:],
        'original complete native read authority differs')
    since, until = instant(receipt['since_utc']), instant(receipt['until_utc'])
    # Observation metadata legitimately has subsecond precision; only actual
    # recording/native interval boundaries require exact whole seconds.
    observed = pending._time(receipt['observation_as_of_utc']).astimezone(dt.UTC)
    require(since < until and since <= instant(item['recording_start_time'])
        < instant(item['recording_end_time']) <= until, 'original native period does not contain recording')
    pages = wire['pages']
    require(isinstance(pages, list) and pages and len(pages) == len(receipt['pages'])
        and pages[-1]['payload'] == [], 'original native wire lacks terminal page')
    entries = []
    for index, (page, saved) in enumerate(zip(pages, receipt['pages'], strict=True), 1):
        require(page['page'] == saved['page'] == index and type(page['page_size']) is int
            and page['page_size'] > 0 and isinstance(page['request_path'], str) and page['request_path']
            and isinstance(page['payload'], list) and saved['wire_payload_count'] == len(page['payload'])
            and (index == len(pages) or page['payload']), 'original native page identity/count differs')
        entries.extend(page['payload'])
    matches = [e for e in entries if e.get('id') == daily['clockify_entry_id']]
    require(len(matches) == 1, 'manual original native identity is absent or duplicated')
    entry = matches[0]
    require(daily['native_entry_digest'].removeprefix('sha256:') == native._digest(entry)
        and daily['native_interval'] == entry['timeInterval'] and entry.get('type') == 'REGULAR',
        'manual original native digest or interval differs')
    interval = entry['timeInterval']
    lo, hi = instant(interval['start']), instant(interval['end'])
    require(hi > lo, 'manual native entry is open or reversed')
    payload = {k: entry.get(k) for k in ('description', 'projectId', 'taskId', 'tagIds', 'billable')}
    payload.update(start=utc(interval['start']), end=utc(interval['end']))
    require(type(payload['billable']) is bool and isinstance(payload['tagIds'], list)
        and artifacts.current_live_matches(payload, entry, workspace_id=target[0], member_id=target[1],
            entry_id=daily['clockify_entry_id']), 'manual original native target differs')
    require(current.get('read_only') is True and (current.get('workspace_id'), current.get('user_id')) == target
        and pending._time(current['captured_at']).astimezone(dt.UTC) >= observed,
        'current exact-entry observation target/time differs')
    live = [e for e in current['entries'] if e.get('id') == daily['clockify_entry_id']]
    require(len(live) == 1 and live[0].get('type') == 'REGULAR', 'current exact native entry type is not REGULAR')
    instant(live[0]['timeInterval']['start'])
    instant(live[0]['timeInterval']['end'])
    require(artifacts.current_live_matches(payload, live[0], workspace_id=target[0],
        member_id=target[1], entry_id=daily['clockify_entry_id']), 'current exact native payload or target differs')
    start, end = instant(item['recording_start_time']), instant(item['recording_end_time'])
    a, b = max(start, lo), min(end, hi)
    require(a < b and int((b - a).total_seconds()) == daily['same_meeting_credit_seconds']
        and type(daily['exact_recording_seconds']) is int
        and int((end - start).total_seconds()) == daily['exact_recording_seconds'],
        'saved same-meeting precise recording/intersection differs')
    residual = []
    for segment in daily['residual_segments']:
        require(type(segment['seconds']) is int and segment['seconds'] > 0
            and int((instant(segment['end']) - instant(segment['start'])).total_seconds()) == segment['seconds'],
            'saved manual residual exact seconds differ')
        residual.append((utc(segment['start']), utc(segment['end'])))
    require(residual == pending_intervals_ and type(daily['residual_seconds']) is int
        and sum(s['seconds'] for s in daily['residual_segments']) == daily['residual_seconds']
        and daily['residual_seconds'] + daily['same_meeting_credit_seconds'] == daily['exact_recording_seconds'],
        'saved manual residual physical pending partition differs')
    return utc(a.isoformat()), utc(b.isoformat())


def verify(config, source_stage, replay_stage, proofs, *, title):
    cache = {}
    load = lambda h: json.loads(artifacts._capture(h, cache))
    document = load(proofs['selection'])
    fields = {'schema_version', 'source_pair', 'source_semantic_analysis', 'attendance_authority',
              'fathom_checkpoint', 'represented_sources', 'recordings', 'spreadsheet_id', 'sheet_title',
              'sheet_id', 'primary_capture', 'diagnostics'} | set(SCOPE)
    require(set(document) == fields and document['schema_version'] == SCHEMA,
        'selected availability contract inventory differs')
    availability_scope(document)
    require((document['spreadsheet_id'], document['sheet_title']) == (str(config['spreadsheet_id']), title),
        'primary destination differs')
    source, replay = completed.completed_pair(document['source_pair'], cache)
    completed.stage_matches(source_stage, source, load)
    completed.stage_matches(replay_stage, replay, load)
    root = Path(source['artifacts']['receipt']['path']).parent
    require(source['proposals'] == source['accounting']['proposals']
        and Path(document['source_semantic_analysis']['path']) == root / 'semantic-analysis.json',
        'full completed source or semantic locator differs')
    analysis = load(document['source_semantic_analysis'])
    require(analysis.get('activities') == [], 'source contains general semantic activities')
    descriptions, _ = batches._recorded_attendance_descriptions(source['proposals'], analysis,
        root, source['routing'], captured={'sources': document['attendance_authority']})
    require(set(descriptions) == set(source['by_review_id']) and descriptions,
        'source is not exhaustive genuine factual recorded attendance')
    items = recording_checkpoint(document['fathom_checkpoint'], cache)
    by_recording = {str(i.get('recording_id') or i.get('id')): i for i in items}
    from scripts import clockify_mixed_review_availability as mixed
    bound, bound_raw = mixed.native_rows(load(document['primary_capture']), document['spreadsheet_id'],
        title, document['sheet_id'], 15, header=publisher.HEADER)
    current, current_raw = mixed.native_rows(load(proofs['live_readback']), document['spreadsheet_id'],
        title, document['sheet_id'], 15, header=publisher.HEADER)
    require(current_raw == bound_raw, 'primary current native CellData differs from bound observation')
    originals = {}
    for name, handles in document['represented_sources'].items():
        require(set(handles) == {'accounting', 'ledger'}, 'old factual identity witness inventory differs')
        originals[name] = {k: load(h) for k, h in handles.items()}
    ids, used_rows, used_recordings, results = set(), set(), set(), []
    for record in document['recordings']:
        require(set(record) == {'source_review_id', 'source_proposal_sha256', 'recording_id',
            'provider_item_sha256', 'represented_source', 'original_review_id', 'original_proposal_sha256',
            'pending_review_ids', 'manual_assessment'}, 'recording availability binding inventory differs')
        rid = record['source_review_id']
        require(rid not in ids and rid in source['by_review_id'], 'source review identity repeats or is foreign')
        ids.add(rid)
        provider_id = str(record['recording_id'])
        require(provider_id not in used_recordings and provider_id in by_recording,
            'represented provider recording is absent or repeated')
        used_recordings.add(provider_id)
        item, proposal = by_recording[provider_id], source['by_review_id'][rid]
        require(pending.digest(item) == record['provider_item_sha256']
            and pending.digest(proposal) == record['source_proposal_sha256'],
            'source proposal or raw provider item digest differs')
        start, end = recording_identity(proposal, source['ledger'], item)
        old = originals[record['represented_source']]
        original = artifacts._source(old['accounting']['proposals'], old['ledger'], record['original_review_id'])
        require(pending.digest(original) == record['original_proposal_sha256']
            and recording_identity(original, old['ledger'], item) == (start, end),
            'old factual activity/recording identity differs')
        row_ids = record['pending_review_ids']
        require(not used_rows.intersection(row_ids), 'physical pending row reused by recordings')
        used_rows.update(row_ids)
        intervals = pending_intervals(original, row_ids, current)
        pending_seconds = sum(int((instant(b) - instant(a)).total_seconds()) for a, b in intervals)
        saved_seconds = 0
        if record['manual_assessment'] is not None:
            intersection = manual_intersection(record['manual_assessment'], original, item, intervals, config, cache)
            intervals = [*intervals, intersection]
            saved_seconds = int((instant(intersection[1]) - instant(intersection[0])).total_seconds())
        precise_seconds = exact_partition(start, end, intervals)
        results.append({'source_review_id': rid, 'historical_source_seconds': proposal['duration_seconds'],
            'precise_recording_seconds': precise_seconds, 'pending_review_ids': row_ids,
            'pending_seconds': pending_seconds, 'existing_same_meeting_intersection_seconds': saved_seconds,
            'financial_equivalence_claimed': False, 'additional_credited_seconds': 0})
    require(ids == set(source['by_review_id']), 'source recording availability partition is not exhaustive')
    diagnostic = {'status': 'missing', 'review_availability_only': True,
                  'diagnostic_resolution_claimed': False, 'missing': ['authentic_diagnostic_availability']}
    if document['diagnostics'] is not None:
        diagnostic = verify_diagnostics(document['diagnostics'], root, config, title, load)
    return {**SCOPE, 'source_review_availability': {'status': 'complete', 'review_ids': sorted(ids),
            'review_availability_only': True}, 'recordings': results, 'selected_review_ids': [],
        'selected_minutes': 0, 'retained_pending_review_ids': sorted(used_rows),
        'diagnostics': diagnostic, 'posted_credits_created': 0, 'adoption_provider_writes': 0,
        'historical_allocation_or_publication_equivalence_claimed': False,
        'literal_canonical_primary_delivery_claimed': False,
        'authority_boundary': 'Original accounting and ledger establish factual recording/activity identity only; '
            'current physical native rows and saved same-meeting GET assessment cover precise attendance, '
            'not historical allocation/publication, payable equivalence, new credit or source-debt resolution.',
        'consumer_runtime': pending.artifact_handle(Path(__file__).resolve()), 'proofs': dict(proofs)}


def verify_diagnostics(proof, source, config, primary_title, load):
    """Verify a genuine native monthly append, retaining explicit provenance debt.

    This does not turn unavailable historical producers into canonical aliases.
    The mixed retained digest in the native result is deliberately NOT the
    current canonical all-provenance digest. No diagnostic is resolved here.
    """
    from scripts import clockify_monthly_unresolved as monthly
    from scripts import clockify_mixed_review_availability as mixed
    from scripts import clockify_selected_delivery_adoption as selected
    require(set(proof) == {'publication_result', 'before', 'after', 'live_readback'},
        'native diagnostic proof inventory differs')
    result = load(proof['publication_result'])
    title, spreadsheet = monthly.title_for_review(primary_title), str(config['spreadsheet_id'])
    require((result['spreadsheet_id'], result['sheet_title']) == (spreadsheet, title),
        'native diagnostic result target differs')
    debt = result['retained_provenance_debt']
    readback = result['retained_target_readback']
    sid, layout = readback['sheet_id'], readback['layout']
    header = monthly.LEGACY_HEADER if layout == monthly.LEGACY_LAYOUT else monthly.HEADER
    canonical = monthly.retained_projection(source, layout=layout)
    digests = monthly.validate_retained_debt(source, canonical, debt,
        spreadsheet_id=spreadsheet, sheet_title=title, layout=layout)
    monthly.validate_retained_readback(readback, debt, spreadsheet_id=spreadsheet, sheet_title=title)
    row_ids = [r[0] for r in canonical]
    expected = publisher._publication_receipt(spreadsheet_id=spreadsheet, sheet_title=title,
        rows=canonical, rows_digest=monthly.retained_rows_digest(row_ids, digests))
    require(all(result.get(k) == v for k, v in expected.items()), 'native mixed retained receipt digest differs')
    parsed = []
    for key in ('before', 'after', 'live_readback'):
        document = load(proof[key])
        cells = selected._capture_rows(document.get('readback', document), spreadsheet, title, sid, 12)
        by_id, native = mixed.native_rows(document, spreadsheet, title, sid, 12, header=header)
        require(sorted(cells) == list(range(1, len(cells) + 1)),
            'native diagnostic capture is not a complete contiguous prefix')
        parsed.append((cells, by_id, native))
    (before, old, before_native), (after, observed, after_native), (_, live, live_native) = parsed
    require(after_native == live_native and all(after_native.get(n) == r for n, r in before_native.items()),
        'native diagnostic prefix, human disposition or current CellData changed')
    require(set(row_ids) <= set(observed), 'diagnostic availability is not exhaustive')
    debt_by_id = {entry['stable_evidence_id']: entry for entry in debt}
    appends = [r for r in canonical if r[0] not in old]
    require(result['appended'] == len(appends) and result['unchanged'] == len(canonical) - len(appends)
        and len(after) == len(before) + len(appends)
        and [after[n] for n in range(len(before) + 1, len(after) + 1)] == appends,
        'native literal diagnostic append partition differs')
    for row in canonical:
        rid = row[0]
        _, actual = observed[rid]
        if rid in debt_by_id:
            require(rid in old and old[rid] == observed[rid] and actual[:10] == row[:10]
                and monthly.retained_row_digest(actual) == debt_by_id[rid]['retained_machine_digest'],
                'retained diagnostic debt preimage differs')
            rebuilt = monthly.prepare_retained_debt(source, row, actual, spreadsheet_id=spreadsheet,
                sheet_title=title, layout=layout)
            require(rebuilt == debt_by_id[rid], 'retained unavailable historical source debt differs')
        else:
            require(actual[:10] == row[:10] and actual[11] == row[11],
                'literal diagnostic source machine cells differ')
    return {'status': 'complete', 'review_ids': row_ids, 'review_availability_only': True,
        'diagnostic_resolution_claimed': False, 'source_debt_resolution_claimed': False,
        'retained_provenance_debt_count': len(debt), 'retained_provenance_debt': debt,
        'literal_appended_review_ids': [r[0] for r in appends], 'additional_minutes': 0,
        'literal_canonical_all_cells_claimed': False}
