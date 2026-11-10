"""Frozen-model external-label evaluation under FFmpeg and browser preparation."""
import argparse
import base64
from contextlib import contextmanager
import json
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time

import numpy as np
from threadpoolctl import threadpool_limits

from yt_playlist.rec import audio_vibe_cruise
from .audio_evaluation import check, hardware, origin, pinned, waveform
from ..studies.candidate_followup import DEFAULT as INPUTS, PILOTS, average_chunks
from ..cruise.cruise_baselines import DEFAULT as BASELINE, interval
from ..studies.external_checkpoint import RUN
from ..support.external_experiment import LAB, ROOT, digest, pcm, save, stamp
from ..evaluation.mapped_vibe_metrics import summary
from ..studies.mapped_vibe_rules import VIBES, apply, registry
from .preprocessing_native import embedding_vectors
from ..evaluation.product_baseline_audit import predict
from ..studies.vibe_specialist_round import input_arrays

DEFAULT = LAB / 'local/preprocessing-quality-20261029/run-1'
COHORTS = (*PILOTS, 'soundtracks-all', 'cal500-raw-activity')
POLICIES = ('stored', 'ffmpeg', 'browser')
ERROR_LIMIT = 5e-4
CODE = ('experiments/audio_lab_tools/audio/preprocessing_quality.py',
        *(str(path.relative_to(ROOT)) for path in sorted((ROOT / 'experiments/audio_lab_tools').rglob('__init__.py'))),
        'experiments/audio_lab_tools/audio/preprocessing_native.py',
        'experiments/audio_lab_tools/audio/test_preprocessing_quality.py',
        'experiments/audio_lab_tools/audio/audio_evaluation.py',
        'experiments/audio_lab_tools/support/external_experiment.py',
        'experiments/audio_lab_tools/evaluation/mapped_vibe_metrics.py',
        'experiments/audio_lab_tools/studies/mapped_vibe_rules.py',
        'experiments/audio_lab_tools/studies/mapped_vibe_inference.py',
        'experiments/audio_lab_tools/evaluation/product_baseline_audit.py',
        'experiments/audio_lab_tools/cruise/cruise_baselines.py',
        'experiments/audio_lab_tools/studies/candidate_followup.py',
        'src/yt_playlist/rec/audio_vibe_cruise.py',
        'src/yt_playlist/rec/audio_vibe_external.py',
        'src/yt_playlist/core/audio_embedding.py')


def load(path):
    return json.loads(Path(path).read_text())


def offsets_for(starts):
    if not starts or any(not np.isfinite(s) or s < 0 for s in starts):
        raise ValueError('Missing or invalid excerpt positions')
    return [float(f'{s:.6f}') for s in starts]


def bind_rows(metadata, original, arrays, cohort):
    """Join by verified identity/hash; preserve the exact frozen row/chunk order."""
    by_id = {r['id']: r for r in original}
    if len(by_id) != len(original):
        raise ValueError('Duplicate source identities')
    rows = []
    expected_offsets = [0]
    for index, row in enumerate(metadata['rows']):
        source = by_id[row['id']]
        if source['audio_sha256'] != row['audio_sha256']:
            raise ValueError('Source audio identity differs from frozen evaluation')
        if cohort in PILOTS:
            starts = row['chunk_starts']
            begin, end = row['covered_interval']
            if (not np.isclose(starts[0], begin, rtol=0, atol=1e-8)
                    or not np.isclose(starts[-1] + 10, end, rtol=0, atol=1e-8)
                    or any(not np.isclose(b - a, 10, rtol=0, atol=1e-8)
                           for a, b in zip(starts, starts[1:]))):
                raise ValueError('Incomplete consecutive annotated interval')
        else:
            if row['sample_seconds'] != 10 or source['sample_start'] != row['sample_start']:
                raise ValueError('Historical excerpt position changed')
            starts = [row['sample_start']]
        rows.append({'cohort': cohort, 'index': index, 'id': row['id'],
            'group': metadata['groups'][index], 'audio': source['audio'],
            'audio_sha256': source['audio_sha256'], 'starts': offsets_for(starts),
            'original_starts': starts, 'annotation_unit': row['annotation_unit'],
            'complete_annotated_interval': cohort in PILOTS,
            'sentinel_chunk': len(starts) // 2 if index in {0, len(metadata['rows']) // 2, len(metadata['rows']) - 1} else None})
        expected_offsets.append(expected_offsets[-1] + len(starts))
    declared = arrays['offsets'].tolist() if cohort in PILOTS else list(range(len(rows) + 1))
    if expected_offsets != declared or arrays['embedding'].shape != (declared[-1], 200):
        raise ValueError('Frozen chunk/row offsets differ')
    if len(rows) != len(arrays['y']) or len(rows) != len(arrays['outer']):
        raise ValueError('Frozen annotation/fold rows differ')
    return rows


def freeze(directory):
    if directory.exists():
        raise ValueError('Use a new directory; preserve frozen evidence')
    references, cases = [pinned(INPUTS / 'plan.json'), pinned(BASELINE / 'evaluation.receipt.json')], []
    previous = load(INPUTS / 'plan.json')
    baseline_receipt = load(BASELINE / 'evaluation.receipt.json')
    check([{'path': str(BASELINE / name), 'sha256': value}
           for name, value in baseline_receipt['outputs'].items()])
    raw = {}
    for cohort in COHORTS:
        for suffix in ('json', 'npz'):
            relative = f'inputs/{cohort}.{suffix}'
            if digest(INPUTS / relative) != previous['inputs'][relative]:
                raise ValueError('Historical frozen input changed: ' + relative)
        metadata = load(INPUTS / 'inputs' / (cohort + '.json'))
        arrays = input_arrays(INPUTS / 'inputs' / (cohort + '.npz'))
        original_path = (RUN / 'new-corpora/features.json' if cohort in PILOTS
                         else RUN / 'inputs/external' / (cohort + '.features.json'))
        if original_path not in raw:
            raw[original_path] = load(original_path)['rows']
            references.append(pinned(original_path))
        cases.extend(bind_rows(metadata, raw[original_path], arrays, cohort))
    directory.mkdir(parents=True)
    for cohort in COHORTS:
        for suffix in ('json', 'npz'):
            source = INPUTS / 'inputs' / f'{cohort}.{suffix}'
            target = directory / 'inputs' / source.name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            references.append(pinned(source))
    for name in ('external-predictions.npz', 'evaluation.report.json'):
        source = BASELINE / name
        shutil.copyfile(source, directory / 'inputs' / name)
        references.append(pinned(source))
    for source_name, target_name in (('emotify-vibes.json', 'shared.json'), ('cruise-vibes.json', 'cruise.json')):
        source = ROOT / 'src/yt_playlist/rec/data' / source_name
        if digest(source) != digest(BASELINE / ('checkpoint.json' if target_name == 'shared.json' else 'cruise-specialist.json')):
            raise ValueError('Current production models differ from historical comparison')
        shutil.copyfile(source, directory / target_name)
        references.append(pinned(source))
    model = ROOT / 'extension/audio_model'
    shutil.copytree(model, directory / 'snapshot/audio_model')
    shutil.copyfile(ROOT / 'extension/audio_capture.js', directory / 'snapshot/audio_capture.js')
    (directory / 'snapshot/index.html').write_text('<!doctype html><title>Audio preparation comparison</title>')
    for source in model.rglob('*'):
        if source.is_file() and digest(source) != digest(directory / 'snapshot/audio_model' / source.relative_to(model)):
            raise ValueError('Production extractor changed during snapshot')
    graph = RUN / 'reference-pipeline/musicnn.pb'
    shutil.copyfile(graph, directory / 'snapshot/musicnn.pb')
    references.append(pinned(graph))
    for name in CODE:
        target = directory / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
        references.append(pinned(ROOT / name))
    for case in cases:
        if digest(case['audio']) != case['audio_sha256']:
            raise ValueError('Supplied source audio changed: ' + case['id'])
    inputs = [pinned(p) for child in ('inputs', 'snapshot', 'source') for p in (directory / child).rglob('*') if p.is_file()]
    inputs += [pinned(directory / name) for name in ('shared.json', 'cruise.json')]
    plan = {'created_at': stamp(), 'cases': cases, 'references': references, 'inputs': inputs,
        'cohorts': list(COHORTS), 'policies': list(POLICIES), 'rules': registry(),
        'embedding_error_limit': ERROR_LIMIT, 'bootstrap_draws': 1000, 'bootstrap_seed': 424530,
        'hardware': hardware(), 'ffmpeg': subprocess.check_output(['ffmpeg', '-version'], text=True).splitlines()[0],
        'scope': 'Fixed production models; existing external human annotations, folds, identity exclusions and mappings. No fitting, decoder selection, local labels, live database or production changes.',
        'browser_policy': 'Decode original supplied file once to 44.1 kHz via OfflineAudioContext; render each original ten-second interval separately to 16 kHz mono with default Web Audio speaker downmix. No independent FFmpeg decode before browser input; no padding/gain/time fitting.',
        'ffmpeg_policy': 'Historical ffmpeg -ss (six decimal places), ten seconds, -ar 16000 -ac 1; same native encoder used for both refreshed policies.',
        'metrics': 'Existing fold-mean tie-aware graded NDCG/top lift/Spearman/MSE. Paired identity-group bootstrap within fixed folds, conditional on fixed models and prior inspection. No equivalence margin or automatic promotion.',
        'limits': ['All five production-baseline cohorts, not all research datasets or all publisher audio.',
            'ManyMusic/Memo retain complete annotated intervals; Soundtracks/CAL500 retain historical ten-second excerpts with excerpt/whole-song labels.',
            'Source-file browser preparation exercises production decode/render operations, not the HTMLMediaElement/MediaRecorder capture path; earlier actual-capture parity remains separate evidence.',
            'Previously inspected cohorts; known identity exclusions cannot resolve missing artists or upstream pretraining overlap.',
            'Native encoder acceleration requires production-browser sentinel agreement; same preparation does not establish unseen-site codec or hardware equivalence.']}
    save(directory / 'plan.json', plan)
    save(directory / 'plan.sha256.json', {'sha256': digest(directory / 'plan.json')})
    print(json.dumps({'plan': str(directory / 'plan.json'), 'recordings': len(cases),
        'chunks': sum(len(c['starts']) for c in cases), 'sentinels': sum(c['sentinel_chunk'] is not None for c in cases)}), flush=True)


def validate(directory, *, audio=False):
    plan = load(directory / 'plan.json')
    if digest(directory / 'plan.json') != load(directory / 'plan.sha256.json')['sha256']:
        raise ValueError('Frozen plan changed')
    if plan['embedding_error_limit'] != ERROR_LIMIT or plan['rules'] != registry():
        raise ValueError('Frozen comparison contract changed')
    check(plan['inputs'] + plan['references'])
    if audio:
        check([{'path': c['audio'], 'sha256': c['audio_sha256']} for c in plan['cases']])
    return plan


BROWSER = """async request => {
 if(request.action==='load') {
   const bytes=await fetch(request.url).then(r=>{if(!r.ok)throw Error('Missing source');return r.arrayBuffer();});
   window.studyAudio=await new OfflineAudioContext(2,1,44100).decodeAudioData(bytes);
   if(studyAudio.numberOfChannels>2)throw Error('Unsupported channel layout');
   return {rate:studyAudio.sampleRate,channels:studyAudio.numberOfChannels,length:studyAudio.length};
 }
 if(request.action==='release') {
   if(window.studyAudio)for(let c=0;c<studyAudio.numberOfChannels;c++)studyAudio.getChannelData(c).fill(0);
   window.studyAudio=null; return true;
 }
 const decoded=window.studyAudio,begin=request.start*44100;
 if(!decoded||!Number.isFinite(begin)||begin<0||begin+441000>decoded.length)throw Error('Incomplete real ten-second interval');
 const context=new OfflineAudioContext(1,160000,16000),source=context.createBufferSource();
 source.buffer=decoded;source.connect(context.destination);source.start(0,request.start);
 const rendered=await context.startRendering(),samples=rendered.getChannelData(0).slice();
 if(samples.length!==160000||!samples.every(Number.isFinite))throw Error('Invalid model PCM');
 let encoded='';const bytes=new Uint8Array(samples.buffer);
 for(let i=0;i<bytes.length;i+=8192)encoded+=String.fromCharCode(...bytes.subarray(i,i+8192));
 let inference=null;
 if(request.sentinel) {
   const worker=new Worker('/snapshot/audio_model/model_worker.js');
   try {inference=await new Promise((resolve,reject)=>{
     const timer=setTimeout(()=>reject(Error('Production worker timeout')),30000);
     worker.onerror=e=>{clearTimeout(timer);reject(Error(e.message));};
     worker.onmessage=({data})=>{clearTimeout(timer);data.error?reject(Error(data.error)):resolve(data);};
     const first=Math.round(begin),left=decoded.getChannelData(0).slice(first,first+441000);
     const right=decoded.numberOfChannels===2?decoded.getChannelData(1).slice(first,first+441000):null;
     worker.postMessage({id:request.id,rate:16000,samples,measurement_rate:44100,
       measurement_left:left,measurement_right:right},[samples.buffer,left.buffer,...(right?[right.buffer]:[])]);
   });}finally{worker.terminate();}
 }
 rendered.getChannelData(0).fill(0);if(samples.byteLength)samples.fill(0);
 return {pcm:btoa(encoded),inference};
}"""


def response(worker, prefix, timeout=120):
    # Worker emits only one protocol line per request; all native diagnostics go to stderr.
    if not select.select([worker.stdout], [], [], timeout)[0]:
        raise TimeoutError('Native worker response deadline exceeded')
    line = worker.stdout.readline()
    if not line.startswith(prefix):
        raise ValueError('Native worker exited or returned an invalid response: ' + line[:200])
    return json.loads(line[len(prefix):])


@contextmanager
def native_worker(directory, lifetime):
    import os
    environment = {**os.environ, 'TF_NUM_INTRAOP_THREADS': '1', 'TF_NUM_INTEROP_THREADS': '1',
        'OMP_NUM_THREADS': '1', 'TF_CPP_MIN_LOG_LEVEL': '2'}
    with (directory / 'native.log').open('a') as log:
        worker = subprocess.Popen([sys.executable, '-B', '-m', 'experiments.audio_lab_tools.audio.preprocessing_native',
            '--graph', str(directory / 'snapshot/musicnn.pb')], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, text=True, bufsize=1, env=environment,
            pass_fds=() if lifetime is None else (lifetime,))
        try:
            runtime = response(worker, 'READY ')
            yield worker, runtime
        finally:
            worker.stdin.close()
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.terminate()
                worker.wait(timeout=10)


def infer(worker, path, identifier):
    worker.stdin.write(json.dumps({'id': identifier, 'pcm': str(path)}) + '\n')
    worker.stdin.flush()
    result = response(worker, 'RESULT ')
    if result['id'] != identifier or result['patches'] != 3:
        raise ValueError('Native worker returned a different input or patch policy')
    embedding_vectors(result['embedding'])
    return result


def case_path(directory, case):
    return directory / 'cases' / case['cohort'] / f"{case['index']:04d}"


def checked_case(directory, case):
    target = case_path(directory, case)
    receipt = load(target / 'receipt.json')
    if receipt['plan_sha256'] != digest(directory / 'plan.json'):
        raise ValueError('Result belongs to another plan')
    check([{'path': str(target / name), 'sha256': value} for name, value in receipt['outputs'].items()])
    result = load(target / 'report.json')
    if result['case'] != case or len(result['chunks']) != len(case['starts']):
        raise ValueError('Missing or reordered excerpts in recording result')
    for index, chunk in enumerate(result['chunks']):
        if chunk['start'] != case['starts'][index]:
            raise ValueError('Excerpt position changed')
        for policy in ('ffmpeg', 'browser'):
            embedding_vectors(chunk[policy]['embedding'])
        sentinel = chunk.get('production_sentinel')
        if index == case['sentinel_chunk']:
            if (sentinel is None or not np.isfinite(sentinel['maximum_absolute_error'])
                    or not 0 <= sentinel['maximum_absolute_error'] <= ERROR_LIMIT):
                raise ValueError('Required production-browser sentinel is absent or failed')
        elif sentinel is not None:
            raise ValueError('Undeclared production-browser sentinel')
    return result


def extract(directory, limit, lifetime):
    from playwright.sync_api import sync_playwright
    plan = validate(directory)
    completed = 0
    for case in plan['cases']:
        if (case_path(directory, case) / 'receipt.json').exists():
            checked_case(directory, case)
            completed += 1
    pending = [c for c in plan['cases'] if not (case_path(directory, c) / 'receipt.json').exists()][:limit]
    network, started = [], time.monotonic()
    with origin(directory) as base, native_worker(directory, lifetime) as (worker, runtime), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--mute-audio'])
        context = browser.new_context()
        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/')
                      else (network.append(route.request.url), route.abort()))
        page = context.new_page()
        page.goto(base + '/snapshot/index.html')
        for case in pending:
            target = case_path(directory, case)
            if target.exists():
                raise ValueError('Incomplete attempt retained; inspect before retrying: ' + str(target))
            if digest(case['audio']) != case['audio_sha256']:
                raise ValueError('Supplied audio changed: ' + case['id'])
            target.mkdir(parents=True)
            # Symlink is local, points to the pinned input and is never a public package asset.
            source = target / 'source.audio'
            source.symlink_to(case['audio'])
            result = {'case': case, 'chunks': [], 'native_runtime': runtime, 'chromium': browser.version}
            try:
                result['decoded'] = page.evaluate(BROWSER, {'action': 'load',
                    'url': base + '/' + str(source.relative_to(directory))})
                for index, start in enumerate(case['starts']):
                    row = {'start': start, 'index': index}
                    data = page.evaluate(BROWSER, {'action': 'chunk', 'start': start,
                        'id': case['id'] + ':' + str(index), 'sentinel': index == case['sentinel_chunk']})
                    for policy in ('browser', 'ffmpeg'):
                        path = target / f'{index}-{policy}.f32'
                        if policy == 'browser':
                            path.write_bytes(base64.b64decode(data['pcm'], validate=True))
                        else:
                            pcm(case['audio'], start, 16000, 1, path, lifetime_fd=lifetime)
                        signal = np.fromfile(path, dtype='<f4')
                        if signal.shape != (160000,) or not np.isfinite(signal).all():
                            raise ValueError('Missing or invalid real audio samples')
                        row[policy] = {**infer(worker, path, case['id'] + ':' + str(index)),
                                       'pcm_sha256': digest(path)}
                    row['waveform'] = waveform(np.fromfile(target / f'{index}-browser.f32', dtype='<f4'),
                                               np.fromfile(target / f'{index}-ffmpeg.f32', dtype='<f4'))
                    if data['inference'] is not None:
                        prediction = embedding_vectors(data['inference']['prediction']['embedding']['values'])
                        delta = float(np.max(np.abs(prediction - row['browser']['embedding'])))
                        row['production_sentinel'] = {'maximum_absolute_error': delta,
                            'pass': bool(delta <= ERROR_LIMIT), 'result': data['inference']}
                        if not row['production_sentinel']['pass']:
                            raise ValueError('Accelerated encoder does not match production browser')
                    result['chunks'].append(row)
                if network:
                    raise ValueError('Browser attempted external network access')
                save(target / 'report.json', result)
                save(target / 'receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
                    'outputs': {'report.json': digest(target / 'report.json')}})
                # Retain PCM hashes, not another multi-gigabyte raw-audio corpus.
                for path in target.glob('*.f32'):
                    path.unlink()
            except BaseException as error:
                save(target / 'failure.json', {'error': repr(error), 'case': case})
                raise
            finally:
                page.evaluate(BROWSER, {'action': 'release'})
                source.unlink()
            completed += 1
            print(json.dumps({'cohort': case['cohort'], 'completed': completed, 'total': len(plan['cases']),
                'id': case['id'], 'chunks': len(case['starts']), 'elapsed_seconds': round(time.monotonic() - started, 1)}), flush=True)
        browser.close()
    validate(directory)


def predictions(shared, cruise, embedding, offsets):
    values = predict(shared, average_chunks(embedding, offsets))
    individual = np.asarray([audio_vibe_cruise.score(cruise, embedding[a:b].tolist())['score']
                             for a, b in zip(offsets[:-1], offsets[1:])])
    return values, individual


def evaluate(directory):
    plan = validate(directory)
    results = [checked_case(directory, case) for case in plan['cases']]
    shared, cruise = load(directory / 'shared.json'), audio_vibe_cruise.load(directory / 'cruise.json')
    old = input_arrays(directory / 'inputs/external-predictions.npz')
    historical = load(directory / 'inputs/evaluation.report.json')
    report, outputs = {}, {}
    for cohort in COHORTS:
        cases = [r for r in results if r['case']['cohort'] == cohort]
        meta = load(directory / 'inputs' / (cohort + '.json'))
        data = input_arrays(directory / 'inputs' / (cohort + '.npz'))
        offsets = data['offsets'] if cohort in PILOTS else np.arange(len(cases) + 1)
        embeddings = {'stored': data['embedding'], **{policy: np.asarray([chunk[policy]['embedding']
            for row in cases for chunk in row['chunks']]) for policy in ('ffmpeg', 'browser')}}
        cache_delta = float(np.max(np.abs(embeddings['stored'] - embeddings['ffmpeg'])))
        if cache_delta > ERROR_LIMIT:
            raise ValueError('Refreshed FFmpeg/reference encoder differs from historical cache: ' + cohort)
        scores = {policy: predictions(shared, cruise, values, offsets) for policy, values in embeddings.items()}
        keep = old[cohort + '__retained']
        if keep.dtype != bool or len(keep) != len(cases) or keep.sum() != historical['cases'][cohort]['shared_training_disjoint_rows']:
            raise ValueError('Historical identity exclusion mask differs')
        split, groups = data['outer'][keep], np.asarray(meta['groups'])[keep]
        observed_rules = plan['rules']['pmemo' if cohort in PILOTS else cohort]
        cohort_report = {'rows': len(cases), 'retained': int(keep.sum()), 'chunks': len(embeddings['stored']),
            'stored_vs_refreshed_embedding_maximum_error': cache_delta, 'vibes': {},
            'sentinels': [c['production_sentinel']['maximum_absolute_error'] for row in cases
                          for c in row['chunks'] if 'production_sentinel' in c]}
        for vibe in VIBES:
            if vibe not in observed_rules:
                continue
            y = apply(observed_rules[vibe]['expression'], data['y'], meta['target_names'])
            values = {}
            for policy, (group_scores, cruise_scores) in scores.items():
                values[policy + '_shared'] = group_scores[:, shared['labels'].index(vibe)]
                if vibe == 'cruise':
                    values[policy + '_cruise'] = cruise_scores
            metrics = {name: summary(y[keep], value[keep], split) for name, value in values.items()}
            expected = historical['cases'][cohort]['vibes'][vibe]['metrics']['checkpoint']
            for metric in ('ndcg', 'mse'):
                if not np.isclose(metrics['stored_shared'][metric], expected[metric], atol=1e-12, rtol=0):
                    raise ValueError('Historical quality baseline does not reproduce')
            active = 'cruise' if vibe == 'cruise' else 'shared'
            contrasts = {'browser_vs_stored_active': ('stored_' + active, 'browser_' + active)}
            if vibe == 'cruise':
                contrasts.update({policy + '_cruise_vs_shared': (policy + '_shared', policy + '_cruise')
                                  for policy in ('stored', 'browser')})
            paired = {name: interval(y[keep], values[before][keep], values[after][keep], split, groups,
                      plan['bootstrap_draws'], plan['bootstrap_seed']) for name, (before, after) in contrasts.items()}
            delta = values['browser_' + active][keep] - values['stored_' + active][keep]
            cohort_report['vibes'][vibe] = {'semantics': observed_rules[vibe]['semantics'],
                'metrics': metrics, 'paired': paired, 'score_difference': {
                    'mean_absolute': float(np.mean(np.abs(delta))), 'maximum_absolute': float(np.max(np.abs(delta))),
                    'rows_over_0_05': int((np.abs(delta) > .05).sum())}}
            outputs[cohort + '__' + vibe + '__observed'] = y
            for name, value in values.items():
                outputs[cohort + '__' + vibe + '__' + name] = value
        outputs[cohort + '__retained'] = keep
        report[cohort] = cohort_report
        print(json.dumps({'evaluated': cohort, 'rows': len(cases), 'retained': int(keep.sum()),
            'cache_maximum_error': cache_delta, 'vibes': list(cohort_report['vibes'])}), flush=True)
    validate(directory)
    save(directory / 'quality.report.json', {'plan_sha256': digest(directory / 'plan.json'),
        'created_at': stamp(), 'cases': report, 'limits': plan['limits']})
    np.savez_compressed(directory / 'predictions.npz', **outputs)
    save(directory / 'evaluation.receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
        'outputs': {name: digest(directory / name) for name in ('quality.report.json', 'predictions.npz')}})


def status(directory):
    plan = validate(directory)
    completed, failures = [], []
    for case in plan['cases']:
        target = case_path(directory, case)
        if (target / 'receipt.json').exists():
            checked_case(directory, case)
            completed.append(case)
        elif target.exists():
            failures.append({'id': case['id'], 'path': str(target)})
    if (directory / 'evaluation.receipt.json').exists():
        receipt = load(directory / 'evaluation.receipt.json')
        if receipt['plan_sha256'] != digest(directory / 'plan.json'):
            raise ValueError('Evaluation belongs to another plan')
        check([{'path': str(directory / name), 'sha256': value} for name, value in receipt['outputs'].items()])
    print(json.dumps({'plan_sha256': digest(directory / 'plan.json'), 'complete_recordings': len(completed),
        'planned_recordings': len(plan['cases']), 'complete_chunks': sum(len(c['starts']) for c in completed),
        'planned_chunks': sum(len(c['starts']) for c in plan['cases']), 'failed_attempts': failures,
        'quality_evaluated': (directory / 'evaluation.receipt.json').exists()}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'extract', 'evaluate', 'status'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--limit', type=int, default=2000)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(LAB / 'local') or args.limit < 1:
        parser.error('Use an experiment-local directory and positive limit')
    if args.action == 'freeze':
        freeze(directory)
    elif args.action == 'status':
        status(directory)
    else:
        lifetime = None
        with threadpool_limits(limits=1):
            if args.action == 'extract':
                extract(directory, args.limit, lifetime)
            else:
                evaluate(directory)


if __name__ == '__main__':
    main()
