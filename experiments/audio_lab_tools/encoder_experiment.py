"""Frozen, matched MusiCNN versus MERT representation experiment #708."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time

import numpy as np
from threadpoolctl import threadpool_limits

from . import encoder_models as models, individual_protocol as protocol, quality_round as evidence
from .dataset_windows import content_hash
from .encoder_assets import ROOT as ASSETS, MODEL, PYTHON
from .external_experiment import LAB, OUT, digest, pcm, probe, save, stamp
from .external_training import folds
from .focus_experiment import arrays, check_receipt, inside, load
from .mapped_vibe_inference import replay
from .mapped_vibe_rules import apply
from .memo_confirmation import holdout_pairs
from .quality_baselines import predict as current_predict, experts
from .quality_reference_audio import Extractor, response, provenance, GRAPH, PACKAGE
from .relax_experiment import global_groups
from .uplift_consensus import native_average

DEFAULT = LAB/'local/encoder-comparison-708-20261101/round-3'
PREVIOUS = LAB/'local/individual-20261030/remaining-1'
DOCUMENT = LAB/'ENCODER_COMPARISON_PLAN.md'
WORKER = Path(__file__).with_name('mert_worker.py')
MAX_SECONDS = 7200


def freeze(directory,feature_source=None):
    inputs = {'encoder-assets.json':ASSETS/'assets.json','encoder-requirements.lock':ASSETS/'requirements.lock'}
    inputs.update({f'adopted-{v}.json':LAB.parent.parent/f'src/yt_playlist/rec/data/{v}-vibes.json' for v in models.VIBES})
    metadata, source_rows, combined, ranges = {}, {}, [], {}
    for source in models.SOURCES:
        for ext in ('json','npz'):inputs[source+'.'+ext] = PREVIOUS/f'inputs/{source}.{ext}'
        meta = load(inputs[source+'.json'])
        metadata[source] = meta
        if source in (*models.VA[2:],):
            prepared = meta['rows']
        else:
            audio = OUT/(source+'.features.json')
            inputs['audio-identities/'+source+'.json'] = audio
            by_id = {r['id']:r for r in load(audio)['rows']}
            prepared = [{**by_id[r['id']],**r} for r in meta['rows']]
        source_rows[source] = prepared
        ranges[source] = slice(len(combined),len(combined)+len(prepared))
        combined.extend(prepared)
    for p in sorted((LAB/'local/per-vibe-20261029/all-sources-1/inputs').glob('*.json')):
        inputs['identities/'+p.name] = p
        combined.extend(load(p).get('rows',[]))
    local = LAB/'local/individual-20261030/local-1/labels.json'
    inputs['local-identities.json'] = local
    local_start = len(combined)
    combined.extend(load(local)['rows'])
    groups = global_groups(combined)
    protected = set(groups[local_start:])
    selected, excluded = [], {}
    for source in models.SOURCES:
        counts = Counter()
        candidates = []
        for i,row in enumerate(source_rows[source]):
            component = int(groups[ranges[source]][i])
            if component in protected:continue
            if not Path(row['audio']).is_file():continue
            candidates.append({**row,'source':source,'row_index':i,'component':component,
                'chunk_starts':row.get('chunk_starts',[row['sample_start']] if 'sample_start' in row else []),
                'native_target_names':models.NATIVE[source]})
        candidates.sort(key=lambda r:hashlib.sha256(('encoder-708-v1/'+source+'/'+r['id']).encode()).hexdigest())
        cohort = []
        for row in candidates:
            if counts[row['component']]>=2:continue
            cohort.append(row); counts[row['component']]+=1
            if len(cohort)==32:break
        if len(cohort)<24 or len(counts)<12:raise ValueError('Insufficient bounded native cohort: '+source)
        excluded[source] = {'available':len(candidates),'selected':len(cohort),'selected_components':len(counts),
            'local_component_exclusions':int(sum(g in protected for g in groups[ranges[source]]))}
        selected.extend(cohort)
    assigned = folds(np.array([r['component'] for r in selected]),count=5,seed=models.SEED)
    for row,fold in zip(selected,assigned,strict=True):row['outer']=int(fold)
    weights = {}
    for vibe in models.VIBES:
        artifact = load(inputs[f'adopted-{vibe}.json'])
        weights[vibe] = ({models.CAL:1.} if vibe=='focus' else {models.JAM:1.} if vibe=='release'
            else {s:1/len(experts(artifact)) for s in experts(artifact)} if vibe=='cruise' else artifact['weights'])
    protocol_spec = {'document_sha256':digest(DOCUMENT),'cohort_seed':'encoder-708-v1/source/id',
        'maximum_per_source':32,'maximum_per_component_per_source':2,'outer_folds':5,'inner_folds':3,'seed':models.SEED,
        'native_targets':models.NATIVE,'weights':weights,'ridge_alphas':[.1,1,10,100,1000],'constant_control':True,
        'musicnn':provenance(),'mert':load(ASSETS/'assets.json'),
        'mert_policy':'FFmpeg exact same ten-second interval, 24 kHz mono float32; published processor normalization; eval float32 CPU one thread; last transformer layer mean over time, 768 values; no layer choice/PCA/learned pooling.',
        'aggregation':'Equal mean per-chunk vectors for head training. Native predictions clip each chunk then equal mean, then unchanged fixed mappings and mixture weights. Inner selection evaluates the same aggregation.',
        'cross_source':'Omit entire destination source (both Many strata together); exclude every destination component from every remaining source before native inner selection. Fixed remaining mixture ratios renormalized.',
        'bootstrap_seed':protocol.SEED,
        'budgets':{'extraction_seconds':MAX_SECONDS,'fit_evaluation_seconds':3600,'worker_peak_rss_bytes':4*1024**3,
            'feature_bytes':256*1024**2,'private_environment_bytes':4*1024**3,'bootstrap_draws':1000},
        'acceptance':{'mean_primary_ndcg_gain':.02,'mean_primary_lift_gain':.01,'every_primary_ndcg_ci_positive':True,
            'maximum_domain_ndcg_loss':.01,'maximum_domain_lift_loss':.01,'maximum_mapped_mse_increase':.005,
            'minimum_case_rows':24,'minimum_case_components':12},
        'exposure':'All corpora previously inspected. All nonpilot Memo excluded, including newly exposed #710 confirmation. Exact current specialists are potentially fitted on these rows and diagnostic only; causal encoder comparison uses matched nested heads.'}
    references = [GRAPH,GRAPH.with_name('acquisition.json'),PACKAGE/'METADATA',PACKAGE/'RECORD',ASSETS/'upstream.json',
        *[MODEL/r['name'] for r in load(ASSETS/'assets.json')['files']],
        *[p for p in (ASSETS/'venv/lib/python3.12/site-packages').glob('*.dist-info/RECORD')],
        *{Path(r['audio']) for r in selected}]
    if feature_source is not None:
        evidence.verify(feature_source)
        receipt=check_receipt(feature_source,'extraction.receipt.json')
        protocol_spec['feature_source']={'directory':str(feature_source),'plan_sha256':digest(feature_source/'plan.json'),
            'receipt_sha256':digest(feature_source/'extraction.receipt.json'),
            'reason':'Archive the entrypoint and holdout helper before fitting/evaluation. The original freezer omitted __main__; no prospective entrypoint hash or AST-equivalence claim is available for extraction. Require fresh PCM/row-order and incumbent feature-parity checks before fitting; retain original cost.'}
        references.extend([feature_source/'plan.json',feature_source/'extraction.receipt.json',
            *[feature_source/name for name in receipt['outputs']]])
    plan = evidence.freeze(directory,708,protocol_spec,inputs,references=references,
        documents=[DOCUMENT,WORKER,Path(__file__).with_name('test_encoder_models.py'),
            LAB.parent/'investigations/encoder_comparison_708.py'])
    save(directory/'inputs/selection.json',{'rows':selected,'audit':excluded,'weights':weights})
    plan['inputs']['inputs/selection.json'] = digest(directory/'inputs/selection.json')
    save(directory/'plan.json',plan)
    print(json.dumps({'frozen':str(directory),'plan_sha256':digest(directory/'plan.json'),
        'rows':len(selected),'chunks':sum(len(r['chunk_starts']) for r in selected),'audit':excluded}),flush=True)


def reuse(directory,source):
    """Reuse only hash-verified identical feature computation, preserving its cost."""
    plan=load(directory/'plan.json')
    origin=plan['protocol'].get('feature_source')
    if not origin or origin['directory']!=str(source) or origin['plan_sha256']!=digest(source/'plan.json'):
        raise ValueError('Unfrozen feature source')
    evidence.verify(source)
    receipt=check_receipt(source,'extraction.receipt.json')
    old=load(source/'plan.json')
    for key in ('cohort_seed','native_targets','weights','musicnn','mert','mert_policy','aggregation','seed'):
        if plan['protocol'][key]!=old['protocol'][key]:raise ValueError('Feature/cohort protocol changed: '+key)
    if load(source/'inputs/selection.json')!=load(directory/'inputs/selection.json'):raise ValueError('Cohort changed')
    for name,checksum in old['inputs'].items():
        if plan['inputs'].get(name)!=checksum:raise ValueError('Frozen input changed: '+name)
    for name in ('audio_lab_tools/mert_worker.py','audio_lab_tools/quality_reference_audio.py','audio_lab_tools/preprocessing_native.py'):
        if plan['code'][name]!=old['code'][name]:raise ValueError('Feature worker changed: '+name)
    started=time.monotonic()
    outputs=[]
    for name in receipt['outputs']:
        target=directory/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source/name,target)
        outputs.append(target)
    audit={'created_at':stamp(),'source_plan_sha256':digest(source/'plan.json'),
        'source_extraction_receipt_sha256':digest(source/'extraction.receipt.json'),
        'source_entrypoint_archived':'audio_lab_tools/encoder_experiment.py' in old['code'],
        'extraction_entrypoint_ast_equivalence':'Unavailable: original __main__ was not archived.',
        'identical_cohort_inputs_workers_policies':True,
        'required_before_fit':['musicnn-parity.receipt.json','pcm-audit.receipt.json'],
        'new_encoder_calls':0,'copy_seconds':time.monotonic()-started,
        'cost_accounting':'Full original measured extraction/decode/model-loading/replay costs remain in extraction.json; copying is additional.'}
    save(directory/'feature-reuse.json',audit);outputs.append(directory/'feature-reuse.json')
    evidence.seal(directory,'extraction',outputs)
    print(json.dumps(audit),flush=True)


def extract(directory):
    if (directory/'extraction.receipt.json').exists():check_receipt(directory,'extraction.receipt.json');return
    selection = load(directory/'inputs/selection.json')['rows']
    started = time.monotonic()
    retained, rejected, outputs, profile = [], [], [], []
    musicnn_chunks, mert_chunks, counts = [], [], []
    env = {**os.environ,'HF_HOME':str(ASSETS/'hf-cache'),'HF_MODULES_CACHE':str(ASSETS/'hf-cache/modules'),
        'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'1'}
    cache_identity = digest(directory/'inputs/encoder-assets.json')
    musicnn_seconds,mert_seconds = 0.,0.
    with TemporaryDirectory(prefix='mert-pcm-',dir=directory) as temporary, Extractor(directory,seconds=MAX_SECONDS) as musicnn, (directory/'mert.log').open('w') as log:
        worker = subprocess.Popen([str(PYTHON),'-B',str(WORKER),str(MODEL)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=log,text=True,bufsize=1,env=env)
        try:
            runtime = response(worker,'READY ')
            for row in selection:
                xx,zz = [],[]
                try:
                    if time.monotonic()-started>MAX_SECONDS:raise RuntimeError('Frozen extraction deadline')
                    duration,channels = probe(row['audio'])
                    if digest(row['audio'])!=row['audio_sha256'] or channels not in (1,2):raise ValueError('Changed/unsupported source')
                    for start in row['chunk_starts']:
                        if start<0 or start+10>duration:raise ValueError('Incomplete exact annotated interval')
                        tick = time.monotonic()
                        vector,path = musicnn.chunk(row,start)
                        musicnn_seconds += time.monotonic()-tick
                        outputs.append(path);xx.append(vector)
                        identity = {'audio_sha256':row['audio_sha256'],'start':start,'seconds':10,'encoder_assets_sha256':cache_identity,
                            'sample_rate':24000,'pooling':'last-layer-time-mean'}
                        path = directory/'mert-chunks'/(content_hash(identity)+'.json')
                        tick = time.monotonic()
                        if path.exists():
                            record = load(path)
                            if record['input']!=identity or content_hash(record['result'])!=record['result_sha256']:raise ValueError('MERT cache changed')
                            result = record['result']
                        else:
                            signal = Path(temporary)/'input.f32'
                            pcm(row['audio'],start,24000,1,signal)
                            worker.stdin.write(json.dumps({'id':row['id'],'pcm':str(signal)})+'\n');worker.stdin.flush()
                            result = response(worker,'RESULT ')
                            if result['id']!=row['id']:raise ValueError('MERT returned different input')
                            result['pcm_sha256'] = digest(signal)
                            save(path,{'input':identity,'result':result,'result_sha256':content_hash(result)})
                            profile.append({k:result[k] for k in ('seconds','frames','peak_rss_bytes')})
                        mert_seconds += time.monotonic()-tick
                        z = np.asarray(result['embedding'])
                        if z.shape!=(768,) or not np.isfinite(z).all():raise ValueError('Invalid MERT vector')
                        zz.append(z);outputs.append(path)
                    retained.append({**row,'duration_seconds':duration,'channels':channels})
                    musicnn_chunks.append(xx);mert_chunks.append(zz);counts.append(len(xx))
                    print(json.dumps({'encoder_features':row['source'],'id':row['id'],'completed':len(retained)}),flush=True)
                except (ValueError,OSError) as error:rejected.append({'id':row['id'],'error':str(error)})
            first = retained[0]
            signal = Path(temporary)/'replay.f32'
            pcm(first['audio'],first['chunk_starts'][0],24000,1,signal)
            replay_errors,replay_seconds = [],[]
            for _ in range(3):
                worker.stdin.write(json.dumps({'id':'replay','pcm':str(signal)})+'\n');worker.stdin.flush()
                repeated = response(worker,'RESULT ')
                replay_errors.append(float(np.max(np.abs(np.asarray(repeated['embedding'])-mert_chunks[0][0]))))
                replay_seconds.append(repeated['seconds'])
            if max(replay_errors)>1e-6:raise ValueError('MERT replay exceeds frozen numerical bound')
        finally:
            worker.stdin.close()
            try:worker.wait(timeout=10)
            except subprocess.TimeoutExpired:worker.kill();worker.wait(timeout=10)
    width = max(counts)
    def padded(values,dimension):
        result = np.zeros((len(values),width,dimension))
        for i,row in enumerate(values):result[i,:len(row)] = row
        return result
    np.savez_compressed(directory/'features.npz',musicnn=padded(musicnn_chunks,200),mert=padded(mert_chunks,768),counts=np.array(counts))
    resources = {'total_seconds':time.monotonic()-started,'musicnn_decode_extraction_seconds':musicnn_seconds,
        'mert_decode_extraction_seconds':mert_seconds,'mert_runtime':runtime,'mert_profile':profile,
        'mert_replay_seconds':replay_seconds,'mert_replay_max_error':max(replay_errors),'musicnn':musicnn.resources,
        'feature_npz_bytes':(directory/'features.npz').stat().st_size,
        'musicnn_cache_bytes':sum(p.stat().st_size for p in (directory/'chunks').glob('*.json')),
        'mert_cache_bytes':sum(p.stat().st_size for p in (directory/'mert-chunks').glob('*.json')),
        'private_environment_bytes':sum(p.stat().st_size for p in ASSETS.rglob('*') if p.is_file()),
        'largest_child_peak_rss_bytes':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024}
    save(directory/'extraction.json',{'rows':retained,'rejected':rejected,'resources':resources})
    evidence.seal(directory,'extraction',[directory/'features.npz',directory/'extraction.json',*outputs])
    print(json.dumps({'retained':len(retained),'rejected':rejected,'resources':{k:v for k,v in resources.items() if k!='mert_profile'}}),flush=True)


def fit(directory):
    check_receipt(directory,'extraction.receipt.json')
    if load(directory/'plan.json')['protocol'].get('feature_source'):
        for receipt in ('musicnn-parity.receipt.json','pcm-audit.receipt.json'):
            check_receipt(directory,receipt)
    if (directory/'predictions.receipt.json').exists():check_receipt(directory,'predictions.receipt.json');return
    started = time.monotonic()
    rows = load(directory/'extraction.json')['rows']
    data = arrays(directory/'features.npz')
    groups = np.array([r['component'] for r in rows])
    outer = np.array([r['outer'] for r in rows])
    protocol.group_guard(groups,outer)
    weights = load(directory/'inputs/selection.json')['weights']
    positions = {s:np.array([i for i,r in enumerate(rows) if r['source']==s]) for s in models.SOURCES}
    y = {}
    for source,ids in positions.items():
        metadata = load(directory/f'inputs/{source}.json')
        original = arrays(directory/f'inputs/{source}.npz')['y']
        columns = [metadata['target_names'].index(n) for n in models.NATIVE[source]]
        y[source] = original[np.array([rows[i]['row_index'] for i in ids])][:,columns]
    result,trace,artifacts,replay_evidence = {},{},[],{}
    for representation in ('musicnn','mert'):
        chunks,counts = data[representation],data['counts']
        native = {s:np.zeros((len(rows),len(models.NATIVE[s]))) for s in models.SOURCES}
        for vibe in models.VIBES:result[f'{representation}__oof__{vibe}'] = np.empty(len(rows))
        def context(name,protected,omitted=()):
            if time.monotonic()-started>3600:raise RuntimeError('Frozen fit/evaluation preparation deadline')
            heads,traces = {},{}
            for source in models.SOURCES:
                if source in omitted:continue
                ids = positions[source]
                keep = np.array([g not in protected for g in groups[ids]],dtype=bool)
                selected = ids[keep]
                if len(set(groups[selected]))<3:raise ValueError('Insufficient native training components')
                heads[source],detail = models.choose(chunks[selected],counts[selected],y[source][keep],groups[selected])
                if set(groups[selected]) & protected:raise AssertionError('Held component entered selection')
                traces[source] = {**detail,'training_ids':[rows[i]['id'] for i in selected]}
            pack = directory/f'models/{representation}-{name}.json'
            save(pack,heads);artifacts.append(pack)
            trace[representation+'__'+name] = {'protected_components':sorted(map(int,protected)),
                'omitted_sources':list(omitted),'sources':traces}
            print(json.dumps({'encoder_fit':representation,'context':name,'heads':len(heads)}),flush=True)
            return heads
        first_heads = None
        for fold in range(5):
            valid = outer==fold
            heads = context('outer-'+str(fold),set(groups[valid]))
            if first_heads is None:first_heads=heads
            values = models.predict(heads,chunks[valid],counts[valid])
            for source,v in values.items():native[source][valid]=v
            for vibe in models.VIBES:result[f'{representation}__oof__{vibe}'][valid]=models.mixture(vibe,values,weights)
        for source,v in native.items():result[f'{representation}__native__{source}']=v
        families = {models.family(s) for s in models.SOURCES}
        for omitted in sorted(families):
            valid = np.array([r['source'] in omitted for r in rows])
            heads = context('omit-'+omitted[0],set(groups[valid]),omitted)
            values = models.predict(heads,chunks[valid],counts[valid])
            for vibe in models.VIBES:
                v = models.mixture(vibe,values,weights)
                if v is None:continue
                full = np.full(len(rows),np.nan);full[valid]=v
                result[f'{representation}__omit-{omitted[0]}__{vibe}']=full
        times, errors = [], []
        reference = models.predict(first_heads,chunks,counts)
        for _ in range(3):
            tick = time.perf_counter()
            repeated = models.predict(first_heads,chunks,counts)
            for vibe in models.VIBES:models.mixture(vibe,repeated,weights)
            times.append(time.perf_counter()-tick)
            errors.append(max(float(np.max(np.abs(reference[s]-repeated[s]))) for s in reference))
        replay_evidence[representation] = {'all_rows_all_heads_seconds':times,'maximum_error':max(errors)}
    x,counts = data['musicnn'],data['counts']
    for vibe in models.VIBES:
        artifact = load(directory/f'inputs/adopted-{vibe}.json')
        if vibe=='focus':v=native_average(artifact['heads'],x,counts,independent=True).mean(1)
        elif vibe=='release':
            raw = replay(artifact['model'],x.reshape(-1,200)).reshape(*x.shape[:2],-1)
            v=np.array([r[:n].mean() for r,n in zip(raw,counts,strict=True)])
        else:v,_=current_predict(artifact,x,counts)
        result['current__'+vibe]=v
    for source,value in y.items():result['truth__'+source]=value
    np.savez_compressed(directory/'predictions.npz',**result)
    save(directory/'fit-trace.json',{'created_at':stamp(),'contexts':trace,'rows':[r['id'] for r in rows],
        'resources':{'seconds':time.monotonic()-started,'head_replay':replay_evidence,
            'python_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'largest_head_pack_bytes':max(p.stat().st_size for p in artifacts),
            'all_head_packs_bytes':sum(p.stat().st_size for p in artifacts)}})
    evidence.seal(directory,'predictions',[directory/'predictions.npz',directory/'fit-trace.json',*artifacts])


def primary(source,vibe,mode):
    if vibe=='focus':return source==models.CAL and mode=='oof'
    if vibe=='release':return (source==models.JAM and mode=='oof') or (source==models.SOUND and mode!='oof')
    return source in (models.SOUND,models.NAT,models.GEN,models.MEMO)


def decision(vibe,tables):
    cases={k:t for k,t in tables.items() if t['vibe']==vibe and t['primary']}
    reasons=[]
    cells=[t['paired']['mert__vs__musicnn'] for t in cases.values()]
    if not cases or any(t['rows']<24 or t['components']<12 for t in cases.values()):
        reasons.append('Insufficient primary support.')
    means={}
    for metric in ('ndcg','top_lift','mse'):
        values=[c[metric]['difference'] for c in cells]
        means[metric]=(float(np.mean(values)) if values and all(v is not None and np.isfinite(v) for v in values)
                       else None)
    if means['ndcg'] is None or means['top_lift'] is None or means['ndcg']<.02 or means['top_lift']<.01:
        reasons.append('Mean primary ranking/lift gain below .02/.01 or unavailable.')
    for key,t in cases.items():
        c=t['paired']['mert__vs__musicnn']
        interval=c['ndcg']['conditional_95_percent_interval']
        if interval is None or not np.isfinite(interval).all() or interval[0]<=0:
            reasons.append('Primary NDCG uncertainty includes zero or is unavailable: '+key)
        values=[c[m]['difference'] for m in ('ndcg','top_lift','mse')]
        if any(v is None or not np.isfinite(v) for v in values):
            reasons.append('Primary metrics unavailable: '+key)
        elif values[0]<-.01 or values[1]<-.01 or values[2]>.005:
            reasons.append('Primary domain regression ceiling: '+key)
    return {'recommendation':'retain' if reasons else 'candidate_for_review','primary_cases':list(cases),
        'mean_primary_difference':means,'reasons':reasons}


def evaluate(directory):
    check_receipt(directory,'predictions.receipt.json')
    started = time.monotonic()
    rows = load(directory/'extraction.json')['rows']
    prediction = arrays(directory/'predictions.npz')
    tables,native,unsupported = {},{},[]
    for source in models.SOURCES:
        ids = np.array([i for i,r in enumerate(rows) if r['source']==source])
        groups = np.array([rows[i]['component'] for i in ids])
        outer = np.array([rows[i]['outer'] for i in ids])
        meta = load(directory/f'inputs/{source}.json')
        original = arrays(directory/f'inputs/{source}.npz')['y']
        yy = original[np.array([rows[i]['row_index'] for i in ids])]
        for j,label in enumerate(models.NATIVE[source]):
            y = prediction['truth__'+source][:,j]
            scores = {r:prediction[f'{r}__native__{source}'][ids,j] for r in ('musicnn','mert')}
            native[source+'__'+label] = {'rows':len(y),'components':len(set(groups)),
                'errors':{r:{'mse':float(np.mean((v-y)**2)),'mae':float(np.mean(np.abs(v-y)))} for r,v in scores.items()},
                'paired':protocol.paired(y,scores,outer,groups,[('mert','musicnn')],1000)}
        for vibe,expression in models.expressions(source).items():
            y = apply(expression,yy,meta['target_names'])
            for mode in ('oof','omit-'+models.family(source)[0]):
                if f'mert__{mode}__{vibe}' not in prediction:
                    unsupported.append({'source':source,'vibe':vibe,'mode':mode,'reason':'All fixed mixture experts omitted; no substitute source/mapping.'});continue
                scores = {r:prediction[f'{r}__{mode}__{vibe}'][ids] for r in ('musicnn','mert')}
                scores['current_exposed']=prediction['current__'+vibe][ids]
                if any(not np.isfinite(v).all() for v in scores.values()):raise ValueError('Missing matched predictions')
                split = outer if mode=='oof' else np.zeros(len(y),dtype=int)
                comparisons=[('mert','musicnn'),('mert','current_exposed')]
                pairs = (protocol.paired(y,scores,split,groups,comparisons,1000) if mode=='oof'
                         else holdout_pairs(y,scores,groups,comparisons,1000))
                key=mode+'__'+source+'__'+vibe
                tables[key]={'source':source,'vibe':vibe,'mode':mode,'rows':len(y),'components':len(set(groups)),
                    'primary':primary(source,vibe,mode),'metrics':{r:protocol.metrics(y,v,split) for r,v in scores.items()},
                    'paired':pairs,'current_control_exposure':'Potentially fitted on these identities; diagnostic only.'}
        print(json.dumps({'encoder_evaluated':source}),flush=True)
    decisions={vibe:decision(vibe,tables) for vibe in models.VIBES}
    extraction=load(directory/'extraction.json')['resources']
    fit=load(directory/'fit-trace.json')['resources']
    resource_failures=[]
    if extraction['total_seconds']>7200 or fit['seconds']+time.monotonic()-started>3600:resource_failures.append('Time bound exceeded.')
    if max(extraction['largest_child_peak_rss_bytes'],fit['python_peak_rss_bytes'])>4*1024**3:resource_failures.append('Per-process RSS bound exceeded.')
    if extraction['feature_npz_bytes']+extraction['musicnn_cache_bytes']+extraction['mert_cache_bytes']>256*1024**2:resource_failures.append('Feature-storage bound exceeded.')
    if extraction['private_environment_bytes']>4*1024**3:resource_failures.append('Private environment storage bound exceeded.')
    for d in decisions.values():
        if resource_failures:d['recommendation']='retain';d['reasons'].extend(resource_failures)
    report={'created_at':stamp(),'decision':decisions,'tables':tables,'native':native,'unsupported':unsupported,
        'resources':{'extraction':extraction,'fit':fit,'evaluation_seconds':time.monotonic()-started,'resource_failures':resource_failures,
            'musicnn_graph_bytes':GRAPH.stat().st_size,'mert_weights_bytes':(MODEL/'pytorch_model.bin').stat().st_size},
        'scope':{'selected':len(load(directory/'inputs/selection.json')['rows']),'retained':len(rows),
            'components':len({r['component'] for r in rows}),'corpora':dict(Counter(r['source'] for r in rows)),
            'new_memo_confirmation_records_used':0},
        'deployment_assessment':'Measured CPU float32 extraction and storage only. No browser MERT export, WebGPU/WASM execution, capture-resampling parity or low-memory client measurement. Existing 200-value production heads cannot consume 768-value features; no replacement artifact or browser feasibility claim.',
        'limitations':['Previously inspected populations; target-independent bounded subsets, not fresh confirmation.',
            'Small per-source cohorts make conditional component intervals wide; native and source-omitted ranking use different evaluation units.',
            'Native error, fixed-reference ranking/lift and calibration are separate; tag/activity/emotion/VA constructs do not establish listening benefit.',
            'Unknown artist identities/aliases and upstream encoder pretraining overlap remain unresolved.',
            'Official MERT card: 20k pretraining hours; weights CC-BY-NC-4.0, implementation Apache-2.0. Exact recording overlap unknown.',
            'Exact current specialists may have trained on these rows. Their scores are exposed diagnostics; matched nested heads identify the controlled encoder comparison.']}
    save(directory/'report.json',report);evidence.seal(directory,'evaluation',[directory/'report.json'])
    print(json.dumps({'decision':{v:d['recommendation'] for v,d in decisions.items()},'cases':len(tables),'resource_failures':resource_failures}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['freeze','extract','reuse','fit','evaluate','run','verify'])
    parser.add_argument('--directory',type=Path,default=DEFAULT)
    parser.add_argument('--from-directory',type=Path)
    args=parser.parse_args();directory=inside(args.directory)
    with threadpool_limits(limits=1):
        source=inside(args.from_directory) if args.from_directory else None
        if args.action=='freeze':freeze(directory,source);return
        evidence.validate(directory,args.action!='verify')
        if args.action=='reuse':
            if source is None:parser.error('reuse requires --from-directory')
            reuse(directory,source);return
        for action,function in [('extract',extract),('fit',fit),('evaluate',evaluate)]:
            if args.action in (action,'run'):function(directory)
        if args.action=='verify':print(json.dumps(evidence.verify(directory)))


if __name__=='__main__':main()
