"""Read-only own-entry semantic authority, never aggregate packet semantics."""
from __future__ import annotations

import json
from pathlib import Path

from scripts import clockify_pending_review_selection as pending
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_source_adoptions as adoptions

SCHEMA = 'pending-append-own-financial-lineages/v1'


def _read(handle, cache):
    return json.loads(adoptions._capture(handle, cache))


def _activity(lineage, proposal, proposal_path, cache):
    ref = lineage['semantic_ref']
    if (set(ref) != {'artifact', 'activity_id', 'activity_sha256'}
            or Path(ref['artifact']['path']) != Path(proposal_path).parent / 'semantic-analysis.json'
            or ref['activity_id'] != proposal['activity_id']):
        raise ValueError('own financial lineage semantic identity differs')
    matches = [activity for activity in _read(ref['artifact'], cache)['activities']
               if activity['activity_id'] == ref['activity_id']]
    if (len(matches) != 1 or pending.digest(matches[0]) != ref['activity_sha256']
            or set(matches[0]['evidence_ids']) != set(proposal['provenance']['evidence_ids'])):
        raise ValueError('own financial lineage activity/source membership differs')
    return matches[0]


def _same_sealed_source_objects(first, second):
    """Exact native ownership only; no timestamp or source alias refinement."""
    return sorted(pending.digest(event) for event in first) == sorted(pending.digest(event) for event in second)


def _supplemental_pending_source(lineage, cache):
    """Authenticate original packet bytes, not completed-run/full-source quality."""
    record = lineage['source']
    if lineage['replay'] != record:
        raise ValueError('own financial lineage supplemental source/replay declaration differs')
    try:
        source = pending._source(record, cache)
    except (KeyError, TypeError) as exc:
        raise ValueError('own financial lineage supplemental receipt is malformed') from exc
    receipt = source['receipt']
    primary, replay = receipt.get('native_accounting_primary'), receipt.get('native_accounting_replay')
    if (not isinstance(primary, str) or not primary or not isinstance(replay, str) or not replay
            or not isinstance(receipt.get('label'), str) or not receipt['label']
            or source['run_id'] != receipt['label']):
        raise ValueError('own financial lineage supplemental receipt identity differs')
    primary, replay = Path(primary), Path(replay)
    expected = {'proposals': primary / 'proposals.json', 'accounting': primary / 'work-accounting-result.json',
                'replay_proposals': replay / 'proposals.json', 'replay_accounting': replay / 'work-accounting-result.json'}
    if (any(Path(source['artifacts'][name]['path']) != path for name, path in expected.items())
            or source['proposals'] != source['accounting']['proposals']):
        raise ValueError('own financial lineage supplemental original packet artifacts differ')
    # pending._source authenticates proposal/accounting replay bytes. Ownership
    # additionally authenticates this own activity's ORIGINAL semantic packet
    # and its saved replay against the same original receipt, never a fresh core.
    semantic = receipt.get('deterministic_accounting_replay', {}).get('semantic-analysis.json')
    if (not isinstance(semantic, dict) or semantic.get('byte_equal') is not True
            or not isinstance(semantic.get('primary_sha256'), str)
            or len(semantic['primary_sha256']) != 64
            or semantic.get('replay_sha256') != semantic['primary_sha256']):
        raise ValueError('own financial lineage supplemental semantic replay receipt differs')
    expected_semantic = {'path': str(primary / 'semantic-analysis.json'),
                         'sha256': 'sha256:' + semantic['primary_sha256']}
    if lineage['semantic_ref'].get('artifact') != expected_semantic:
        raise ValueError('own financial lineage supplemental semantic artifact differs from original receipt')
    _read(expected_semantic, cache)
    _read({'path': str(replay / 'semantic-analysis.json'),
           'sha256': 'sha256:' + semantic['replay_sha256']}, cache)
    return source


def authenticate(lineage, *, surface, identifier, record, captured, actual, cache):
    """Validate receipt→review→proposal→activity and current financial binding."""
    gaps = []
    if surface == 'posted':
        if set(lineage) != {'basis', 'review_id', 'artifacts', 'semantic_ref'} or lineage['basis'] != 'native-posted':
            raise ValueError('own financial lineage posted schema differs')
        entry = actual[identifier]
        proof = adoptions.verify_prior_native_proof(lineage['artifacts'], lineage['review_id'], identifier,
            workspace_id=entry['workspaceId'], member_id=entry['userId'], capture_cache=cache)
        proposal, events = proof['prior_proposal'], proof['source_events']
        ledger = _read(lineage['artifacts']['source_ledger'], cache)
        proposal_path = lineage['artifacts']['prior_proposals']['path']
        if not adoptions.current_live_matches(proof['payload'], entry, workspace_id=proof['workspace_id'],
                member_id=proof['member_id'], entry_id=identifier):
            gaps.append('current-clockify-payload-differs-from-own-native-receipt')
        seconds = adoptions._seconds(proof['payload'])
        financial = {'project_suffix': proof['payload']['projectId'][-6:],
                     'tag_suffixes': [tag[-8:] for tag in proof['payload']['tagIds']],
                     'billable': proof['payload']['billable'],
                     'start': proof['payload']['start'], 'end': proof['payload']['end']}
    elif surface == 'pending':
        if (set(lineage) != {'basis', 'review_id', 'source', 'replay', 'semantic_ref'}
                or lineage['basis'] != 'native-pending' or lineage['review_id'] != identifier):
            raise ValueError('own financial lineage pending requires genuine completed source/replay')
        if (lineage['source'].get('basis') == 'completed-review-run'
                and lineage['replay'].get('basis') == 'completed-review-replay'):
            source, replay = pending._source(lineage['source'], cache), pending._source(lineage['replay'], cache)
            if (source['proposals'] != source['accounting']['proposals']
                    or any(source[name] != replay[name] for name in ('proposals', 'accounting', 'ledger', 'routing', 'replay'))):
                raise ValueError('own financial lineage pending full native replay differs')
            publisher.verify_gates(source['proposals'], source['quality'], replay['replay'], source['run_id'])
        elif (lineage['source'].get('basis') == 'supplemental-native-packet'
                and lineage['replay'].get('basis') == 'supplemental-native-packet'):
            source = _supplemental_pending_source(lineage, cache)
        else:
            raise ValueError('own financial lineage pending requires genuine completed source/replay or original supplemental packet')
        proposal = source['by_review_id'].get(identifier)
        if proposal is None:
            raise ValueError('own financial lineage pending proposal is absent')
        expected = publisher.proposal_row(proposal, source['run_id'], project_allowlist=publisher.project_allowlist(source['routing']))
        prior = captured[identifier]
        if any(prior[index] != expected[index] for index in range(9)):
            gaps.append('current-pending-machine-fields-differ-from-own-native-source')
        ledger, events = source['ledger'], adoptions._source_events(proposal, source['ledger'])
        proposal_path = source['artifacts']['proposals']['path']
        seconds = proposal['duration_seconds']
        financial = {'project_suffix': proposal['clockify_project_suffix'], 'tag_suffixes': proposal['tag_suffixes'],
                     'billable': proposal['billable'], 'start': proposal['start'], 'end': proposal['end']}
    else:
        raise ValueError('own financial lineage surface differs')
    if publisher.stable_review_id(proposal) != lineage['review_id']:
        raise ValueError('own financial lineage native review identity differs')
    activity = _activity(lineage, proposal, proposal_path, cache)
    # Ownership binds the original exact sealed objects, including recordings,
    # tool/system messages and locators. It grants no alias/equivalence license;
    # candidate precision correspondence is a separate comparison-only lane.
    if not _same_sealed_source_objects(events, record['events']):
        raise ValueError('own financial lineage does not authenticate this complete financial source binding')
    return {'lineage': lineage, 'lineage_sha256': pending.digest(lineage), 'proposal': proposal,
            'semantic_ref': lineage['semantic_ref'], 'core': {key: activity.get(key) for key in ('action', 'object', 'outcome', 'lifecycle')},
            'events': events, 'duration_seconds': seconds, 'financial': financial,
            'gaps': gaps, 'verified_current': not gaps}


def verify(handle, *, records, captured, actual, cache):
    if handle is None:
        return {}
    document = _read(handle, cache)
    if (set(document) != {'schema_version', 'records'} or document['schema_version'] != SCHEMA
            or not isinstance(document['records'], list)):
        raise ValueError('own financial lineage inventory schema differs')
    results = {}
    for declaration in document['records']:
        if set(declaration) != {'surface', 'id', 'lineage'}:
            raise ValueError('own financial lineage inventory binding differs')
        key = (declaration['surface'], declaration['id'])
        matches = [record for record in records if (record['surface'], record['id']) == key]
        if key in results or len(matches) != 1:
            raise ValueError('own financial lineage counterpart is absent or ambiguous')
        results[key] = authenticate(declaration['lineage'], surface=key[0], identifier=key[1],
            record=matches[0], captured=captured, actual=actual, cache=cache)
    return results
