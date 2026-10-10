"""Consume the original Sep29 supplement, never republish or create credit.

The packet's six review representations are five appends plus one retained
human row. A whole unresolved-routing proposal remains a diagnostic, not a
claim that all proposals were posted or financially covered.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from scripts import clockify_selected_delivery_adoption as selected
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_checkpoint_snapshot as native
from scripts import clockify_mixed_review_availability as mixed
from scripts import clockify_pending_review_selection as pending
from scripts import evidence_ledger, review_corrections


def require(condition, message):
    if not condition:
        raise ValueError('Sep29 original supplement: '+message)


def source_key(p):
    return (p['activity_id'], selected._time(p['start']), selected._time(p['end']),
        p['duration_minutes'], p['duration_seconds'], review_corrections.proposal_target(p)[1],
        tuple(sorted(p['provenance']['evidence_ids'])))


def declared_root(selection, names, *, exclude=()):
    """Select from exact original declarations, not from filesystem discovery."""
    candidates=[Path(path) for path,files in selection['input_inventory'].items()
        if Path(path) not in exclude and set(names)<=set(files)]
    require(len(candidates)==1, 'original declared artifact root is ambiguous or missing')
    return candidates[0]


def original_file(selection, root, name, raw):
    digest=selection['input_inventory'][str(root)][name]['sha256']
    return raw({'path':str(root/name),'sha256':'sha256:'+digest})


def publication_rows(packet, receipt, live, spreadsheet, title):
    require(receipt['schema_version']=='verified-sheet-publication-receipt/v1'
        and receipt['spreadsheet_id']==spreadsheet and receipt['exact_readback'] is True
        and receipt['user_dispositions_notes_preserved'] is True, 'original publication authority differs')
    sid=receipt['sheet_id']
    captured=selected._capture_rows(receipt['readback'],spreadsheet,title,sid,15)
    current=selected._capture_rows(live.get('readback',live),spreadsheet,title,sid,15)
    require(selected._time(live['captured_utc'])>=selected._time(
        receipt['utc'].replace(' UTC','Z').replace(' ','T')), 'current capture predates publication')
    match=re.fullmatch(r'A([1-9][0-9]*):O([1-9][0-9]*)',receipt['range'])
    require(match is not None, 'append range differs')
    numbers=list(range(int(match[1]),int(match[2])+1))
    existing=receipt['existing_row']
    require(type(existing) is int and existing>1 and existing not in numbers
        and type(receipt['rows']) is int and receipt['rows']==len(numbers), 'retained/append coordinates overlap or differ')
    rows=packet['rows']; by_id={row[0]:row for row in rows}
    require(rows and all(len(row)==15 for row in rows) and len(by_id)==len(rows)==len(numbers)+1,
        'primary review identities are duplicated or partition differs')
    original_ids=selected._by_identity(captured); current_ids=selected._by_identity(current)
    retained=captured.get(existing)
    require(retained is not None and retained[0] in by_id, 'original retained source identity is missing or foreign')
    rid=retained[0]; expected=by_id[rid]
    require(all(retained[i]==expected[i] for i in range(14))
        and retained[9]=='pending' and retained[13]=='unposted'
        and current.get(existing)==retained and current_ids.get(rid)==(existing,retained),
        'retained source/machine cells, disposition or original human note differs')
    appended=[row for row in rows if row[0]!=rid]
    require([captured.get(n) for n in numbers]==appended and [current.get(n) for n in numbers]==appended
        and all(original_ids.get(row[0])==(n,row) and current_ids.get(row[0])==(n,row)
            for n,row in zip(numbers,appended,strict=True)), 'exact original or current appended rows differ')
    require(receipt['minutes']==sum(row[3] for row in appended)
        and receipt['existing_minutes']==retained[3] and receipt['updated_columns']==['E','F','L','M'],
        'original appended/retained minutes or update inventory differs')
    appended_requests=[]; retained_columns=[]
    for request in receipt['requests']:
        require(set(request) in ({'updateCells'},{'copyPaste'}), 'unknown original sheet request')
        if 'copyPaste' in request:
            copy_request=request['copyPaste']; dest=copy_request['destination']
            require(copy_request['pasteType'] in {'PASTE_FORMAT','PASTE_DATA_VALIDATION'}
                and dest=={'sheetId':sid,'startRowIndex':numbers[0]-1,'endRowIndex':numbers[-1],
                    'startColumnIndex':0,'endColumnIndex':15}, 'original format/validation request differs')
            continue
        update=request['updateCells']; area=update['range']
        require(update['fields']=='userEnteredValue' and area['sheetId']==sid, 'original value update authority differs')
        values=[[next(iter(cell.get('userEnteredValue',{}).values()),'') for cell in row['values']] for row in update['rows']]
        if area=={'sheetId':sid,'startRowIndex':numbers[0]-1,'endRowIndex':numbers[-1],
                'startColumnIndex':0,'endColumnIndex':15}:
            require(values==appended, 'original append request cells differ')
            appended_requests.append(update)
        else:
            col=area['startColumnIndex']
            require(area=={'sheetId':sid,'startRowIndex':existing-1,'endRowIndex':existing,
                'startColumnIndex':col,'endColumnIndex':col+1} and col in (4,5,11,12)
                and values==[[retained[col]]], 'original retained update touches foreign or human cells')
            retained_columns.append(col)
    require(len(appended_requests)==1 and sorted(retained_columns)==[4,5,11,12], 'original write partition differs')
    return [row[0] for row in rows],[row[0] for row in appended],[rid]


def diagnostics(config, source, packet, proofs, documents, title):
    expected=monthly.project_rows(source)
    missing={'unresolved_packet','unresolved_receipt','header_readback'}-set(documents)
    if missing:
        return {'status':'diagnostic_proof_missing','missing':sorted(missing)}
    unresolved=documents['unresolved_packet']; receipt=documents['unresolved_receipt']
    require(unresolved['rows']==expected and unresolved['source_run']==str(source)
        and receipt['packet_sha256']==proofs['unresolved_packet']['sha256'][7:]
        and receipt['spreadsheet_id']==str(config['spreadsheet_id']), 'original diagnostic source/receipt differs')
    diagnostic_title=monthly.title_for_review(title); sid=receipt['sheet_id']
    old=selected._by_identity(selected._capture_rows(receipt['readback'],str(config['spreadsheet_id']),diagnostic_title,sid,12))
    current_doc=documents['header_readback']; capture=current_doc.get('readback',current_doc)
    rows=selected._capture_rows(capture,str(config['spreadsheet_id']),diagnostic_title,sid,12)
    require(rows.get(1) in (monthly.HEADER,monthly.LEGACY_HEADER), 'current diagnostic header differs')
    current=selected._by_identity(rows); observations=[]; unknown=[]; resolved=[]; links=[]
    primary=documents['live_readback']; primary=primary.get('readback',primary)
    primary_rows=selected._by_identity(selected._capture_rows(primary,str(config['spreadsheet_id']),
        title,documents['publication_receipt']['sheet_id'],15))
    source_review_ids={row[0] for row in packet['rows']}
    for canonical in expected:
        rid=canonical[0]
        require(rid in old and rid in current, 'current diagnostic source identity is absent')
        old_number,original=old[rid]; number,now=current[rid]
        legacy=monthly.rows_for_layout([canonical],monthly.LEGACY_LAYOUT)[0]
        require(original in (canonical,legacy) and number==old_number
            and all(now[i]==original[i] for i in range(12) if i not in (9,10))
            and now[10] in {'needs_review','resolved'}, 'current diagnostic source core or disposition differs')
        observations.append({'review_id':rid,'row_number':number,'observed_disposition':now[10],
            'current_row_sha256':pending.digest(now),'new_resolution_claimed':False})
        if now[10]=='resolved':resolved.append(rid)
        if now[9]!=original[9]:
            unknown.append({'review_id':rid,'status':'unknown_original_J_operation_lineage',
                'warning':mixed.UNKNOWN_J,'canonical_exact_delivery_claimed':False,'diagnostic_resolution_claimed':False})
            for linked_id in dict.fromkeys(re.findall(r'wka-[A-Za-z0-9-]+',str(now[9]))):
                observed=primary_rows.get(linked_id)
                link={'diagnostic_id':rid,'review_id':linked_id,'same_outcome_alias_claimed':False,
                    'financial_equivalence_claimed':False,'additional_credited_seconds':0,
                    'status':'not_in_bounded_native_capture'}
                if observed is not None and linked_id in source_review_ids:
                    link.update(status='verified_source_owned_native_review_observation',row_number=observed[0],
                        native_row_sha256=pending.digest(observed[1]))
                links.append(link)
    return {'status':'complete','review_ids':[row[0] for row in expected],'historical_alias_ids':[],
        'review_availability_only':True,'current_observations':observations,'existing_resolved_review_ids':resolved,
        'unknown_editorial_lineage':unknown,'diagnostic_resolution_claimed':False,'new_diagnostic_resolutions':0,
        'native_primary_link_observations':links,
        'financial_coverage_claimed':False,'additional_minutes':0}


def verify(config, source_stage, replay_stage, proofs, documents, *, title, raw, load):
    selection,packet,receipt=[documents[name] for name in ('selection','publication_packet','publication_receipt')]
    source=Path(source_stage['run_dir']); replay=Path(replay_stage['run_dir'])
    require(selection['schema_version']=='sep29-publication-validation/v1'
        and selection['source_run']==packet['source_run']==str(source)
        and selection['replay_run']==packet['replay_run']==str(replay)
        and packet['schema_version']=='verified-review-publication-supplement/v2'
        and packet['source_proposals_sha256']==source_stage['proposals_digest'][7:]
        and packet['canonical_header']==publisher.HEADER and packet['duration_reduced_for_overlap'] is False
        and packet['external_writes'] is False
        and selection['artifact_sha256']['review-publication.private.json']==proofs['publication_packet']['sha256'][7:]
        and receipt['packet_sha256']==proofs['publication_packet']['sha256'][7:], 'original selection/source/publication binding differs')
    proposals=load({'path':str(source/'proposals.json'),'sha256':source_stage['proposals_digest']})
    require(proposals==load({'path':str(replay/'proposals.json'),'sha256':replay_stage['proposals_digest']})
        and source_stage['accounting_digest']==replay_stage['accounting_digest'], 'exact completed source/replay differs')
    quality=load({'path':str(source/'quality_report.json'),'sha256':source_stage['quality_digest']})
    integrity=load({'path':str(replay/'replay-integrity.json'),'sha256':replay_stage['replay_integrity_digest']})
    publisher.verify_gates(proposals,quality,integrity,source.name)
    routing=load({'path':str(source/'routing.json'),'sha256':source_stage['snapshot_digests']['routing.json']})
    allowlist=publisher.project_allowlist(routing)
    require(publisher._routing_allowlist(source/'routing.json',integrity)==allowlist, 'source routing differs from sealed replay')
    by_activity={p['activity_id']:p for p in proposals}; by_review={publisher.stable_review_id(p):p for p in proposals}
    require(len(by_activity)==len(by_review)==len(proposals)
        and set(by_review)==set(source_stage['review_ids'])==set(replay_stage['review_ids']), 'completed source identities differ')
    audit_root=declared_root(selection,packet['audit_artifact_sha256'])
    for name,digest in packet['audit_artifact_sha256'].items():
        require(selection['input_inventory'][str(audit_root)][name]['sha256']==digest, 'audit declaration digest differs')
        original_file(selection,audit_root,name,raw)
    require(packet['audit_artifact_sha256']['review-corrections.jsonl']==source_stage['snapshot_digests']['review-corrections.jsonl'][7:],
        'audit corrections differ from sealed source')
    audit=json.loads(original_file(selection,audit_root,'native-accomplishment-audit.private.json',raw))
    metadata=json.loads(original_file(selection,audit_root,'audit-metadata.json',raw))
    require(audit['schema_version']=='sep29-native-accomplishment-duplicate-audit/v1'
        and metadata['schema_version']=='sep29-editorial-accomplishment-audit/v1', 'original audit schemas differ')
    decisions={d['activity_id']:d for d in audit['records']}; editorial={d['activity_id']:d for d in metadata['editorial_audit']}
    require(len(decisions)==len(audit['records'])==len(proposals) and set(decisions)==set(editorial)==set(by_activity), 'own-source audit partition differs')
    original_root=declared_root(selection,{'proposals.json'},exclude=(source,replay))
    originals=json.loads(original_file(selection,original_root,'proposals.json',raw))
    original_keys={source_key(p):p for p in originals}; current_keys={source_key(p):p for p in proposals}
    require(len(original_keys)==len(current_keys)==len(proposals) and set(original_keys)==set(current_keys), 'original source ownership/timing/allocation differs')
    preservation={p['activity_id']:p for p in selection['repaired_original_keyed_preservation']}
    allowed={'description','rendered_description','client_project','clockify_project_suffix','tag_names','tag_suffixes',
        'routing_disposition','review_warnings','billable'}
    for target,p in current_keys.items():
        old=original_keys[target]; changes=sorted(k for k in set(old)|set(p) if old.get(k)!=p.get(k))
        declaration=preservation[p['activity_id']]
        require(set(changes)<=allowed and declaration['changed_editorial_fields']==changes
            and all(old.get(k)==p.get(k) for k in ('allocation_mode','allocation_segment','effort','candidate_key','workstream_id','review_activity_key'))
            and declaration['evidence_fingerprint']==decisions[p['activity_id']]['evidence_fingerprint']==editorial[p['activity_id']]['evidence_fingerprint']==target[5]
            and decisions[p['activity_id']]['source_minutes']==editorial[p['activity_id']]['preserved_minutes']==p['duration_minutes'],
            'original own-source result/editorial transition differs')
    manifest=Path(proofs['native_checkpoint_manifest']['path']); page=Path(proofs['native_checkpoint_page']['path'])
    require(page==manifest.parent/'pages/000001.json' and packet['fresh_clockify_sha256']==proofs['native_evidence']['sha256'][7:]
        and packet['native_page_sha256']==proofs['native_checkpoint_page']['sha256'][7:], 'original fresh native handles differ')
    period=json.loads((source/'period-manifest.json').read_bytes())['period']
    since,until=selected._time(period['since_utc']),selected._time(period['until_utc'])
    identity,request=native._request(str(config['workspace_id']),str(config['member_id']),since,until)
    entries,_,_=native._validate(manifest.parent,{'manifest.json':raw(proofs['native_checkpoint_manifest']),
        'pages/000001.json':raw(proofs['native_checkpoint_page'])},raw(proofs['native_evidence']),identity=identity,request=request,since=since,until=until)
    events=[e.document() for e in evidence_ledger.normalize_collector_snapshot({'clockify':documents['native_evidence']}) if e.source_type=='clockify']
    by_index={int(e['source_ref']['source_id'].removeprefix('row-'))-1:e for e in events}
    bindings=packet['fresh_evidence_bindings']; prior={b['native_original_index']:b for b in audit['native_original_index_binding']}
    require(len(bindings)==len(entries)==len(by_index)==len(prior) and set(by_index)==set(prior)==set(range(len(entries))), 'native original-index partition differs')
    for i,(binding,entry) in enumerate(zip(bindings,entries,strict=True)):
        require(binding=={'native_original_index':i,'native_entry_id':entry['id'],'native_entry':entry,'evidence_event':by_index[i]}
            and prior[i]['native_entry_id']==entry['id'] and prior[i]['normalized_evidence_id']==by_index[i]['evidence_id'], 'native original source binding differs')
    selected_ids,appended,retained=publication_rows(packet,receipt,documents['live_readback'],str(config['spreadsheet_id']),title)
    held=[]
    for hold in packet['held']:
        p=by_activity[hold['proposal_identity']]; decision=decisions[p['activity_id']]; entry=entries[decision['native_original_index']]
        require(hold['native_decision']==decision and decision['recommendation']=='hold_represented'
            and decision['automatic_credit_created'] is False and hold['minutes']==p['duration_minutes']
            and decision['native_entry_id']==entry['id'] and decision['native_description_sha256']==hashlib.sha256(entry['description'].encode()).hexdigest()
            and selected._time(entry['timeInterval']['start'])==selected._time(p['start'])
            and selected._time(entry['timeInterval']['end'])==selected._time(p['end'])
            and decision['native_fully_covers_source_interval'] is True and decision['effective_routing_matches'] is True
            and entry['projectId'].endswith(p['clockify_project_suffix']) and sorted(t[-8:] for t in entry['tagIds'])==sorted(p['tag_suffixes']),
            'held own-source/native result differs')
        held.append(publisher.stable_review_id(p))
    projected=monthly.project_rows(source); gap=packet['mixed_meeting_excluded']; excluded=by_activity[gap['activity_id']]
    gap_rows=[row for row in projected if row[3]=='routing_gap']
    require(excluded.get('routing_disposition')=='unresolved-routing' and not excluded.get('client_project')
        and gap['full_minutes']==excluded['duration_minutes'] and len(gap_rows)==1 and gap_rows[0][0]==gap['canonical_diagnostic_id']
        and json.loads(gap_rows[0][11])['evidence_ids']==sorted(excluded['provenance']['evidence_ids']), 'whole routing-gap diagnostic representation differs')
    routing_ids=[publisher.stable_review_id(excluded)]
    require(len(set(selected_ids+held+routing_ids))==len(selected_ids)+len(held)+1
        and set(selected_ids+held+routing_ids)==set(by_review), 'review/held/whole-diagnostic source partition is not disjoint and exhaustive')
    packet_proposals={publisher.stable_review_id(p):p for p in packet['row_proposals']}
    require(len(packet_proposals)==len(packet['row_proposals'])==len(selected_ids) and set(packet_proposals)==set(selected_ids), 'primary proposal bindings differ')
    for row in packet['rows']:
        p=by_review[row[0]]; displayed=packet_proposals[row[0]]
        require({k:v for k,v in p.items() if k!='review_warnings'}=={k:v for k,v in displayed.items() if k!='review_warnings'}, 'source-owned primary proposal differs')
        expected=publisher.proposal_row(p,source.name,project_allowlist=allowlist)
        require(all(row[i]==expected[i] for i in [*range(9),10,11]) and row[9]=='pending' and row[13]=='unposted', 'primary source/machine/disposition cells differ')
        # The explicitly pinned original packet contains the financial warnings;
        # each added warning must still be a real bounded native temporal overlap.
        added=displayed.get('review_warnings',[])[len(p.get('review_warnings',[])):]
        require(displayed.get('review_warnings',[])[:len(p.get('review_warnings',[]))]==p.get('review_warnings',[]), 'original financial warning facts changed')
        for warning in added:
            event=next((e for e in events if e['evidence_id']==warning['counterpart_id']),None)
            require(event is not None and warning['type']=='existing_clockify_overlap', 'financial warning counterpart is foreign')
            a=max(selected._time(p['start']),selected._time(event['raw_source_span']['start']))
            b=min(selected._time(p['end']),selected._time(event['raw_source_span']['end']))
            require(a<b and selected._time(warning['overlap_start'])==a and selected._time(warning['overlap_end'])==b
                and warning['overlap_duration_seconds']==int((b-a).total_seconds()), 'financial warning overlap differs')
            publisher._validate_review_warning(warning,allowlist)
    require(selection['review_rows']==len(selected_ids) and selection['review_minutes']==sum(by_review[i]['duration_minutes'] for i in selected_ids)
        and selection['held_rows']==len(held) and selection['held_minutes']==sum(by_review[i]['duration_minutes'] for i in held)
        and selection['routing_gap_rows']==len(routing_ids) and selection['canonical_diagnostic_rows']==len(projected), 'original selection totals differ')
    if 'unresolved_packet' in proofs:
        require(selection['artifact_sha256']['unresolved-publication.private.json']==proofs['unresolved_packet']['sha256'][7:], 'original diagnostic packet pin differs')
    return {'selected_review_ids':selected_ids,'appended_review_ids':appended,'retained_pending_review_ids':retained,
        'held_review_ids':held,'routing_diagnostic_review_ids':routing_ids,'selected_minutes':selection['review_minutes'],
        'held_review_disposition_basis':'Authenticated original audited own-source disposition; not new automated duplicate financial clearance.',
        'original_financial_warning_sha256':{row[0]:pending.digest(row[12]) for row in packet['rows']},
        'appended_minutes':receipt['minutes'],'retained_pending_minutes':receipt['existing_minutes'],'held_minutes':selection['held_minutes'],
        'source_review_availability':{'status':'complete','review_ids':list(by_review),'review_availability_only':True},
        'proofs':dict(proofs),'diagnostics':diagnostics(config,source,packet,proofs,documents,title),
        'financial_coverage_claimed':False,'whole_source_canonical_exact_delivery_claimed':False,
        'adoption_provider_writes':0,'posted_credits_created':0,'additional_credited_seconds':0,
        'consumer_runtime':pending.artifact_handle(Path(__file__).resolve())}
