"""Typed diagnostic REVIEW AVAILABILITY, never resolution or financial credit.

Literal current publications and retained source representations are distinct.
Changed kinds remain changed kinds; strict canonical alias APIs are untouched.
Only explicit saved source/receipt/native-capture handles are read.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_source_adoptions as artifacts
from scripts import clockify_source_event_correspondence as correspondence

FILES = {'work-accounting-result.json','proposals.json','ambiguous.json',
         'evidence/evidence-ledger.json','semantic-analysis.json','run-report.json'}
CHANGED_WARNING = ('Changed diagnostic kind is a review view of the same exact evidence, never additional work or resolution of the original diagnostic. '
    'The original low-confidence row already preserves the unconfirmed source; this rerun supplies no new actionable source evidence or valid interval, so no additional visible row is proposed.')
SAME_WARNING = ('Same exact evidence and kind does not prove completed outcome, equivalent payable work, or duplicate time. '
    'Source reason/presentation differences are retained as comparison warnings only.')
VIEW_WARNING = ('Diagnostic view only: no independently confirmed work interval or additional time; '
    'same-work/duplicate status against actual Clockify remains unknown.')


def require(condition, message):
    if not condition:
        raise ValueError('native diagnostic review availability: '+message)


def digest(value):
    return 'sha256:'+hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def instant(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'timestamp lacks timezone')
    return parsed.astimezone(dt.timezone.utc)


def evidence_ids(item, analysis):
    ids = item.get('evidence_ids') or (item.get('provenance') or {}).get('evidence_ids')
    if not ids:
        matches = [a for a in analysis['activities'] if a.get('activity_id') == item.get('activity_id')]
        require(len(matches) == 1, 'exception lacks unique cited activity')
        ids = matches[0].get('evidence_ids')
    require(isinstance(ids,list) and ids and all(isinstance(i,str) and i for i in ids), 'invalid evidence membership')
    return sorted(set(ids))


class SourceGraph:
    """Byte-bound original source readers, with independent native re-projection."""
    def __init__(self, pins, load):
        self.pins, self.load, self.cache = pins, load, {}

    def source(self, root):
        root = Path(root)
        if root not in self.cache:
            handles = {}
            for name in FILES:
                path = str(root/name)
                require(path in self.pins, 'source artifact lacks explicit pin')
                handles[name] = {'path':path,'sha256':self.pins[path]['sha256']}
            documents = {name:self.load(handle) for name,handle in handles.items()}
            rows = {r[0]:r for r in monthly.project_rows(root)}
            for row in rows.values():
                lineage = json.loads(row[11])
                require(lineage['source_run_id'] == root.name
                    and lineage['artifacts'] == {name:h['sha256'] for name,h in handles.items()},
                    'projection source bytes differ from bound graph')
            events,sha = monthly._ledger_events(root)
            require(sha == handles['evidence/evidence-ledger.json']['sha256'], 'ledger bytes differ from bound graph')
            self.cache[root] = (documents,rows,events,handles)
        return self.cache[root]

    def exception(self, root, row):
        documents,_,_,_ = self.source(root)
        ids = json.loads(row[11])['evidence_ids']
        matches = [item for item in documents['ambiguous.json']
                   if item.get('exception_kind') == row[3]
                   and evidence_ids(item,documents['semantic-analysis.json']) == ids]
        require(len(matches) == 1, 'canonical diagnostic lacks exact unique native exception')
        return matches[0]


def paired_events(current_ids, prior_ids, current_events, prior_events):
    result,used = [],set()
    for current_id in current_ids:
        a = current_events[current_id]
        matches = [old for old in prior_ids if a == prior_events[old]
                   or correspondence.corresponds(a,prior_events[old],historical_timezone='Europe/Bucharest')]
        require(len(matches) <= 1, 'source-event correspondence is ambiguous')
        if matches:
            old = matches[0]
            require(old not in used, 'source-event correspondence repeats a member')
            used.add(old);result.append((current_id,old))
    return result


def verify_retained(mapping, *, source, canonical, graph, cells, native_cells):
    current_id,represented = mapping['current_diagnostic_id'],mapping['represented_diagnostic_id']
    require(current_id in canonical and represented in cells, 'retained identity is foreign or missing')
    requested = canonical[current_id]
    current_docs,_,current_events,current_handles = graph.source(source)
    prior_root = Path(mapping['represented_source'])
    prior_docs,prior_rows,prior_events,prior_handles = graph.source(prior_root)
    require(represented in prior_rows, 'represented ID is not native to its declared source')
    old = monthly.rows_for_layout([prior_rows[represented]],monthly.LEGACY_LAYOUT)[0]
    number,actual = cells[represented]
    require(actual == old == mapping['represented_current_native_cells']
        and native_cells[number] == mapping['represented_current_native_cell_data']
        and number == mapping['represented_current_sheet_row']
        and actual[10] == mapping['human_disposition_preserved']
        and monthly.machine_digest(actual) == mapping['represented_machine_digest'],
        'retained native cells, disposition or machine digest differ')
    require(mapping['current_source'] == str(source)
        and mapping['current_source_handles'] == current_handles
        and mapping['represented_source_handles'] == {name:prior_handles[name] for name in (
            'evidence/evidence-ledger.json','ambiguous.json','semantic-analysis.json','work-accounting-result.json')},
        'retained source locator or artifact inventory differs')
    current_exception = graph.exception(source,requested)
    prior_exception = graph.exception(prior_root,prior_rows[represented])
    current_ids,prior_ids = json.loads(requested[11])['evidence_ids'],json.loads(old[11])['evidence_ids']
    require(mapping['current_canonical_row'] == requested
        and mapping['current_canonical_row_sha256'] == digest(requested)
        and mapping['current_native_exception'] == current_exception
        and mapping['current_exception_sha256'] == digest(current_exception)
        and mapping['represented_native_exception'] == prior_exception
        and mapping['represented_native_exception_sha256'] == digest(prior_exception)
        and mapping['current_evidence_ids'] == current_ids and mapping['represented_evidence_ids'] == prior_ids,
        'retained canonical exception or evidence membership differs')
    same = requested[3] == old[3]
    mode = 'retained-same-evidence-and-kind' if same else 'retained-same-evidence-changed-kind'
    require(mapping['current_kind'] == requested[3]
        and mapping['represented_native_kind'] == mapping['represented_visible_kind'] == old[3]
        and mapping['representation_mode'] == mode and mapping['same_kind_alias_claimed'] is same
        and mapping['warning'] == (SAME_WARNING if same else CHANGED_WARNING),
        'diagnostic kinds or review-only warning were relabelled')
    pairs = paired_events(current_ids,prior_ids,current_events,prior_events)
    declarations = mapping['evidence_pairs']
    require(len(pairs) == len(current_ids) == len(prior_ids) == len(declarations)
        and {(p['current_evidence_id'],p['represented_evidence_id']) for p in declarations} == set(pairs),
        'retained evidence correspondence is not one-to-one and exhaustive')
    for pair in declarations:
        a,b = current_events[pair['current_evidence_id']],prior_events[pair['represented_evidence_id']]
        require(pair['current_sealed_atom'] == a and pair['represented_sealed_atom'] == b
            and pair['current_object_sha256'] == digest(a) and pair['represented_object_sha256'] == digest(b)
            and pair['relation'] == ('exact-sealed-event' if a == b else 'stable-sealed-source-event-correspondence'),
            'retained sealed source atom was substituted')
    require(mapping['review_availability_only'] is True and mapping['literal_current_canonical_delivery_claimed'] is False
        and mapping['work_completed_or_posted_credit_inferred'] is False
        and mapping['additional_minutes'] == 0 and mapping['diagnostic_work_interval_status'] == 'unknown',
        'retained representation claims resolution, work or credit')
    return {'current_diagnostic_id':current_id,'represented_diagnostic_id':represented,
        'current_kind':requested[3],'represented_kind':old[3],'representation_mode':mode,
        'warning':mapping['warning'],'review_availability_only':True}


def actual_entries(document, config):
    require(document.get('complete') is True and document.get('intervals_closed_complete') is True
        and document.get('terminal_empty_page') is True and document.get('invalid_interval_count') == 0
        and document.get('open_timer_count') == 0
        and (document['workspace_id'],document['user_id']) == (config['workspace_id'],config['member_id']),
        'actual-entry warning observation is incomplete or foreign')
    entries = {}
    for page in document['pages']:
        for entry in page['payload']:
            require(entry['id'] not in entries or entries[entry['id']] == entry, 'conflicting actual entry payload')
            a,b = instant(entry['timeInterval']['start']),instant(entry['timeInterval']['end'])
            require(a < b, 'actual entry lacks valid closed interval')
            entries[entry['id']] = entry
    require(len(entries) == document['entry_count'], 'actual entry observation count differs')
    return entries


def context_warning(exception, entries):
    details,counterparts = [],[]
    for index,context in enumerate(exception.get('timing_context_intervals',[])):
        try:
            lo,hi = (instant(context['start']),instant(context['end'])) if isinstance(context,dict) else map(instant,context)
            if lo >= hi:continue
        except (ValueError,TypeError,KeyError):
            continue  # Invalid metadata is not promoted into a work interval.
        for entry in entries.values():
            a,b = instant(entry['timeInterval']['start']),instant(entry['timeInterval']['end'])
            start,end = max(lo,a),min(hi,b)
            if start >= end:continue
            seconds = (end-start).total_seconds()
            details.append(f"Source context {lo.isoformat()} to {hi.isoformat()} overlaps Clockify entry {entry['id']} by {seconds:g} seconds (overlap {start.isoformat()} to {end.isoformat()}; actual entry {a.isoformat()} to {b.isoformat()}).")
            counterparts.append({'context_index':index,'source_context_start':lo.isoformat(),'source_context_end':hi.isoformat(),
                'original_native_context_digest':digest(context),'entry_id':entry['id'],'actual_payload_sha256':digest(entry),
                'actual_start':a.isoformat(),'actual_end':b.isoformat(),'overlap_start':start.isoformat(),'overlap_end':end.isoformat(),
                'overlap_seconds':seconds,'warning_only':True,'owned_work_interval_or_duplicate_inferred':False})
    return (' '.join(details)+' Shared source context only: no owned work interval or additional minutes; same-work/duplicate status remains unknown.' if details else ''),counterparts


def grid(document, spreadsheet, title, sheet_id):
    from scripts import clockify_selected_delivery_adoption as selected
    capture = document.get('readback',document)
    rows = selected._capture_rows(capture,spreadsheet,title,sheet_id,12)
    native = capture.get('structuredContent',capture)
    sheets = [s for s in native['sheets'] if s['properties']['sheetId'] == sheet_id and s['properties']['title'] == title]
    require(len(sheets) == 1, 'native sheet is ambiguous')
    cell_data = {}
    for block in sheets[0]['data']:
        for number,row in enumerate(block['rowData'],block.get('startRow',0)+1):
            require(len(row.get('values',[])) <= 12, 'native diagnostic row is wider than12')
            cell_data[number] = row
    require(sorted(rows) == list(range(1,len(rows)+1)) and rows.get(1) == monthly.LEGACY_HEADER,
        'full contiguous original legacy diagnostic capture is missing')
    return selected._by_identity({n:r for n,r in rows.items() if n > 1}),cell_data


def verify(config: Mapping[str,Any], source: Path, title: str, proofs, documents):
    """Reconstruct every source/member/cell relation; no producer summary trust."""
    cache = {}
    raw = lambda handle:artifacts._capture(handle,cache)
    load = lambda handle:json.loads(raw(handle))
    proof,pub,current = (documents[name] for name in ('unresolved_packet','unresolved_receipt','header_readback'))
    require(proof['source'] == str(source) and proof['published_receipt'] == proofs['unresolved_receipt'],
        'publication authority is not bound to the exact source')
    pins = proof['input_pins']
    for path,pin in pins.items():raw({'path':path,'sha256':pin['sha256']})
    graph = SourceGraph(pins,load)
    source_docs,canonical,current_events,_ = graph.source(source)
    require(digest(list(canonical.values())) == proof['source_projection_sha256'], 'source projection differs')
    spreadsheet = str(config['spreadsheet_id']);diagnostic_title = monthly.title_for_review(title)
    require(pub.get('schema_version') == 'clockify-root-diagnostic-publication-readback/v1'
        and (pub['spreadsheet_id'],pub['sheet_title']) == (spreadsheet,diagnostic_title)
        and pub['clockify_writes'] == pub['new_minutes'] == 0 and pub['adoption_executed'] is False,
        'actual diagnostic publication receipt differs')
    sid = pub['sheet_id']
    published,published_native = grid(pub,spreadsheet,diagnostic_title,sid)
    live,live_native = grid(current,spreadsheet,diagnostic_title,sid)
    require(all(number in live_native and live_native[number] == row for number,row in published_native.items()),
        'current native capture is stale or cells/dispositions changed')
    prepublication = load(pub['source_receipt'])
    before,before_native = grid(load(prepublication['latest_native_preimage']),spreadsheet,diagnostic_title,sid)
    require(prepublication['existing_native_cell_data'] == [before_native[n] for n in sorted(before_native)]
        and all(published_native.get(n) == row for n,row in before_native.items()),
        'original retained native preimage changed')
    records = proof['published_current_diagnostics'];retained = proof['retained_representation_mappings']
    published_ids = [r['current_diagnostic_id'] for r in records]
    retained_ids = [r['current_diagnostic_id'] for r in retained]
    represented_ids = published_ids+[r['represented_diagnostic_id'] for r in retained]
    require(len(set(published_ids+retained_ids)) == len(published_ids)+len(retained_ids)
        and set(published_ids+retained_ids) == set(canonical)
        and len(set(represented_ids)) == len(represented_ids), 'review mapping is not disjoint and exhaustive')
    match = re.fullmatch(r'A([1-9][0-9]*):L([1-9][0-9]*)',pub['range'])
    require(match is not None, 'publication range differs')
    start,end = int(match[1]),int(match[2])
    rows = load(prepublication['appendable_rows'])
    require(start == len(before_native)+1 and len(rows) == len(records) == pub['rows'] == end-start+1,
        'bounded literal publication count differs')
    comparison = load(prepublication['all114_dispositions'])
    require(comparison['source_run'] == str(source), 'editorial comparison source differs')
    comparison_ids = [r['diagnostic_id'] for r in comparison['results']]
    require(len(set(comparison_ids)) == len(canonical) and set(comparison_ids) == set(canonical), 'comparison identity differs')
    compared = {r['diagnostic_id']:r for r in comparison['results']}
    editorial = load(prepublication['j_only_editorial_projection_bindings'])
    require(editorial['comparison_report'] == prepublication['all114_dispositions']
        and editorial['actual_capture'] == pub['fresh_clockify'], 'J-only warning source differs')
    bindings = {b['diagnostic_id']:b for b in editorial['bindings']}
    require(len(bindings) == len(editorial['bindings']) == len(records) and set(bindings) == set(published_ids), 'J-warning identity differs')
    entries = actual_entries(load(editorial['actual_capture']),config)
    inventory_path = str(Path(prepublication['all114_dispositions']['path']).with_name('origin-inventory-v2.private.json'))
    require(inventory_path in pins, 'shared-source original inventory lacks explicit handle')
    inventory = load({'path':inventory_path,'sha256':pins[inventory_path]['sha256']})
    originals = {r['stable_id']:r for r in inventory['bound']}
    require(len(originals) == len(before) and set(originals) == set(before), 'original source inventory differs')
    for offset,(record,row) in enumerate(zip(records,rows,strict=True),start):
        cid = record['current_diagnostic_id'];canonical_row = canonical[cid]
        legacy = monthly.rows_for_layout([canonical_row],monthly.LEGACY_LAYOUT)[0]
        native_exception = graph.exception(source,canonical_row)
        item,binding = compared[cid],bindings[cid]
        require(item['source_projection'] == canonical_row and item['legacy_projection'] == legacy
            and item['native_exception'] == native_exception, 'published exception source differs')
        warning = VIEW_WARNING
        if item['source_member_correspondence'] == 'some-members-represented':
            current_ids = json.loads(canonical_row[11])['evidence_ids'];matched = set()
            for relation in item['existing_relations']:
                oid = relation['existing_id'];require(oid in originals and oid in before,'shared warning counterpart is foreign')
                origin = originals[oid];oldroot = Path(origin['source_run'])
                olddocs,oldrows,oldevents,_ = graph.source(oldroot)
                require(oid in oldrows and monthly.rows_for_layout([oldrows[oid]],monthly.LEGACY_LAYOUT)[0] == before[oid][1],
                    'shared warning counterpart differs from original source')
                oldids = json.loads(oldrows[oid][11])['evidence_ids']
                pairs = paired_events(current_ids,oldids,current_events,oldevents)
                require(len(pairs) == relation['matched_evidence_count'] and relation['selected_evidence_count'] == len(current_ids)
                    and relation['original_evidence_count'] == len(oldids) and relation['existing_row'] == before[oid][0]
                    and set(pairs) == {(p['selected_evidence_id'],p['original_evidence_id']) for p in relation['evidence_pairs']},
                    'shared warning evidence membership differs')
                matched.update(a for a,_ in pairs)
                warning += f" Shared source with {oid} (existing row {before[oid][0]}): {len(pairs)}/{len(current_ids)} members; not complete diagnostic or outcome coverage."
            require(matched and matched < set(current_ids), 'shared warning is not a partial source relation')
        context,counterparts = context_warning(native_exception,entries)
        expected = list(legacy);expected[9] += ' '+warning+(' '+context if context else '')
        require(row == expected == record['published_cells'] == published[cid][1] == live[cid][1]
            and published[cid][0] == offset == record['sheet_row'] and cid not in before
            and record['publication_receipt'] == proofs['unresolved_receipt']
            and record['published_full_native_cell_data'] == published_native[offset]
            and record['published_machine_digest'] == monthly.machine_digest(row)
            and record['source_canonical_row_sha256'] == digest(canonical_row)
            and record['human_disposition'] == row[10] == 'needs_review'
            and record['review_availability_only'] is True and record['additional_minutes'] == 0,
            'literal published cells, disposition or bounded J warning differ')
        require(binding['editorial_columns'] == ['J'] and binding['actual_capture'] == editorial['actual_capture']
            and binding['comparison_item_sha256'] == digest(item) and binding['original_legacy_row_sha256'] == digest(legacy)
            and binding['v3_j_value'] == row[9] and binding['v3_row_sha256'] == digest(row)
            and binding['actual_counterparts'] == counterparts and binding['new_owned_minutes'] == 0
            and binding['duplicate_status'] == 'unknown', 'J warning source/context binding differs')
    mappings = [verify_retained(m,source=source,canonical=canonical,graph=graph,cells=live,native_cells=live_native) for m in retained]
    warnings = [m for m in mappings if m['representation_mode'] == 'retained-same-evidence-changed-kind']
    return {'status':'complete','review_ids':list(canonical),'represented_review_ids':represented_ids,
        'literal_current_canonical_review_ids':published_ids,'historical_alias_ids':[],
        'retained_same_kind_count':len(mappings)-len(warnings),'retained_changed_kind_count':len(warnings),
        'retained_review_representations':mappings,'changed_kind_warnings':warnings,'review_availability_only':True,
        'diagnostic_resolution_claimed':False,'financial_coverage_claimed':False,
        'additional_minutes':0,'posted_credits_created':0,'provider_writes':0}
