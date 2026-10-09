"""Mixed native review availability; never posted credit or resolution.

Original literal rows, historical own accomplishments and an explicitly
projected pending subscope are separate representations. Only saved handles
are consumed. This module performs no discovery, inference, writes or replay.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from scripts import clockify_pending_review_selection as pending
from scripts import clockify_pending_review_append as append
from scripts import clockify_financial_semantic_lineage as lineage
from scripts import clockify_source_adoptions as artifacts
from scripts import clockify_source_event_correspondence as correspondence
from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_sheet_publish as publisher

SCHEMA='mixed-review-availability/v1'
LITERAL='literal-existing-pending'
OWN='positive-own-original-accomplishment-variant'
SUBSCOPE='published-bounded-pending-subscope-warning-only'
UNKNOWN_J='Current authentic native cells preserved; separate original J-update operation/decision receipt not located in bounded source/publication/notes scope. Not fabricated canonical projection or acceptance.'


def require(condition,message):
    if not condition:raise ValueError('mixed review availability: '+message)


def verify_scope(document):
    require(set(document)=={'schema_version','preparation','comparison','review_availability_only',
        'financial_coverage_claimed','diagnostic_resolution_claimed','additional_credited_seconds'}
        and document['schema_version']==SCHEMA and document['review_availability_only'] is True
        and document['financial_coverage_claimed'] is False and document['diagnostic_resolution_claimed'] is False
        and type(document['additional_credited_seconds']) is int and document['additional_credited_seconds']==0,
        'availability-only contract or scope differs')


def native_rows(document,spreadsheet,title,sheet_id,width,*,header=None):
    from scripts import clockify_selected_delivery_adoption as selected
    document=document.get('readback',document)
    rows=selected._capture_rows(document,spreadsheet,title,sheet_id,width)
    if header is not None:require(rows.get(1)==header,'native header differs')
    structured=document.get('structuredContent',document)
    sheet=next(s for s in structured['sheets'] if s['properties']['sheetId']==sheet_id)
    raw={}
    for block in sheet['data']:
        for number,row in enumerate(block['rowData'],block.get('startRow',0)+1):
            require(len(row.get('values',[]))<=width,'native row is too wide')
            raw[number]=row
    return selected._by_identity({n:r for n,r in rows.items() if n!=1}),raw


def event_receipt(event):
    return {'evidence_id':event['evidence_id'],'event_sha256':pending.digest(event),
        'source_type':event['source_type'],'source_ref':event['source_ref'],
        'role':event['attributes'].get('role'),'kind':event['attributes'].get('kind'),
        'content_sha256':hashlib.sha256(event['attributes'].get('content','').encode()).hexdigest()}


def verify_anchors(declared,selected_events,original_events,*,same_recording=False):
    """Authenticate an explicit bounded result witness, never classify by atoms."""
    current={e['evidence_id']:e for e in selected_events};old={e['evidence_id']:e for e in original_events}
    if same_recording:
        require(not declared and len(selected_events)==len(original_events)==1
            and selected_events==original_events and selected_events[0]['source_type'] in {'fathom','calendly'},
            'original recording outcome witness differs')
        return
    require(isinstance(declared,list) and declared,'own assistant outcome anchor is missing')
    seen=set()
    for pair in declared:
        a,b=pair['new'],pair['original_posted_own']
        require(a['evidence_id'] in current and b['evidence_id'] in old
            and a==event_receipt(current[a['evidence_id']]) and b==event_receipt(old[b['evidence_id']])
            and a['role']==b['role']=='assistant' and a['content_sha256']==b['content_sha256']
            and (current[a['evidence_id']]==old[b['evidence_id']] or correspondence.corresponds(
                current[a['evidence_id']],old[b['evidence_id']],historical_timezone='Europe/Bucharest'))
            and a['evidence_id'] not in seen,'own assistant outcome anchor differs or repeats')
        seen.add(a['evidence_id'])


def recorded_append(actual,expected,raw):
    """Recorded bytes stay recorded; current runtime is a separate observation."""
    require(actual['schema_version']=='pending-review-append-acceptance/v1'
        and actual['acceptance_sha256']==pending.digest({k:v for k,v in actual.items() if k!='acceptance_sha256'})
        and set(actual['runtime'])=={'consumer','comparison_only_source_correspondence',
            'own_financial_semantic_lineage','native_credit_consumer','publisher'},'pending acceptance integrity differs')
    for handle in actual['runtime'].values():raw(handle)
    require({k:v for k,v in actual.items() if k not in {'runtime','acceptance_sha256'}}
        =={k:v for k,v in expected.items() if k not in {'runtime','acceptance_sha256'}},
        'pending acceptance semantic proof differs')


def verify(config,source_stage,replay_stage,proofs,*,title):
    cache={}
    raw=lambda handle:artifacts._capture(handle,cache)
    load=lambda handle:json.loads(raw(handle))
    wrapper=load(proofs['selection']);verify_scope(wrapper)
    prep,comparison=load(wrapper['preparation']),load(wrapper['comparison'])
    require(prep.get('schema_version')=='sep30-exhaustive-representation-preparation/v1',
        'explicit preparation data schema differs')
    source_record,replay_record=prep['source'],prep['replay']
    require(source_record==comparison['source'] and replay_record==comparison['replay'],
        'source/replay declarations differ')
    source=pending._source(source_record,cache);replay=pending._source(replay_record,cache)
    root=Path(source_record['artifacts']['proposals']['path']).parent
    for record,stage,seal in ((source_record,source_stage,prep['source_seal']),(replay_record,replay_stage,prep['replay_seal'])):
        require(Path(stage['run_dir'])==Path(record['artifacts']['proposals']['path']).parent
            and stage['proposals_digest']==record['artifacts']['proposals']['sha256']
            and stage['bundle_digest']==seal['bundle_digest'] and load(record['artifacts']['receipt'])==seal,
            'completed source stage or seal differs')
    require(source['proposals']==source['accounting']['proposals']
        and all(source[k]==replay[k] for k in ('proposals','accounting','ledger','routing','replay')),
        'complete original source differs from replay')
    publisher.verify_gates(source['proposals'],source['quality'],replay['replay'],source['run_id'])
    allowlist=publisher.project_allowlist(source['routing'])
    require(publisher._routing_allowlist(Path(source['artifacts']['routing']['path']),replay['replay'])==allowlist,
        'source routing differs from sealed replay')
    proposals=source['by_review_id'];records=prep['review_outputs'];saved=comparison['outputs']
    ids=[r['review_id'] for r in records];saved_ids=[r['review_id'] for r in saved]
    require(len(set(ids))==len(ids)==len(proposals) and set(ids)==set(proposals)==set(source_stage['review_ids'])
        ==set(replay_stage['review_ids']) and len(set(saved_ids))==len(saved_ids)==len(proposals)
        and set(saved_ids)==set(proposals),'primary partition is not disjoint and exhaustive')
    by_saved={r['review_id']:r for r in saved}
    classes={r['review_id']:r['representation_class'] for r in records}
    require(set(classes.values())<={LITERAL,OWN,SUBSCOPE},'unknown primary representation class')
    spreadsheet=str(config['spreadsheet_id'])
    packet=load(prep['original_review_packet']);receipt=load(prep['original_review_receipt'])
    require(packet['schema_version']=='verified-review-publication-supplement/v2'
        and packet['source_run']==str(root) and packet['replay_run']==replay_stage['run_dir']
        and packet['source_proposals_sha256']==source_stage['proposals_digest'].removeprefix('sha256:')
        and packet['duration_reduced_for_overlap'] is False
        and receipt['packet_sha256']==prep['original_review_packet']['sha256'].removeprefix('sha256:')
        and receipt['spreadsheet_id']==spreadsheet,'original primary publication authority differs')
    sid=receipt['sheet_id']
    original,original_raw=native_rows(receipt['readback'],spreadsheet,title,sid,15)
    full,full_raw=native_rows(load(prep['current_primary_capture']),spreadsheet,title,sid,15,header=publisher.HEADER)
    current,current_raw=native_rows(load(proofs['live_readback']),spreadsheet,title,sid,15,header=publisher.HEADER)
    bound,bound_raw=native_rows(load(prep['fresh_targeted_native_capture']),spreadsheet,title,sid,15,header=publisher.HEADER)
    require(all(current_raw.get(n)==r for n,r in bound_raw.items()),'current native primary capture differs or is stale')
    literal={row[0]:row for row in packet['rows']}
    require(len(literal)==len(packet['rows'])==receipt['rows']
        and set(literal)=={rid for rid,kind in classes.items() if kind==LITERAL},'literal primary partition differs')
    match=re.fullmatch(r'A([1-9][0-9]*):O([1-9][0-9]*)',receipt['range'])
    require(match and len(literal)==int(match[2])-int(match[1])+1,'literal primary publication range differs')
    actual_doc=load(prep['actual_clockify']['capture'])
    entries,_=append._actual(actual_doc)
    require((actual_doc['workspace_id'],actual_doc['user_id'])==(config['workspace_id'],config['member_id']),
        'actual financial observation is foreign')
    actual={e['id']:e for e in entries}
    financial_warnings=[];represented=[];published=[]
    source_events={e['evidence_id']:e for e in source['ledger']['events']}
    for record in records:
        rid=record['review_id'];p=proposals[rid];saved_item=by_saved[rid]
        require(saved_item.get('financial_novelty_claimed') is False
            and saved_item.get('additional_credited_seconds') in (None,0)
            and record['automatic_credit_or_suppression_authorized'] is False,'credit claim is not review availability')
        es=artifacts._source_events(p,source['ledger'])
        require(record['proposal_id']==saved_item['proposal_id']==p['id']
            and record['proposal_sha256']==saved_item['native_proposal_sha256']==pending.digest(p)
            and record['source_seconds']==saved_item['native_seconds']==p['duration_seconds']
            and record['source_owned_evidence_ids']==[e['evidence_id'] for e in es]
            and saved_item['source_owned_events']==[event_receipt(e) for e in es],
            'current proposal or source objects differ')
        activity=lineage._activity({'semantic_ref':record['source_semantic_ref']},p,
            source['artifacts']['proposals']['path'],cache)
        require(record['source_semantic_ref']==saved_item['semantic_ref'],'current semantic reference differs')
        kind=record['representation_class']
        if kind==LITERAL:
            row=literal[rid];number,now=current[rid]
            expected=publisher.proposal_row(p,source['run_id'],project_allowlist=allowlist)
            require(row==original[rid][1]==now==full[rid][1]==record['current_representation']['all15values']
                and number==original[rid][0]==record['current_representation']['row_number']
                and [row[i] for i in [*range(9),10,11]]==[expected[i] for i in [*range(9),10,11]]
                and row[9]=='pending' and row[13]=='unposted','literal native pending/source row differs')
            published.append(rid)
            continue
        witness=record['positive_original_result_witness']
        require(witness['source_and_original_semantic_refs']['current']==record['source_semantic_ref']
            and witness['source_and_original_semantic_refs']['original']==saved_item['original_own_semantic_ref'],
            'own historical semantic witness differs')
        old=artifacts.verify_prior_native_proof(witness['original_posting_artifacts'],saved_item['original_review_id'],
            saved_item['original_clockify_entry_id'],workspace_id=config['workspace_id'],member_id=config['member_id'],capture_cache=cache)
        require(witness['original_posting_artifacts']==saved_item['original_posting_artifacts']
            and pending.digest(old['prior_proposal'])==saved_item['original_native_proposal_sha256']
            ==witness['saved_original_proposal_sha256'],'original own POST provenance differs')
        declaration={'basis':'native-posted','review_id':old['prior_review_id'],
            'artifacts':witness['original_posting_artifacts'],'semantic_ref':saved_item['original_own_semantic_ref']}
        own=lineage.authenticate(declaration,surface='posted',identifier=old['clockify_entry_id'],
            record={'events':old['source_events']},captured={},actual=actual,cache=cache)
        anchors=witness['own_assistant_outcome_anchor_pairs']
        require(anchors==saved_item['own_assistant_outcome_anchor_pairs'],'own result anchor declarations differ')
        verify_anchors(anchors,es,old['source_events'],same_recording=saved_item['current_same_recording_object_exact'] is True)
        require(saved_item['disposition'].startswith(('same-','distinct-bounded-subscope-'))
            and witness['saved_inspected_original_completion_facts']==saved_item['inspected_original_completion_facts']
            and isinstance(saved_item['inspected_original_completion_facts'],str)
            and saved_item['inspected_original_completion_facts'].strip()
            and witness['source_atoms_or_words_alone_are_not_outcome_proof'] is True
            and saved_item['same_source_or_wording_used_alone_to_prove_same_work'] is False,
            'explicit bounded own outcome adjudication differs')
        live=actual[old['clockify_entry_id']]
        changed=[k for k in ('description','projectId','billable','tagIds','taskId') if old['payload'].get(k)!=live.get(k)]
        route=all(p[k]==old['prior_proposal'][k] for k in ('clockify_project_suffix','billable','tag_suffixes'))
        require(changed==saved_item['current_vs_original_POST_payload_changed_fields']
            ==witness['saved_current_original_POST_changed_fields']
            and route is saved_item['native_route_billable_tags_equal']
            and route is witness['saved_native_route_billable_tags_equal']
            and own['verified_current'] is saved_item['own_current_financial_verified']
            and own['gaps']==saved_item['own_current_financial_gaps'],'financial warnings or live payload drift were laundered')
        if changed or not route or own['gaps']:
            financial_warnings.append({'proposal_id':p['id'],'review_id':rid,'changed_fields':changed,
                'native_route_billable_tags_equal':route,'own_current_financial_gaps':own['gaps'],
                'warning_only':True,'financial_credit_equivalence_claimed':False})
        old_repr=witness['current_original_human_row'];old_number,old_cells=full[old['prior_review_id']]
        require(old_repr['all15values']==old_cells and old_repr['row_number']==old_number
            and pending.digest(full_raw[old_number])==old_repr['full_native_row_sha256'],
            'preserved original posted human representation differs')
        if kind==OWN:
            require(rid not in current and rid not in full,'historical own variant was relabelled as literal')
            represented.append(rid)
        else:
            verify_subscope(record,saved_item,p,activity,current,current_raw,full,root,source,spreadsheet,title,sid,raw,load)
            published.append(rid)
    diagnostic=verify_diagnostics(prep,root,source,spreadsheet,title,proofs,raw,load)
    return {'source_review_availability':{'status':'complete','review_ids':ids,'review_availability_only':True},
        'literal_current_review_ids':published,'represented_own_accomplishment_ids':represented,
        'financial_warnings':financial_warnings,'financial_coverage_claimed':False,
        'whole_source_canonical_exact_delivery_claimed':False,'additional_credited_seconds':0,
        'posted_credits_created':0,'adoption_provider_writes':0,'proofs':dict(proofs),'diagnostics':diagnostic,
        'current_capture_basis':'byte-bound native targeted observations; no unstated wall-clock freshness',
        'consumer_runtime':pending.artifact_handle(Path(__file__).resolve())}


def verify_subscope(record,saved,p,activity,current,current_raw,full,root,source,spreadsheet,title,sid,raw,load):
    rid=record['review_id'];result=load(record['native_append_result'])
    verified=append.verify(bindings_path=Path(record['native_append_binding']['path']))
    require(verified['rows']==result['rows'] and len(result['rows'])==1,'pending subscope native rows differ')
    recorded_append(result['receipt'],verified['receipt'],raw)
    projection,pub=load(record['editorial_projection']),load(record['publication_readback'])
    require(projection['schema_version']=='client-facing-review-editorial-projection/v1'
        and projection['accepted_native_result_sha256']==pub['accepted_native_result_sha256']
        ==record['native_append_result']['sha256'].removeprefix('sha256:')
        and pub['editorial_projection_sha256']==record['editorial_projection']['sha256'].removeprefix('sha256:')
        and projection['native_row']==pub['original_native_row']==result['rows'][0]
        and projection['review_row']==pub['published_review_row']==current[rid][1]==full[rid][1]
        ==record['current_representation']['all15values']
        and projection['review_id']==rid and (pub['spreadsheet_id'],pub['sheet_title'],pub['sheet_id'])==(spreadsheet,title,sid)
        and projection['changed_columns']==record['editorial_changed_columns']==['I','O']
        and [i for i in range(15) if projection['native_row'][i]!=projection['review_row'][i]]==[8,14]
        and projection['clockify_approval_claimed'] is False and projection['additional_credit_claimed'] is False
        and pub['clockify_writes']==0 and pub['approval_claimed'] is False
        and pub['additional_financial_credit_claimed'] is False,'pending subscope I/O publication provenance differs')
    require(record['additional_human_minutes_established'] is False and current[rid][1][9]=='pending'
        and current[rid][1][13]=='unposted' and current[rid][1][3]*60==p['duration_seconds'],
        'pending subscope has unsupported minutes or disposition')
    captured,captured_raw=native_rows(pub,spreadsheet,title,sid,15,header=publisher.HEADER)
    require(captured[rid]==current[rid] and pub['range']==f'A{current[rid][0]}:O{current[rid][0]}',
        'pending subscope publication range differs')


def verify_diagnostics(prep,root,source,spreadsheet,title,proofs,raw,load):
    rows=monthly.project_rows(root);canonical={row[0]:row for row in rows}
    packet,receipt=load(prep['original_diagnostic_packet']),load(prep['original_diagnostic_receipt'])
    require(packet['rows']==rows and packet['source_run']==str(root)
        and receipt['packet_sha256']==prep['original_diagnostic_packet']['sha256'].removeprefix('sha256:')
        and receipt['spreadsheet_id']==spreadsheet,'diagnostic source publication differs')
    sid=receipt['sheet_id'];diagnostic_title=monthly.title_for_review(title)
    current,native=native_rows(load(proofs['live_readback']),spreadsheet,diagnostic_title,sid,12,header=monthly.HEADER)
    bound,bound_native=native_rows(load(prep['fresh_targeted_native_capture']),spreadsheet,diagnostic_title,sid,12,header=monthly.HEADER)
    require(all(native.get(n)==r for n,r in bound_native.items()),'current native diagnostic capture differs or is stale')
    original,_=native_rows(receipt['readback'],spreadsheet,diagnostic_title,sid,12)
    records=prep['diagnostic_source_correspondence'];ids=[r['stable_evidence_id'] for r in records]
    require(len(set(ids))==len(ids)==len(canonical) and set(ids)==set(canonical),'diagnostic partition differs')
    events={e['evidence_id']:e for e in source['ledger']['events']}
    exceptions=load({'path':str(root/'ambiguous.json'),'sha256':'sha256:'+prep['input_pins'][str(root/'ambiguous.json')]})
    pub=load(prep['current_primary_capture']);primary_sid=pub['sheet_id']
    linked,linked_raw=native_rows(load(prep['fresh_linked_native_capture']),spreadsheet,title,primary_sid,15)
    unknown=[]
    for record in records:
        rid=record['stable_evidence_id'];row=canonical[rid];number,now=current[rid]
        evidence=json.loads(row[11])['evidence_ids']
        matches=[a for a in exceptions if a['exception_kind']==row[3] and sorted(a['evidence_ids'])==evidence]
        require(len(matches)==1 and record['source_exception_sha256']==pending.digest(matches[0])
            and record['source_kind']==row[3] and record['evidence_ids']==evidence
            and record['source_evidence_event_sha256']=={eid:pending.digest(events[eid]) for eid in evidence}
            and record['original_projected12cells']==original[rid][1]==row
            and record['current_row_number']==number and record['current_raw12values']==now
            and record['current_full_native_row_sha256']==pending.digest(native[number])
            and all(now[i]==row[i] for i in range(12) if i!=9)
            and now[10]==record['current_disposition']=='needs_review'
            and record['diagnostic_resolved'] is False and record['owned_human_interval_proven'] is False,
            'diagnostic source, exact native cells or disposition differ')
        declarations=record['current_human_J_pending_links']
        links=re.findall(r'rândul\s+(\d+)\s+\(([^)]+)\)',now[9])
        require(len(links)==len(declarations) and [(str(link['current_primary_representation']['row_number']),link['review_id'])
            for link in declarations]==links,'diagnostic current J link differs')
        if now[9]==row[9]:require(not declarations,'canonical diagnostic has invented links')
        else:
            require(declarations and record['J_only_editorial_lineage']==UNKNOWN_J,'diagnostic editorial lineage was invented')
            unknown.append({'review_id':rid,'status':'unknown_original_J_operation_lineage',
                'warning':'Current source core and linked native pending cells are verified for review availability only; original J-edit operation/decision receipt is unknown.',
                'canonical_exact_delivery_claimed':False,'diagnostic_resolution_claimed':False})
        for link in declarations:
            lid=link['review_id'];linked_number,linked_cells=linked[lid]
            observed=link['fresh_linked_full_native_capture']
            require(link['current_primary_representation']['all15values']==linked_cells
                and linked_number==link['current_primary_representation']['row_number']==observed['row_number']
                and linked_cells[9]=='pending' and linked_cells[13]=='unposted'
                and pending.digest(linked_raw[linked_number])==observed['full_native_row_sha256']
                and link['reviewed_availability_only_not_entire_evidence_coverage'] is True
                and link['source_outcome_equivalence_or_new_minutes_not_established_by_link'] is True,
                'diagnostic linked current native pending cells differ')
    return {'status':'complete','review_ids':ids,'review_availability_only':True,'historical_alias_ids':[],
        'diagnostic_resolution_claimed':False,'financial_coverage_claimed':False,'additional_minutes':0,
        'unknown_editorial_lineage':unknown,'current_canonical_exact_row_count':len(ids)-len(unknown)}
