"""Fixed, effect-free readers for the exact retained immutable native runtime.

This is not an execution API. Receipt locations never select executable code;
the retained consumer's full release tree and every recorded role are pinned.
Current ordinary inference/request construction remains in the current code.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from scripts import clockify_source_adoptions as adoptions

NATIVE_SHA = 'cafe70dcfcf480f9a1bc65f586602f13bff18a3b'
NATIVE_TREE = '9a04f455661ab47b052e70adfd37b115cb1dd4607ccd9b9f0910382c3af2ee5b'
_ROLE_FILES = {'consumer': 'clockify_pending_review_selection.py',
               'pipeline': 'work_accounting_pipeline.py', 'allocator': 'work_allocator.py'}
_MAX_OUTPUT = 16 * 1024 * 1024
_PROJECTION_FILES = frozenset({
    'review-learning-cases.json', 'review-regression-cases.json', 'semantic-analysis.json',
    'allocation-report.json', 'fathom-reconciliation.json', 'review-regression-results.json',
    'proposals.json', 'ambiguous.json', 'skipped.json', 'review-tombstones.json',
    'work-accounting-result.json',
})


def _release() -> Path:
    root = Path.home() / 'Work/automation-clockify-sync-releases' / NATIVE_SHA
    helper_path = Path(__file__).resolve().parents[1] / 'ops/systemd/user/clockify_review_cycle_release.py'
    spec = importlib.util.spec_from_file_location('clockify_historical_release_identity', helper_path)
    if spec is None or spec.loader is None:
        raise ValueError('historical runtime identity validator is unavailable')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    identity = helper._identity(root, NATIVE_SHA)
    if identity['tree_digest'] != NATIVE_TREE:
        raise ValueError('historical runtime differs from the authenticated retained tree')
    return root


def _handle(path: Path) -> dict[str, str]:
    handle = {'path': str(Path(path).absolute()),
              'sha256': 'sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest()}
    adoptions._capture(handle, {})
    return handle


# Only these named readers exist in the child. The Python audit hook denies
# even attempted provider access, process creation and filesystem mutation.
_PROOF = r'''import base64,hashlib,json,os,sys
from pathlib import Path
class NoOutput:
    def write(self,value):
        if value: raise RuntimeError('historical reader emitted unexpected output')
    def flush(self): pass
sys.stdout=NoOutput()
def guard(event,args):
    if event.startswith(('socket.','os.exec','os.spawn')) or event in {'subprocess.Popen','os.system','os.posix_spawn'}:
        raise RuntimeError('historical reader forbids network and children')
    if event=='open' and isinstance(args[2],int) and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
        raise RuntimeError('historical reader forbids writes')
    if event in {'os.remove','os.rename','os.mkdir','os.rmdir','os.chmod','os.chown','os.utime','os.link','os.symlink','os.truncate'}:
        raise RuntimeError('historical reader forbids mutations')
sys.addaudithook(guard)
request=json.load(sys.stdin)
from scripts import clockify_source_adoptions as adoptions
def pins():
    for handle in request['pins']: adoptions._capture(handle,{})
def _preserve_saved_semantic_order(saved, reconstructed):
    """Align only identity-equal native collections; never change core fields."""
    import copy
    result=copy.deepcopy(reconstructed)
    for section in ('activities','exceptions','omissions'):
        original=saved.get(section); actual=result.get(section)
        if not isinstance(original,list) or not isinstance(actual,list):
            raise ValueError('historical semantic collection is invalid')
        def identity(row):
            if not isinstance(row,dict):
                raise ValueError('historical semantic row is invalid')
            if section=='activities':
                key=row.get('activity_id')
                if not isinstance(key,str) or not key:
                    raise ValueError('historical semantic activity identity is invalid')
                return key
            ids=row.get('evidence_ids')
            if (not isinstance(ids,list) or not ids
                or any(not isinstance(value,str) or not value for value in ids)
                or len(set(ids))!=len(ids)):
                raise ValueError('historical semantic evidence group is invalid')
            return tuple(sorted(ids))
        original_keys=[identity(row) for row in original]
        actual_keys=[identity(row) for row in actual]
        if (len(set(original_keys))!=len(original_keys)
            or len(set(actual_keys))!=len(actual_keys)
            or set(original_keys)!=set(actual_keys)):
            raise ValueError('historical semantic collection identities differ')
        by_key=dict(zip(actual_keys,actual)); ordered=[]
        excluded={'extractor_model','rendered_description'} if section=='activities' else set()
        for key, source_row in zip(original_keys,original):
            row=by_key[key]
            if ({name:value for name,value in row.items() if name not in excluded}
                != {name:value for name,value in source_row.items() if name not in excluded}):
                raise ValueError('historical semantic collection content differs')
            for name in excluded:
                if name in source_row: row[name]=copy.deepcopy(source_row[name])
                else: row.pop(name,None)
            ordered.append(row)
        result[section]=ordered
    return result
def identity(value):
    names=('run_dir','slice_id','since_utc','until_utc','source_coverage_digest',
           'collector_runtime_identity','legacy_completion_bundle_digest','source_bundle_digest',
           'verified_artifact_digests')
    result={name:(str(getattr(value,name)) if name=='run_dir' else getattr(value,name)) for name in names}
    for name in ('pending_binding','native_checkpoint_metadata'):
        if hasattr(value,name): result[name]=getattr(value,name)
    result['generated_native_artifacts']={relative:base64.b64encode(content).decode('ascii')
        for relative,content in value.verified_artifact_bytes.items()
        if relative.startswith(receipts.NATIVE_CHECKPOINT_PREFIX) and not (value.run_dir/relative).is_file()}
    return result
phase='inputs'
try:
    pins()
    from scripts import clockify_review_run as review, collector_receipts as receipts
    review._configure_runs_root(Path(request['runs_root']))
    operation=request['operation']; data=request['data']; phase=operation
    if operation=='pending':
        from scripts import clockify_pending_review_selection as pending, clockify_sheet_publish as publisher
        actual=data['acceptance']; artifacts=actual['current_source_artifacts']
        proposals=json.loads(adoptions._capture(artifacts['proposals'],{}))
        routing=json.loads(adoptions._capture(artifacts['routing'],{}))
        source=Path(artifacts['proposals']['path']).parent
        result=pending.verify(bindings_path=Path(actual['selection']['path']),source_dir=source,
            proposals=proposals,spreadsheet_id=actual['spreadsheet_id'],sheet_title=actual['sheet_title'],
            run_id=source.name,project_allowlist=publisher.project_allowlist(routing))
        result['prior']=[{key:record[key] for key in ('review_id','row','disposition')}
                         for record in result['prior']]
    elif operation=='cache':
        source=Path(data['source']); analysis=json.loads(adoptions._capture(data['analysis'],{}))
        original_retry=review.work_accounting_pipeline.run_scoped_failed_review_retry
        def ordered_retry(*args,**kwargs):
            return _preserve_saved_semantic_order(analysis,original_retry(*args,**kwargs))
        # Only the named, effect-free cache proof adapts scoped reconstruction.
        # The fixed native preflight still verifies every other output field,
        # lineage, complete saved request and exact used cache decision.
        review.work_accounting_pipeline.run_scoped_failed_review_retry=ordered_retry
        try:
            result=review._preflight_replay_analyzer_cache(source,Path(data['cache']['path']),analysis,
                retry_origin=Path(data['retry_origin']) if data['retry_origin'] else None,
                inference_context=Path(data['inference_context']) if data['inference_context'] else None)
        finally:
            review.work_accounting_pipeline.run_scoped_failed_review_retry=original_retry
    elif operation=='collector':
        source=Path(data['source'])
        result=identity(receipts.load_collector_source_bundle(source/'completion-bundle.json',run_dir=source))
    elif operation=='derivation':
        parent,value,lineage=review._verified_collector_derivation(Path(data['source']))
        result={'parent':str(parent),'identity':identity(value),'lineage':lineage}
    elif operation=='lineage':
        source=Path(data['source'])
        context=review._verified_replay_inference_context(source)
        if (source/'repair-source.json').is_file():
            review._repair_analysis_fixture(source)
        completed=receipts.load_completion_bundle(source/'completion-bundle.json',run_dir=source)
        if (context/'collector-source.json').is_file():
            parent,value,lineage=review._verified_collector_derivation(context)
        else:
            value=receipts.load_collector_source_bundle(context/'completion-bundle.json',run_dir=context)
        result={'inference_context':str(context),'identity':identity(value),
                'completion_bundle_digest':completed.bundle_digest}
    elif operation=='fresh':
        from scripts import clockify_review_cycle as cycle
        result=cycle._verify_fresh_native_source(data['config'],data['record'],data['source'])
    elif operation=='projection':
        from scripts import work_accounting_pipeline as accounting
        source=Path(data['source']); target=Path(data['target'])
        analysis=json.loads(adoptions._capture(data['analysis'],{}))
        review._preflight_replay_analyzer_cache(source,Path(data['cache']['path']),analysis,
            inference_context=Path(data['inference_context']))
        allowed=set(data['outputs']); captured={}
        def capture(path,value):
            if path.parent != target or path.name not in allowed:
                raise ValueError('native projection output is outside the fixed inventory')
            captured[path.name]=json.loads(json.dumps(value,ensure_ascii=False))
        accounting._write_json=capture
        accounting.run_accounting(target,root=Path.cwd(),
            routing_path=Path(data['routing']['path']),
            corrections_path=Path(data['corrections']['path']),
            analysis_fixture=Path(data['analysis']['path']),
            analyzer_cache_path=Path(data['cache']['path']))
        if set(captured)!=allowed: raise ValueError('native projection output inventory differs')
        result=captured
    else: raise ValueError('unsupported historical read operation')
    phase='input_recheck'; pins()
    returned={'result':result}
except Exception as exc:
    returned={'error_type':type(exc).__name__,'proof_phase':phase}
payload=json.dumps(returned,separators=(',',':')).encode()
if len(payload)>16777216: raise RuntimeError('historical reader output exceeds bound')
os.write(1,payload)
'''


def _invoke(operation: str, data: Mapping[str, Any], *, pins: list[dict[str, str]],
            runs_root: Path) -> Any:
    release = _release()
    for handle in pins:
        adoptions._capture(handle, {})
    request = {'operation': operation, 'data': data, 'pins': pins, 'runs_root': str(runs_root)}
    try:
        completed = subprocess.run(['/usr/bin/python3', '-B', '-c', _PROOF], cwd=release,
            env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'},
            input=json.dumps(request), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('historical native reader could not finish') from exc
    if completed.returncode or len(completed.stdout) > _MAX_OUTPUT:
        raise ValueError('historical native reader failed or exceeded its output bound')
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or set(value) != {'result'}:
        if isinstance(value, dict) and set(value) == {'error_type', 'proof_phase'}:
            raise ValueError('historical native ' + str(value['proof_phase']) +
                             ' proof failed (' + str(value['error_type']) + ')')
        raise ValueError('historical native reader returned an invalid proof')
    for handle in pins:
        adoptions._capture(handle, {})
    _release()  # Detect concurrent runtime replacement rather than trusting cwd.
    return value['result']


def pending_selection(actual: Mapping[str, Any]) -> dict[str, Any]:
    release = _release()
    roles = actual.get('runtime_artifacts')
    if not isinstance(roles, Mapping) or set(roles) != set(_ROLE_FILES):
        raise ValueError('historical pending runtime role inventory differs')
    pins = [actual['selection'], *actual['current_source_artifacts'].values()]
    for role, filename in _ROLE_FILES.items():
        current = _handle(release / 'scripts' / filename)
        recorded = roles[role]
        try:
            adoptions._capture(recorded, {})
            if recorded['sha256'] != current['sha256']:
                raise ValueError('role bytes differ')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError('historical pending runtime artifact differs') from exc
        pins.extend((recorded, current))
    source = Path(actual['current_source_artifacts']['proposals']['path']).parent
    result = _invoke('pending', {'acceptance': actual}, pins=pins, runs_root=source.parent)
    from scripts import clockify_pending_runtime_proof as proof
    result['receipt'] = proof.verify_recorded(actual, result['receipt'])
    return result


def preflight_cache(source: Path, cache: Path, analysis: Mapping[str, Any], *,
                    retry_origin: Path | None = None, inference_context: Path | None = None) -> list[dict[str, str]]:
    analysis_handle = _handle(source / 'semantic-analysis.json')
    if json.loads(adoptions._capture(analysis_handle, {})) != analysis:
        raise ValueError('historical semantic input differs from its immutable source')
    cache_handle = _handle(cache)
    context = inference_context or source
    pins = [analysis_handle, cache_handle, _handle(source / 'evidence/evidence-ledger.json'),
            _handle(context / 'routing.json'), _handle(context / 'review-corrections.jsonl')]
    return _invoke('cache', {'source': str(source), 'analysis': analysis_handle, 'cache': cache_handle,
        'retry_origin': str(retry_origin) if retry_origin else None,
        'inference_context': str(inference_context) if inference_context else None},
        pins=pins, runs_root=source.parent)


def _source_pins(source: Path) -> list[dict[str, str]]:
    # The native reader verifies receipt/ledger/lineage hashes as well. Capture
    # existing source files so a concurrent change cannot go unnoticed.
    # Optional diagnostic aliases are not native evidence. Every actually used
    # handle remains subject to the native consumer's no-symlink checks.
    return [_handle(path) for path in sorted(source.rglob('*'))
            if path.is_file() and not path.is_symlink()]


def collector_source(source: Path):
    return _identity_from_document(_invoke('collector', {'source': str(source)},
        pins=_source_pins(source), runs_root=source.parent))


def collector_derivation(source: Path):
    result = _invoke('derivation', {'source': str(source)},
        pins=_source_pins(source), runs_root=source.parent)
    return Path(result['parent']), _identity_from_document(result['identity']), result['lineage']


def legacy_inference_lineage(source: Path) -> Path:
    """Authenticate existing source/repair ancestry under the one fixed reader.

    Locators select immutable data only. Capture every ancestor inventory before
    the native validators resolve the same lineage, then recheck all handles.
    """
    source = Path(source)
    runs = source.parent
    pending = [source]
    seen: set[Path] = set()
    pins: list[dict[str, str]] = []
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        if current.parent != runs or current.is_symlink() or current.resolve() != current:
            raise ValueError('historical lineage source escapes its canonical run root')
        seen.add(current)
        pins.extend(_source_pins(current))
        for name in ('repair-source.json', 'replay-source.json', 'collector-source.json'):
            path = current / name
            if not path.exists() and not path.is_symlink():
                continue
            document = json.loads(adoptions._capture(_handle(path), {}))
            identifier = document.get('source_run_id') if isinstance(document, Mapping) else None
            if not isinstance(identifier, str) or not identifier or Path(identifier).name != identifier:
                raise ValueError('historical lineage parent identity is invalid')
            parent = runs / identifier
            if parent in seen:
                raise ValueError('historical lineage source ancestry loops')
            pending.append(parent)
            binding = document.get('pending_source_binding')
            if binding is not None:
                if not isinstance(binding, Mapping):
                    raise ValueError('historical pending ancestor binding is invalid')
                backlog = _handle(Path(binding['backlog_manifest_path']))
                if backlog['sha256'] != binding.get('backlog_manifest_digest'):
                    raise ValueError('historical pending backlog binding differs')
                pins.append(backlog)
                snapshot = json.loads(adoptions._capture(_handle(
                    current / 'evidence/clockify-native-checkpoint/snapshot.json'), {}))
                base = Path(backlog['path']).parent / 'source-checkpoints'
                # The fixed native pending reader derives its directory from
                # checkpoint identity, not the copied snapshot's old locator.
                checkpoint_id = hashlib.sha256(json.dumps(snapshot['checkpoint_identity'],
                    ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                inventory = binding['native_checkpoint_digests']
                if not isinstance(inventory, Mapping) or 'manifest.json' not in inventory:
                    raise ValueError('historical pending checkpoint manifest binding is missing')
                for relative, digest in inventory.items():
                    name = Path(relative)
                    if name.is_absolute() or '..' in name.parts:
                        raise ValueError('historical pending checkpoint member escapes')
                    handle = _handle(base / checkpoint_id / name)
                    if handle['sha256'] != digest:
                        raise ValueError('historical pending checkpoint binding differs')
                    pins.append(handle)
    result = _invoke('lineage', {'source': str(source)}, pins=pins, runs_root=runs)
    context = Path(result['inference_context'])
    if context not in seen:
        raise ValueError('historical inference context is outside the captured lineage')
    _identity_from_document(result['identity'])
    return context


def _identity_from_document(document: Mapping[str, Any]):
    from scripts import collector_receipts as receipts
    import base64
    source = Path(document['run_dir'])
    generated = document.get('generated_native_artifacts', {})
    contents = {}
    for relative, digest in document['verified_artifact_digests'].items():
        if relative in generated:
            if not relative.startswith(receipts.NATIVE_CHECKPOINT_PREFIX):
                raise ValueError('historical projection introduced an unbound artifact')
            content = base64.b64decode(generated[relative], validate=True)
            if 'sha256:' + hashlib.sha256(content).hexdigest() != digest:
                raise ValueError('historical generated native artifact bytes differ')
            contents[relative] = content
        else:
            contents[relative] = adoptions._capture({'path': str(source / relative), 'sha256': digest}, {})
    if set(generated) - set(document['verified_artifact_digests']):
        raise ValueError('historical projection artifact inventory differs')
    values = {**document, 'run_dir': source, 'verified_artifact_bytes': contents}
    values.pop('generated_native_artifacts', None)
    factory = receipts.PendingCollectorSource if 'pending_binding' in values else receipts.CollectorSourceBundle
    if factory is receipts.PendingCollectorSource:
        if values.pop('legacy_completion_bundle_digest') is not None:
            raise ValueError('historical pending source incorrectly claims a completion')
    return factory(**values)


def fresh_source(config: Mapping[str, Any], record: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    directory = Path(source['run_dir'])
    return _invoke('fresh', {'config': config, 'record': record, 'source': source},
        pins=_source_pins(directory), runs_root=directory.parent)


def snapshot_projection(source: Path, target: Path) -> dict[str, Any]:
    """Calculate fixed native artifacts in memory; the caller owns new-run writes.

    The original sealed cache is preflighted before fixture-only accounting.
    Exact input bytes are required: this adapter cannot authorize new inference
    or changes to the source snapshot.
    """
    source, target = Path(source), Path(target)
    context = legacy_inference_lineage(source)
    analysis = _handle(source / 'semantic-analysis.json')
    cache = _handle(source / 'analyzer-cache-used.jsonl')
    routing = _handle(target / 'routing.json')
    corrections = _handle(target / 'review-corrections.jsonl')
    for relative in ('evidence/evidence-ledger.json', 'routing.json', 'review-corrections.jsonl'):
        if (source / relative).read_bytes() != (target / relative).read_bytes():
            raise ValueError('historical projection target input differs from source')
    result = _invoke('projection', {'source': str(source), 'target': str(target),
        'inference_context': str(context),
        'analysis': analysis, 'cache': cache, 'routing': routing, 'corrections': corrections,
        'outputs': sorted(_PROJECTION_FILES)},
        pins=[*_source_pins(source), _handle(target / 'evidence/evidence-ledger.json'),
              routing, corrections, _handle(context / 'routing.json'),
              _handle(context / 'review-corrections.jsonl')], runs_root=source.parent)
    if not isinstance(result, dict) or set(result) != _PROJECTION_FILES:
        raise ValueError('historical projection returned an invalid artifact inventory')
    return result
