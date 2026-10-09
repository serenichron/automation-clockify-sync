"""Authentic selected pending publication plus retained REVIEW AVAILABILITY.

Original receipt types and source/runtime bytes remain immutable. No prospective
packet is synthesized, no duplicate financial credit or live adoption is granted.
"""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path
from scripts import clockify_pending_review_selection as pending
from scripts import clockify_source_adoptions as artifacts
from scripts import clockify_selected_delivery_adoption as selected
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_mixed_review_availability as mixed
from scripts import clockify_pending_diagnostic_availability as diagnostic

SCHEMA='native-pending-review-availability/v1'
REPEATED_WARNING_PRODUCER='sha256:4562fb9c432c7ebd2509471f7fc45cf5e8f8aff615a96195bfe3984432c93039'

def require(condition,message):
    if not condition:raise ValueError('authentic pending review availability: '+message)

def native_rows(document,spreadsheet,title,sid,width,*,header):
    capture=document.get('readback',document)
    rows=selected._capture_rows(capture,spreadsheet,title,sid,width)
    _,raw=mixed.native_rows(capture,spreadsheet,title,sid,width,header=header)
    return rows,raw

def editable_native_row(row):
    """Exclude only derived display fields absent from the current API mask."""
    return {'values':[{k:v for k,v in cell.items() if k not in {'effectiveValue','formattedValue','effectiveFormat'}}
        for cell in row.get('values',[])]}

def alias_relation(alias,proposal,prior_proposal,activity,prior_activity,events,prior_events):
    """Own semantic objects plus a declared exact completed-result witness."""
    require(alias['review_availability_only'] is True and alias['financial_equivalence_claimed'] is False
        and type(alias['additional_credited_seconds']) is int and alias['additional_credited_seconds']==0,
        'alias claims financial credit')
    require(alias['source_activity_id']==proposal['activity_id']==activity['activity_id']
        and alias['represented_activity_id']==prior_proposal['activity_id']==prior_activity['activity_id'],
        'own semantic activity differs')
    for declared,actual,p in ((alias['source_semantic_core'],activity,proposal),
                             (alias['represented_semantic_core'],prior_activity,prior_proposal)):
        require({'action','object','outcome','evidence_ids'}<=set(declared)
            and all(actual.get(k)==v for k,v in declared.items())
            and all(isinstance(actual.get(k),str) and actual[k].strip() for k in ('action','object','outcome'))
            and set(actual['evidence_ids'])==set(p['provenance']['evidence_ids']),
            'own semantic result or evidence membership differs')
    require(sorted(pending.digest(e) for e in events)==sorted(pending.digest(e) for e in prior_events)
        and len(events)==len(proposal['provenance']['evidence_ids'])
        and len(prior_events)==len(prior_proposal['provenance']['evidence_ids']), 'full sealed source objects differ')
    mixed.verify_anchors(alias['outcome_anchors'],events,prior_events)
    require(type(alias['source_seconds']) is int and type(alias['represented_seconds']) is int
        and alias['source_seconds']==proposal['duration_seconds']
        and alias['represented_seconds']==prior_proposal['duration_seconds'], 'native alias seconds differ')
    return {'source_seconds':alias['source_seconds'],'represented_seconds':alias['represented_seconds'],
        'financial_equivalence_claimed':False,'additional_credited_seconds':0,'review_availability_only':True,
        'warning':'Authentic pending result representation only; retained allocation and source allocation remain distinct, never payable equivalence or additional time.'}

def completed_pair(declared,cache):
    require(set(declared)=={'source','replay'},'source pair inventory differs')
    a,b=(pending._source(declared[k],cache) for k in ('source','replay'))
    require(a['basis']=='completed-review-run' and b['basis']=='completed-review-replay'
        and b['replay']['source_run_id']==a['run_id']
        and all(a[k]==b[k] for k in ('proposals','accounting','ledger','routing','replay')),
        'full completed source differs from exact replay')
    publisher.verify_gates(a['proposals'],a['quality'],b['replay'],a['run_id'])
    require(publisher._routing_allowlist(Path(a['artifacts']['routing']['path']),b['replay'])
        ==publisher.project_allowlist(a['routing']),'source routing differs from replay')
    return a,b

def stage_matches(stage,source,load):
    seal=load(source['artifacts']['receipt'])
    require(Path(stage['run_dir'])==Path(source['artifacts']['receipt']['path']).parent
        and stage['proposals_digest']==source['artifacts']['proposals']['sha256']
        and stage['bundle_digest']==seal['bundle_digest']
        and set(stage['review_ids'])==set(source['by_review_id']), 'requested completed stage differs')

def own_activity(handle,proposal,source,load):
    root=Path(source['artifacts']['receipt']['path']).parent
    require(Path(handle['path'])==root/'semantic-analysis.json','own semantic original locator differs')
    matches=[a for a in load(handle)['activities'] if a['activity_id']==proposal['activity_id']]
    require(len(matches)==1,'own semantic activity is not unique')
    return matches[0]

def warning_repetition_projection(actual,current,rows):
    """Prove exact old projection while keeping only identical repetition drift."""
    old,new=actual['native_review_warnings'],current['native_review_warnings']
    require(set(old)==set(new),'historical warning identity inventory differs')
    for rid,warnings in old.items():
        require(isinstance(warnings,list) and all(isinstance(w,dict) for w in warnings), 'historical warning facts are invalid')
        unique=[]
        for warning in warnings:
            if warning not in unique:unique.append(warning)
        require(unique==new[rid], 'historical unique warning facts or order differ')
    for receipt in (actual,current):
        projected=copy.deepcopy(rows)
        for row in projected:
            if row[0] in receipt['native_review_warnings']:
                warnings=receipt['native_review_warnings'][row[0]]
                row[12]=json.dumps(warnings,ensure_ascii=False,sort_keys=True) if warnings else ''
        require(pending.digest(projected)==receipt['native_projection_rows_sha256'],
            'historical/current exact native warning projection digest differs')
    return {'original_warnings':old,'current_warnings':new,
        'original_projection_sha256':actual['native_projection_rows_sha256'],
        'current_projection_sha256':current['native_projection_rows_sha256'],
        'boundary':'Only identical repetitions removed in current native warning projection; source, intervals, allocations, routing and every other historical proof field are unchanged.'}

def historical_acceptance(actual,verified,raw):
    """Fixed original producer's duplicated warnings, not a generic ignored field."""
    current=verified['receipt'];roles=actual.get('runtime_artifacts',{})
    require(set(roles)=={'consumer','pipeline','allocator'}
        and roles['consumer']['sha256']==REPEATED_WARNING_PRODUCER
        and all(roles[k]['sha256']==current['runtime_artifacts'][k]['sha256'] for k in ('pipeline','allocator')),
        'historical repeated-warning producer runtime differs')
    for h in roles.values():raw(h)
    drift=warning_repetition_projection(actual,current,verified['rows'])
    reconstructed=copy.deepcopy(current)
    for k in ('native_review_warnings','native_projection_rows_sha256'):reconstructed[k]=actual[k]
    checks=selected._pending_acceptance(actual,reconstructed,raw)
    return checks,drift

def verify(config,source_stage,replay_stage,proofs,*,title):
    cache={};raw=lambda h:artifacts._capture(h,cache);load=lambda h:json.loads(raw(h))
    doc=load(proofs['selection'])
    require(set(doc)=={'schema_version','review_availability_only','financial_coverage_claimed','additional_credited_seconds',
        'source_pairs','selection','acceptance','publication_plan','publication_receipt','primary_capture','actual_capture','aliases','diagnostics'}
        and doc['schema_version']==SCHEMA and doc['review_availability_only'] is True
        and doc['financial_coverage_claimed'] is False and type(doc['additional_credited_seconds']) is int
        and doc['additional_credited_seconds']==0 and len(doc['source_pairs'])==2,
        'scope, source inventory or credit claims differ')
    pairs=[completed_pair(p,cache) for p in doc['source_pairs']]
    original,original_replay=pairs[0];current,current_replay=pairs[-1]
    stage_matches(source_stage,original,load);stage_matches(replay_stage,original_replay,load)
    require(original['ledger']==current['ledger'] and original['routing']==current['routing']
        and set(original['by_review_id'])==set(current['by_review_id']), 'source transition changes sealed identity')
    fixed={'activity_id','candidate_key','review_activity_key','allocation_segment','allocation_mode','start','end',
        'duration_seconds','duration_minutes','provenance','source','billable'}
    for rid,p in original['by_review_id'].items():
        require(all(p.get(k)==current['by_review_id'][rid].get(k) for k in fixed),
            'corrected publication changes native source allocation or ownership')
    spreadsheet=str(config['spreadsheet_id']);selection=load(doc['selection'])
    require(selection['sources'][selection['current_source']]==doc['source_pairs'][-1]['source']
        and (selection['spreadsheet_id'],selection['sheet_title'])==(spreadsheet,title),
        'actual selection source or destination differs')
    root=Path(current['artifacts']['receipt']['path']).parent
    verified=pending.verify(bindings_path=Path(doc['selection']['path']),source_dir=root,proposals=current['proposals'],
        spreadsheet_id=spreadsheet,sheet_title=title,run_id=root.name,project_allowlist=publisher.project_allowlist(current['routing']))
    require(all(p['disposition']=='retain' for p in verified['prior']), 'selection has non-retained predecessors')
    accepted=load(doc['acceptance']);checks,warning_drift=historical_acceptance(accepted,verified,raw)
    expected={r[0]:r for r in verified['rows']}
    require(len(expected)==len(verified['rows']),'native selection duplicates accepted identity')
    new_ids=verified['new_ids'];retained={p['review_id']:p for p in verified['prior']}
    require(not set(new_ids)&set(retained) and set(new_ids)|set(retained)==set(expected),
        'native new/retained partition is not disjoint and exhaustive')
    plan,receipt=load(doc['publication_plan']),load(doc['publication_receipt'])
    new_rows=[expected[i] for i in new_ids];sid=plan['sheet_id']
    require(plan['schema_version']=='offline-native-oct21-append-plan/v1'
        and (plan['spreadsheet_id'],plan['sheet_title'])==(spreadsheet,title)
        and plan['rows']==verified['rows'] and plan['appends']==new_rows and plan['new_ids']==sorted(new_ids)
        and plan['updates']==[] and plan['selection_acceptance']==doc['acceptance']
        and receipt['schema_version']=='clockify-native-pending-append-readback/v1'
        and (receipt['spreadsheet_id'],receipt['sheet_title'],receipt['sheet_id'])==(spreadsheet,title,sid)
        and receipt['source_plan']['path']==doc['publication_plan']['path']
        and receipt['source_plan']['sha256'].removeprefix('sha256:')==doc['publication_plan']['sha256'][7:]
        and receipt['status']=='pending/unposted' and receipt['clockify_writes']==0
        and receipt['financial_novelty_claimed'] is False
        and receipt['appended_rows']==len(new_rows) and receipt['proposed_minutes']==sum(r[3] for r in new_rows),
        'actual native publication type, selection or credit receipt differs')
    publication_handle=lambda h:{'path':h['path'],'sha256':'sha256:'+h['sha256'].removeprefix('sha256:')}
    published,published_raw=native_rows(load(publication_handle(receipt['readback'])),spreadsheet,title,sid,15,header=publisher.HEADER)
    before,before_raw=native_rows(load(publication_handle(receipt['prewrite'])),spreadsheet,title,sid,15,header=publisher.HEADER)
    response=load(publication_handle(receipt['response']))
    require(response.get('isError') is not True and response.get('structuredContent',response).get('spreadsheetId')==spreadsheet,
        'original native append response differs')
    live,live_raw=native_rows(load(proofs['live_readback']),spreadsheet,title,sid,15,header=publisher.HEADER)
    bound,bound_raw=native_rows(load(doc['primary_capture']),spreadsheet,title,sid,15,header=publisher.HEADER)
    require(live_raw==bound_raw,'current native capture differs from declared observation')
    match=re.fullmatch(r'A([1-9][0-9]*):O([1-9][0-9]*)',receipt['range'])
    require(match is not None and len(new_rows)==int(match[2])-int(match[1])+1
        and plan['proposed_append_range']=="'"+title.replace("'","''")+"'!"+receipt['range']
        and [published.get(i) for i in range(int(match[1]),int(match[2])+1)]==new_rows,
        'actual native publication range or exact original rows differ')
    require(all(published_raw.get(n)==r for n,r in before_raw.items()),'original existing native cells were not preserved')
    observed=selected._by_identity(live)
    require(set(expected)<=set(observed) and all(observed[rid][1]==row for rid,row in expected.items()),
        'current selected or retained native15values differ')
    require(all(row[9]=='pending' and row[13]=='unposted' for row in expected.values()),
        'current selected/retained rows are not pending/unposted')
    for rid,p in retained.items():
        number,row=observed[rid]
        require(row==p['row'] and published.get(number)==row and before.get(number)==row
            and published_raw[number]==before_raw[number]
            and editable_native_row(live_raw[number])==editable_native_row(published_raw[number]),
            'retained editable native cells differ')
    aliases={};represented=set();alias_results=[]
    for declaration in doc['aliases']:
        rid,prior_id=declaration['source_review_id'],declaration['represented_review_id']
        require(rid not in aliases and prior_id not in represented and rid in original['by_review_id']
            and rid not in expected and prior_id in retained and prior_id in expected,
            'pending alias identity is duplicate, literal or unretained')
        prior=retained[prior_id]['source']
        require(declaration['represented_source']==selection['sources'][next(
            p['source'] for p in selection['prior_rows'] if p['review_id']==prior_id)], 'alias own source locator differs')
        proposal,prior_proposal=original['by_review_id'][rid],prior['by_review_id'][prior_id]
        activity=own_activity(declaration['source_semantic'],proposal,original,load)
        prior_activity=own_activity(declaration['represented_semantic'],prior_proposal,prior,load)
        result=alias_relation(declaration,proposal,prior_proposal,activity,prior_activity,
            artifacts._source_events(proposal,original['ledger']),artifacts._source_events(prior_proposal,prior['ledger']))
        require(expected[prior_id][3]*60==result['represented_seconds'],'retained alias cell duration differs')
        aliases[rid]=prior_id;represented.add(prior_id)
        alias_results.append({'source_review_id':rid,'represented_review_id':prior_id,**result})
    literal=sorted(set(original['by_review_id'])&set(expected))
    require(set(literal)|set(aliases)==set(original['by_review_id']) and not set(literal)&set(aliases)
        and set(expected)==set(literal)|set(aliases.values()), 'source review availability partition is not exhaustive')
    actual=diagnostic.actual_entries(load(doc['actual_capture']),config);overlaps=[]
    for rid,p in original['by_review_id'].items():
        warning,relations=diagnostic.context_warning({'timing_context_intervals':[{'start':p['start'],'end':p['end']}]},actual)
        if relations:overlaps.append({'source_review_id':rid,'warning':warning,'actual_counterparts':relations,
            'same_accomplishment_or_financial_equivalence_inferred':False})
    diagnostics=verify_diagnostics(doc['diagnostics'],root,spreadsheet,title,load)
    return {'source_review_availability':{'status':'complete','review_ids':sorted(original['by_review_id']),'review_availability_only':True},
        'literal_current_review_ids':literal,'literal_canonical_primary_delivery_claimed':False,
        'represented_pending_aliases':alias_results,'selected_review_ids':new_ids,'selected_minutes':sum(r[3] for r in new_rows),
        'retained_pending_review_ids':list(retained),'native_residual_minutes':accepted['native_residual_minutes'],
        'remaining_recoverable_minutes':accepted['remaining_recoverable_minutes'],
        'historical_pending_acceptance_sha256':accepted['acceptance_sha256'],'current_additive_native_checks':checks,
        'historical_warning_repetition_proof':warning_drift,
        'actual_overlap_warnings':overlaps,'diagnostics':diagnostics,'financial_coverage_claimed':False,
        'additional_credited_seconds':0,'posted_credits_created':0,'adoption_provider_writes':0,
        'current_capture_basis':'byte-bound actual native observations; no unstated wall-clock freshness',
        'retained_native_metadata_basis':'All captured editable CellData fields conserved; historical effectiveValue/formattedValue/effectiveFormat are derived display observations, not a fresh current claim.',
        'native_consumer_runtime':pending.artifact_handle(Path(__file__).resolve()),'proofs':dict(proofs)}

def verify_diagnostics(proof,root,spreadsheet,primary_title,load):
    require(set(proof)=={'preparation','prepublication_capture','publication_plan','publication_receipt','live_readback'},'diagnostic proof inventory differs')
    prep,plan,receipt=(load(proof[k]) for k in ('preparation','publication_plan','publication_receipt'))
    sid=plan['sheet_id'];title=primary_title.removesuffix('portfolio review')+'unresolved evidence'
    require(Path(prep['source_stage']['run_dir'])==root and prep['layout']==monthly.LEGACY_LAYOUT
        and plan['schema']=='source-bound-diagnostic-availability-publication-plan/v1'
        and receipt['schema']=='source-bound-diagnostic-availability-publication-receipt/v1'
        and plan['source_preparation_sha256']==proof['preparation']['sha256'][7:]
        and receipt['publication_plan']==proof['publication_plan']['path']
        and receipt['readback']==proof['live_readback']['path']
        and (plan['spreadsheet_id'],receipt['spreadsheet_id'],receipt['sheet_id'])==(spreadsheet,spreadsheet,sid)
        and receipt['range']==plan['range'] and plan['new_minutes']==receipt['new_minutes']==0
        and plan['diagnostic_resolution_claimed'] is receipt['diagnostic_resolution_claimed'] is False
        and receipt['clockify_writes']==0,'diagnostic publication source, target or credit claims differ')
    require(proof['prepublication_capture']['sha256']=='sha256:'+plan['prepublication_capture_sha256'],
        'diagnostic actual prepublication capture binding differs')
    before,before_raw=native_rows(load(proof['prepublication_capture']),spreadsheet,title,sid,12,header=monthly.LEGACY_HEADER)
    current,current_raw=native_rows(load(proof['live_readback']),spreadsheet,title,sid,12,header=monthly.LEGACY_HEADER)
    require(sorted(before)==list(range(1,len(before)+1)) and sorted(current)==list(range(1,len(current)+1))
        and len(before)-1==plan['existing_rows_to_preserve']==receipt['data_rows_before']
        and all(current_raw.get(n)==row for n,row in before_raw.items()), 'existing diagnostic full native cells were changed')
    canonical={row[0]:row for row in monthly.rows_for_layout(monthly.project_rows(root),monthly.LEGACY_LAYOUT)}
    observed=selected._by_identity({n:r for n,r in current.items() if n>1})
    old=selected._by_identity({n:r for n,r in before.items() if n>1})
    append_rows=[row for rid,row in canonical.items() if rid not in old]
    require(append_rows==plan['rows']==prep['append_candidate_rows'] and receipt['new_rows']==len(append_rows)
        and len(current)-len(before)==len(append_rows) and len(current)-1==receipt['data_rows_after'],
        'diagnostic exact append partition differs')
    match=re.fullmatch(r'A([1-9][0-9]*):L([1-9][0-9]*)',plan['range'])
    require(match and int(match[1])==len(before)+1 and int(match[2])==len(current)
        and [current.get(n) for n in range(int(match[1]),int(match[2])+1)]==append_rows,
        'diagnostic original exact publication range differs')
    records={r['stable_id']:r for r in prep['records']}
    require(len(records)==len(prep['records'])==len(canonical) and set(records)==set(canonical)
        and all(records[i]['canonical_row']==r for i,r in canonical.items()), 'diagnostic canonical preparation differs')
    alias_ids=[];literal_ids=[];changed=[]
    for rid,row in canonical.items():
        require(rid in observed and row[7]=='' and row[10]=='needs_review', 'diagnostic identity, duration or status differs')
        number,actual=observed[rid];record=records[rid]
        if rid in old:
            declaration=record['canonical_alias_api_proof'];h=declaration['historical_source']
            load(h)
            alias=monthly.canonical_source_alias(root,row,actual,layout=monthly.LEGACY_LAYOUT,
                historical_sources={declaration['source_run_id']:h})
            require(alias==declaration and actual[:11]==row[:11] and actual[10]=='needs_review'
                and record['current_native']['all_values_sha256']==pending.digest(actual),
                'retained diagnostic source alias or current core differs')
            alias_ids.append(rid)
        else:
            require(actual==row,'published diagnostic all12values differ');literal_ids.append(rid)
        for context in record.get('same_evidence_contexts_not_same_outcome_aliases',[]):
            cid=context['current_id'];require(cid in old and cid in observed,'changed-kind context is missing')
            _,prior=observed[cid];lineage=json.loads(prior[11]);requested=json.loads(row[11])
            require(prior[3]!=row[3] and lineage['evidence_ids']==requested['evidence_ids']
                and context['kind_equal'] is context['positive_same_outcome_alias_proven'] is False,
                'changed diagnostic kind was promoted to alias')
            historical=Path(context['source_root']);events,_=monthly._ledger_events(historical)
            now,_=monthly._ledger_events(root)
            for relative,sha in lineage['artifacts'].items():load({'path':str(historical/relative),'sha256':sha})
            require(all(events.get(i)==now.get(i) and i in events for i in requested['evidence_ids']),
                'changed-kind context sealed source differs')
            changed.append({'current_diagnostic_id':rid,'context_diagnostic_id':cid,'current_kind':row[3],
                'context_kind':prior[3],'same_outcome_alias_proven':False,'warning':'Same exact source evidence with a different diagnostic kind is context only; neither diagnostic is resolved and no additional time is established.'})
    verify_diagnostic_formats(plan,before_raw,current_raw)
    return {'status':'complete','review_ids':sorted(canonical),'literal_review_ids':literal_ids,
        'historical_alias_ids':alias_ids,'changed_kind_context_warnings':changed,'review_availability_only':True,
        'literal_canonical_all_cells_claimed':False,'diagnostic_resolution_claimed':False,'additional_minutes':0}

def verify_diagnostic_formats(plan,before,current):
    requests=plan['requests'];require(len(requests)==5 and list(requests[-1])==['updateCells'], 'diagnostic native request inventory differs')
    update=requests[-1]['updateCells'];dest=update['range'];start,end=dest['startRowIndex'],dest['endRowIndex']
    require(dest=={'sheetId':plan['sheet_id'],'startRowIndex':len(before),'endRowIndex':len(current),'startColumnIndex':0,'endColumnIndex':12}
        and update['fields']=='userEnteredValue' and len(update['rows'])==end-start
        and requests[0]=={'appendDimension':{'sheetId':plan['sheet_id'],'dimension':'ROWS','length':end-start}},
        'diagnostic native append request target differs')
    source={**dest,'startRowIndex':start-1,'endRowIndex':start}
    for request,mode in zip(requests[1:3],('PASTE_FORMAT','PASTE_DATA_VALIDATION'),strict=True):
        require(request=={'copyPaste':{'source':source,'destination':dest,'pasteType':mode,'pasteOrientation':'NORMAL'}},
            'diagnostic format/validation request differs')
    require(requests[3]=={'repeatCell':{'range':dest,'cell':{},'fields':'userEnteredFormat.textFormat.link'}},
        'diagnostic format cleanup differs')
    exemplar=before[start]['values']
    for offset,declared in enumerate(update['rows']):
        actual=current[start+offset+1]['values']
        require(len(actual)==len(declared['values'])==len(exemplar)==12,'diagnostic cell count differs')
        for expected,cell,template in zip(declared['values'],actual,exemplar,strict=True):
            value=expected.get('userEnteredValue',{})
            require(cell.get('userEnteredValue',{})==({} if value=={'stringValue':''} else value), 'diagnostic exact typed value differs')
            formatting=copy.deepcopy(template.get('userEnteredFormat',{}))
            formatting.get('textFormat',{}).pop('link',None)
            require(cell.get('userEnteredFormat',{})==formatting
                and cell.get('dataValidation')==template.get('dataValidation'), 'diagnostic original format or validation differs')
