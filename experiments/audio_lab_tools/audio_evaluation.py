"""Pinned #424 browser/reference evaluation; no fitting or live database access."""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from threading import Event, Thread
import time

import numpy as np
from threadpoolctl import threadpool_limits

from .browser_capture import CAPTURE_FILES, browser_sample
from .external_checkpoint import RUN
from .external_experiment import LAB, ROOT, digest, save, stamp
from .reference_pipeline import WORK as NATIVE_WORK, maximum_delta, process_sample

DEFAULT = LAB / 'local/evaluation-424-20261029/run-1'
MODEL = ROOT / 'extension/audio_model'
CAPTURE = RUN / 'browser-capture/2026-10-29T112815.236036_0000/report.json'
CODE = ('experiments/audio_lab_tools/audio_evaluation.py',
        'experiments/audio_lab_tools/browser_capture.py',
        'experiments/audio_lab_tools/reference_pipeline.py',
        'src/yt_playlist/rec/audio_vibe_cruise.py')
BUDGETS = {'embedding_absolute_error': 5e-4, 'tag_absolute_error': 1e-4,
           'mood_absolute_error': 2e-4, 'worker_wall_seconds': 25.,
           'capture_total_wall_seconds': 45., 'browser_cpu_seconds': 25.,
           'browser_peak_pss_bytes': 768 * 1024 * 1024}


def load(path):
    return json.loads(Path(path).read_text())


def pinned(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': digest(path)}


def check(records):
    for row in records:
        if digest(row['path']) != row['sha256']:
            raise ValueError('Pinned input changed: ' + row['path'])


def unique(rows, count):
    result, seen = [], set()
    for row in sorted(rows, key=lambda r: hashlib.sha256(r['id'].encode()).hexdigest()):
        key = row.get('recording_id', row['id'])
        if key not in seen:
            seen.add(key)
            result.append(row)
            if len(result) == count:
                return result
    raise ValueError('Insufficient distinct supplied recordings')


def freeze(directory):
    if directory.exists():
        raise ValueError('Use a new evaluation directory; never overwrite a frozen plan')
    sources = [RUN / 'inputs/external/emotify-artist.features.json', RUN / 'new-corpora/features.json']
    emotify, pilots = (load(path)['rows'] for path in sources)
    wanted = {'emotify:' + str(i) for i in (1, 2, 101, 102, 201, 202, 301, 302)}
    selected = [row for row in emotify if row['id'] in wanted]
    if {row['id'] for row in selected} != wanted:
        raise ValueError('Fixed Emotify IDs are unavailable')
    for subset, count in (('manymusic-natural', 4), ('manymusic-generated', 4), ('memo2496-pilot', 8)):
        selected += unique([r for r in pilots if r.get('subset') == subset], count)
    cases = []
    for row in selected:
        audio = Path(row['audio'])
        if digest(audio) != row['audio_sha256']:
            raise ValueError('Supplied audio changed: ' + str(audio))
        starts = [float(row['sample_start'])] if 'sample_start' in row else [float(row['chunk_starts'][len(row['chunk_starts']) // 2])]
        if row['id'] in {'emotify:1', 'emotify:101', 'emotify:201', 'emotify:301'}:
            starts = [0., float(row['sample_start']), float(row['recording_duration']) - 10.05]
        for position, start in enumerate(starts):
            cases.append({'index': len(cases), 'id': row['id'], 'recording_id': row.get('recording_id', row['id']),
                'group': row['group'], 'subset': row.get('subset', 'emotify'),
                'audio': str(audio), 'audio_sha256': row['audio_sha256'], 'start': start,
                'ordered_excerpt': position, 'excerpt_count': len(starts), 'model_seconds': 10.,
                'decode_container_seconds': 10.05,
                'annotation_unit': row['annotation_unit'], 'source_targets': row['targets'],
                'target_names': row['target_names'], 'license_url': row.get('license_url'),
                'artist': row.get('artist', ''), 'title': row.get('title', '')})
    directory.mkdir(parents=True)
    snapshot = directory / 'snapshot'
    shutil.copytree(MODEL, snapshot / 'audio_model')
    for name in CAPTURE_FILES:
        shutil.copyfile(ROOT / 'extension' / name, snapshot / name)
    # The real key makes the controlled player's extension world match production.
    shutil.copyfile(ROOT / 'extension/manifest.json', snapshot / 'original_manifest.json')
    native = load(NATIVE_WORK / 'acquisition.json')['files']
    dependencies = [pinned(ROOT / name) for name in CODE]
    dependencies += [pinned(path) for path in sources]
    dependencies += [pinned(NATIVE_WORK / name) for name in native]
    dependencies += [pinned(ROOT / 'src/yt_playlist/rec/data' / name)
                     for name in ('emotify-vibes.json', 'cruise-vibes.json')]
    capture = load(CAPTURE)
    dependencies += [pinned(CAPTURE)]
    for row in capture['cases']:
        dependencies += [pinned(row['browser']['container']), pinned(row['browser']['model_pcm'])]
    snapshots = [pinned(path) for path in sorted(snapshot.rglob('*')) if path.is_file()]
    plan = {'schema': 1, 'created_at': stamp(), 'cases': cases, 'budgets': BUDGETS,
        'inputs': dependencies, 'snapshot': snapshots, 'capture_report': str(CAPTURE),
        'selection': 'Fixed two IDs per Emotify genre; SHA256-ID order, unique recordings, four natural/four generated ManyMusic and eight Memo. No target-score selection. Four fixed Emotify tracks retain early/center/late results separately.',
        'scope': 'Numerical alignment and resource evaluation, no training, no local-label scoring, no live database, no network. Existing human-reference accuracy reports remain separate.',
        'hardware': hardware(), 'production_source': {str(p.relative_to(ROOT)): digest(p)
            for p in sorted(MODEL.rglob('*')) if p.is_file()}}
    save(directory / 'plan.json', plan)
    save(directory / 'plan.sha256.json', {'sha256': digest(directory / 'plan.json')})
    print(json.dumps({'plan': str(directory / 'plan.json'), 'recordings': len(selected), 'cases': len(cases)}), flush=True)


def hardware():
    cpu = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                if line.startswith('model name')), platform.processor())
    supplies = [p.name for p in Path('/sys/class/power_supply').glob('*')
                if (p / 'type').exists() and (p / 'type').read_text().strip() == 'Battery']
    return {'cpu': cpu, 'platform': platform.platform(), 'python': platform.python_version(),
        'numpy': np.__version__, 'allowed_cpus': sorted(os.sched_getaffinity(0)),
        'battery_devices': supplies, 'battery_claim': 'No battery measurement; desktop has no battery' if not supplies else 'Battery present; no energy equivalence inferred from CPU time'}


def validate(directory):
    plan = load(directory / 'plan.json')
    if digest(directory / 'plan.json') != load(directory / 'plan.sha256.json')['sha256']:
        raise ValueError('Frozen evaluation plan changed')
    if plan['budgets'] != BUDGETS:
        raise ValueError('Budgets changed after freezing')
    check(plan['inputs'] + plan['snapshot'])
    return plan


@contextmanager
def origin(directory):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    class Files(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Files, directory=str(directory)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


DECODE = """async request => {
 const encoded=await fetch(request.url).then(r=>r.arrayBuffer());
 const pcm=await new OfflineAudioContext(2,1,44100).decodeAudioData(encoded);
 if(pcm.length<441000||pcm.numberOfChannels>2)throw Error('Incomplete decoded input: '+pcm.length+' samples/'+pcm.numberOfChannels+' channels');
 const context=new OfflineAudioContext(1,160000,16000),source=context.createBufferSource();
 source.buffer=pcm;source.connect(context.destination);source.start();
 const rendered=await context.startRendering(),samples=rendered.getChannelData(0).slice();
 const encode=values=>{const bytes=new Uint8Array(values.buffer,values.byteOffset,values.byteLength);
   let value='';for(let i=0;i<bytes.length;i+=8192)value+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(value);};
 const encodedPCM=encode(samples);
 const result={pcm:encodedPCM,rate:pcm.sampleRate,channels:pcm.numberOfChannels,decoded_length:pcm.length};
 if(request.infer){
   const worker=new Worker('/snapshot/audio_model/model_worker.js');
   try{result.inference=await new Promise((resolve,reject)=>{
     const timer=setTimeout(()=>reject(Error('Worker deadline exceeded')),30000);
     worker.onerror=e=>{clearTimeout(timer);reject(Error(e.message));};
     worker.onmessage=({data})=>{clearTimeout(timer);data.error?reject(Error(data.error)):resolve(data);};
     const left=pcm.getChannelData(0).slice(0,441000),right=pcm.numberOfChannels===2?pcm.getChannelData(1).slice(0,441000):null;
     worker.postMessage({id:request.id,rate:16000,samples,measurement_rate:44100,measurement_left:left,measurement_right:right},
       [samples.buffer,left.buffer,...(right?[right.buffer]:[])]);
   });}finally{worker.terminate();}
 }
 for(let i=0;i<pcm.numberOfChannels;i++)pcm.getChannelData(i).fill(0);
 rendered.getChannelData(0).fill(0);if(samples.byteLength)samples.fill(0);
 return result;
}"""


def decode_browser(page, url, identifier, infer):
    return page.evaluate(DECODE, {'url': url, 'id': identifier, 'infer': infer})


def native(path, directory, lifetime):
    output = directory / (path.stem + '.native.json')
    request = directory / 'native-request.json'
    save(request, {'id': directory.name, 'model_pcm': str(path), 'native_output': str(output)})
    env = {**os.environ, 'TF_NUM_INTRAOP_THREADS': '1', 'TF_NUM_INTEROP_THREADS': '1',
        'OMP_NUM_THREADS': '1', 'TF_CPP_MIN_LOG_LEVEL': '2'}
    with (directory / 'native.log').open('a') as stream:
        subprocess.run([sys.executable, '-B', '-m', 'experiments.audio_lab_tools.reference_pipeline',
            '--native', str(request)], check=True, stdout=stream, stderr=stream, env=env,
            timeout=120, pass_fds=() if lifetime is None else (lifetime,))
    return load(output)


def waveform(left, right):
    left, right = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    if left.shape != (160000,) or right.shape != left.shape or not np.isfinite([left, right]).all():
        raise ValueError('Expected two finite ten-second mono waveforms')
    residual = left - right
    power = np.abs(np.fft.rfft(residual)) ** 2
    high = np.fft.rfftfreq(len(residual), 1 / 16000) >= 6000
    return {'rms_delta': float(np.sqrt(np.mean(residual ** 2))),
        'maximum_delta': float(np.max(np.abs(residual))),
        'reference_peak': float(np.max(np.abs(left))), 'other_peak': float(np.max(np.abs(right))),
        'projected_amplitude_ratio': float(np.dot(left, right) / np.dot(left, left)) if np.dot(left, left) else None,
        'residual_power_above_6khz_fraction': float(power[high].sum() / max(power.sum(), 1e-30))}


def independent(container, output, policy, lifetime):
    filters = {'default': 'aresample=16000',
        'arithmetic': 'pan=mono|c0=0.5*c0+0.5*c1,aresample=16000',
        'browser_rates': 'aresample=44100,pan=mono|c0=0.5*c0+0.5*c1,aresample=16000'}
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1', '-i', str(container)]
    if policy == 'default':
        command += ['-ac', '1']
    subprocess.run([*command, '-af', filters[policy] + ',atrim=end_sample=160000',
        '-f', 'f32le', '-y', str(output)], check=True, timeout=45,
        pass_fds=() if lifetime is None else (lifetime,))
    result = np.fromfile(output, dtype='<f4')
    if result.shape != (160000,) or not np.isfinite(result).all():
        raise ValueError('Independent decoder did not supply ten finite seconds')
    return result


def scores(embedding):
    from yt_playlist.rec import audio_vibe_cruise as cruise
    model = cruise.load(str(ROOT / 'src/yt_playlist/rec/data/cruise-vibes.json'))
    return cruise.score(model, [list(map(float, embedding))])


def matched(reference, prediction):
    return {'embedding': maximum_delta(reference['embedding'], prediction['embedding']['values']),
        'tags': maximum_delta(reference['scores'], prediction['scores']),
        'moods': maximum_delta(reference['traits'], {k: v['score'] for k, v in prediction['moods']['traits'].items()})}


def numerical_pass(deltas):
    return (all(np.isfinite(value) and value >= 0 for value in deltas.values())
        and deltas['embedding'] <= BUDGETS['embedding_absolute_error']
        and deltas['tags'] <= BUDGETS['tag_absolute_error']
        and deltas['moods'] <= BUDGETS['mood_absolute_error'])


def receipt(directory, target, result):
    save(target / 'report.json', result)
    save(target / 'receipt.json', {'plan_sha256': digest(directory / 'plan.json'),
        'outputs': {str(p.relative_to(target)): digest(p) for p in sorted(target.rglob('*'))
                    if p.is_file() and p.name != 'receipt.json'}})


def compare(directory, limit, lifetime):
    from playwright.sync_api import sync_playwright
    plan = validate(directory)
    network = []
    with origin(directory) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--mute-audio'])
        context = browser.new_context()
        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/')
                      else (network.append(route.request.url), route.abort()))
        page = context.new_page()
        page.goto(base + '/snapshot/audio_preview_frame.html')
        session = browser.new_browser_cdp_session()
        roots = [row['id'] for row in session.send('SystemInfo.getProcessInfo')['processInfo'] if row['type'] == 'browser']
        remaining = [r for r in plan['cases'] if not (directory / 'comparison' / f"{r['index']:03d}" / 'receipt.json').exists()]
        for case in remaining[:limit]:
            target = directory / 'comparison' / f"{case['index']:03d}"
            if target.exists():
                raise ValueError('Failed attempt retained; inspect before using a new evaluation directory: ' + str(target))
            target.mkdir(parents=True)
            if digest(case['audio']) != case['audio_sha256']:
                raise ValueError('Supplied audio changed')
            wav = target / 'excerpt.wav'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1',
                '-ss', str(case['start']), '-i', case['audio'], '-t', str(case['decode_container_seconds']), '-ar', '48000', '-ac', '2',
                '-c:a', 'pcm_f32le', str(wav)], check=True, timeout=45,
                pass_fds=() if lifetime is None else (lifetime,))
            stopped, resources = Event(), [process_sample(roots)]
            def sample():
                while not stopped.wait(.1):
                    resources.append(process_sample(roots))
            sampler = Thread(target=sample, daemon=True)
            sampler.start()
            start = time.monotonic()
            try:
                data = decode_browser(page, base + '/' + str(wav.relative_to(directory)), case['id'], True)
            finally:
                stopped.set()
                sampler.join(timeout=5)
            elapsed = time.monotonic() - start
            resources.append(process_sample(roots))
            pcm = target / 'browser.f32'
            pcm.write_bytes(base64.b64decode(data.pop('pcm'), validate=True))
            prediction = data['inference']['prediction']
            reference = native(pcm, target, lifetime)
            deltas = matched(reference, prediction)
            sensitivity = {}
            actual = np.fromfile(pcm, dtype='<f4')
            for policy in ('default', 'arithmetic'):
                alternative = target / (policy + '.f32')
                signal = independent(wav, alternative, policy, lifetime)
                alternative_native = native(alternative, target, lifetime)
                sensitivity[policy] = {**waveform(actual, signal),
                    'embedding_maximum_delta': maximum_delta(reference['embedding'], alternative_native['embedding']),
                    'cruise_score': scores(alternative_native['embedding'])['score'],
                    'cruise_absolute_score_delta': abs(scores(reference['embedding'])['score'] - scores(alternative_native['embedding'])['score'])}
            resource = {'decode_and_worker_wall_seconds': elapsed,
                'worker_wall_seconds': data['inference']['wall_ms'] / 1000,
                'peak_browser_pss_bytes': max(r['pss_bytes'] for r in resources),
                'baseline_browser_pss_bytes': resources[0]['pss_bytes'],
                'browser_cpu_seconds': max(0., resources[-1]['cpu_seconds'] - resources[0]['cpu_seconds']),
                'resource_samples': len(resources)}
            result = {'case': case, 'chromium': browser.version, 'browser': data, 'resource': resource,
                'matched_pcm_native_deltas': deltas, 'browser_cruise': scores(prediction['embedding']['values']),
                'native_cruise': scores(reference['embedding']), 'independent_decoder_sensitivity': sensitivity,
                'numerical_pass': numerical_pass(deltas),
                'resource_pass': resource['worker_wall_seconds'] <= BUDGETS['worker_wall_seconds']
                    and resource['browser_cpu_seconds'] <= BUDGETS['browser_cpu_seconds']
                    and resource['peak_browser_pss_bytes'] <= BUDGETS['browser_peak_pss_bytes'],
                'external_network_requests': list(network)}
            if network:
                raise ValueError('External browser requests: ' + repr(network))
            validate(directory)
            receipt(directory, target, result)
            print(json.dumps({'index': case['index'], 'id': case['id'], 'deltas': deltas,
                'numerical_pass': result['numerical_pass'], 'resource_pass': result['resource_pass'],
                'resource': resource}), flush=True)
        browser.close()


def decode_retained(directory, lifetime):
    from playwright.sync_api import sync_playwright
    plan = validate(directory)
    target = directory / 'retained-decode'
    if target.exists():
        raise ValueError('Retained decode results already exist')
    target.mkdir()
    results = []
    with origin(directory) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--mute-audio'])
        page = browser.new_page()
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
        page.goto(base + '/snapshot/audio_preview_frame.html')
        for index, row in enumerate(load(plan['capture_report'])['cases']):
            work = target / str(index)
            work.mkdir()
            container = work / 'capture.webm'
            shutil.copyfile(row['browser']['container'], container)
            data = decode_browser(page, base + '/' + str(container.relative_to(directory)), row['id'], False)
            pcm = work / 'redecoded.f32'
            pcm.write_bytes(base64.b64decode(data.pop('pcm'), validate=True))
            signal = np.fromfile(pcm, dtype='<f4')
            original = np.fromfile(row['browser']['model_pcm'], dtype='<f4')
            methods = {policy: waveform(signal, independent(container, work / (policy + '.f32'), policy, lifetime))
                       for policy in ('default', 'arithmetic', 'browser_rates')}
            results.append({'id': row['id'], 'original_capture_redecode': waveform(original, signal),
                'methods': methods, 'browser': data})
        browser.close()
    validate(directory)
    receipt(directory, target, {'cases': results, 'policy': 'Replay exact retained PCM containers through current browser decode/rates; no fitting, shifting, gain fitting or padding.'})
    print(json.dumps({'retained_decode_report': str(target / 'report.json'), 'cases': results}), flush=True)


def prepare_current(directory, snapshot):
    destination = directory / 'extension'
    destination.mkdir()
    for name in CAPTURE_FILES:
        shutil.copyfile(snapshot / name, destination / name)
    shutil.copytree(snapshot / 'audio_model', destination / 'audio_model')
    save(destination / 'manifest.json', {'manifest_version': 3, 'name': 'TuneConsole #424 current package audit',
        'version': '1.0', 'key': load(snapshot / 'original_manifest.json')['key'], 'permissions': ['scripting'],
        'host_permissions': ['https://music.youtube.com/*'], 'background': {'service_worker': 'audit_background.js'},
        'content_security_policy': {'extension_pages': "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'"},
        'web_accessible_resources': [{'resources': ['audio_preview_frame.html'], 'matches': ['https://music.youtube.com/*']}],
        'content_scripts': [{'matches': ['https://music.youtube.com/*'], 'js': ['audio_preview_capture.js'], 'run_at': 'document_idle'}]})
    (destination / 'audit_background.js').write_text('chrome.runtime.onMessage.addListener(() => {});\n')
    return destination, {name: digest(destination / name) for name in CAPTURE_FILES}, destination / 'audio_model/model_worker.js'


def capture_current(directory, limit, lifetime):
    from playwright.sync_api import sync_playwright
    plan = validate(directory)
    selected = [r for r in plan['cases'] if r['id'] in {'emotify:1', 'emotify:101', 'emotify:201', 'emotify:301'}
                and r['ordered_excerpt'] == 1]
    original_affinity = os.sched_getaffinity(0)
    try:
        pending = [(case, profile) for case in selected for profile in ('ordinary', 'one_cpu')
                if not (directory / 'captures' / f"{case['index']:03d}-{profile}" / 'receipt.json').exists()]
        for case, profile in pending[:limit]:
            os.sched_setaffinity(0, original_affinity if profile == 'ordinary' else {min(original_affinity)})
            # Start the driver after setting affinity: Chromium inherits from
            # the Playwright driver, rather than directly from this process.
            with sync_playwright() as playwright:
                target = directory / 'captures' / f"{case['index']:03d}-{profile}"
                target.mkdir(parents=True, exist_ok=False)
                wav = target / 'playback.wav'
                subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1',
                    '-ss', str(case['start']), '-i', case['audio'], '-t', '35', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s16le', str(wav)],
                    check=True, timeout=45, pass_fds=() if lifetime is None else (lifetime,))
                result = browser_sample(playwright, target, wav, 'evaluation-424-' + str(case['index']),
                    prepare=lambda path: prepare_current(path, directory / 'snapshot'))
                # browser_sample owns a browser and closes it before returning.
                reference = native(Path(result['model_pcm']), target, lifetime)
                prediction = result['result']['prediction']
                result['matched_pcm_native_deltas'] = matched(reference, prediction)
                result['numerical_pass'] = numerical_pass(result['matched_pcm_native_deltas'])
                result['profile'] = profile
                result['allowed_cpus'] = sorted(os.sched_getaffinity(0))
                if any(cpus != result['allowed_cpus'] for cpus in result['browser_root_cpu_affinity'].values()):
                    raise ValueError('Browser did not inherit the declared CPU profile')
                result['case'] = case
                result['playback_pass'] = (not result['playback']['paused'] and not result['playback']['error']
                    and not result['playback']['errors'] and result['playback']['position'] >= 10.)
                result['resource_pass'] = (result['wall_seconds'] <= BUDGETS['capture_total_wall_seconds']
                    and result['sampled_browser_cpu_seconds'] <= BUDGETS['browser_cpu_seconds']
                    and result['peak_browser_pss_bytes'] <= BUDGETS['browser_peak_pss_bytes'])
                validate(directory)
                # Do not hash mutable Chromium profiles; the capture result records exact input/model hashes.
                save(target / 'report.json', result)
                outputs = {name: digest(target / name) for name in ('report.json', 'capture.webm', 'browser.f32', 'browser.native.json')}
                save(target / 'receipt.json', {'plan_sha256': digest(directory / 'plan.json'), 'outputs': outputs})
                print(json.dumps({'capture': case['id'], 'profile': profile, 'resource_pass': result['resource_pass'],
                    'wall_seconds': result['wall_seconds'], 'cpu_seconds': result['sampled_browser_cpu_seconds'],
                    'peak_pss_bytes': result['peak_browser_pss_bytes'], 'deltas': result['matched_pcm_native_deltas']}), flush=True)
    finally:
        os.sched_setaffinity(0, original_affinity)


def verify(directory):
    plan = validate(directory)
    reports = []
    for path in sorted(directory.rglob('receipt.json')):
        item = load(path)
        if item['plan_sha256'] != digest(directory / 'plan.json'):
            raise ValueError('Receipt belongs to another plan')
        check([{'path': str(path.parent / name), 'sha256': value} for name, value in item['outputs'].items()])
        reports.append(str(path.parent / 'report.json'))
    comparisons = [load(p) for p in reports if '/comparison/' in p]
    captures = [load(p) for p in reports if '/captures/' in p]
    complete = len(comparisons) == len(plan['cases']) and len(captures) == 8 and (directory / 'retained-decode/receipt.json').exists()
    result = {'plan_sha256': digest(directory / 'plan.json'), 'comparison_cases': len(comparisons),
        'planned_cases': len(plan['cases']), 'capture_cases': len(captures), 'complete': complete,
        'numerical_failures': [r['case']['index'] for r in comparisons if not r['numerical_pass']],
        'resource_failures': [r['case']['index'] for r in comparisons if not r['resource_pass']],
        'capture_resource_failures': [{'id': r['case']['id'], 'profile': r['profile']} for r in captures if not r['resource_pass']],
        'capture_numerical_failures': [{'id': r['case']['id'], 'profile': r['profile']} for r in captures if not r['numerical_pass']],
        'capture_playback_failures': [{'id': r['case']['id'], 'profile': r['profile']} for r in captures if not r['playback_pass']],
        'reports': reports, 'hardware': plan['hardware'],
        'limits': ['One physical desktop, plus one-core affinity sensitivity; not another hardware class.',
            'CPU/PSS samples are not battery measurements; sub-100ms memory peaks may be missed.',
            'Numerical comparisons do not add human quality judgments or calibrated thresholds.',
            'Controlled supplied-audio capture is not signed-in site or ordinary app scheduling acceptance.']}
    save(directory / 'summary.json', result)
    print(json.dumps(result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'decode', 'compare', 'capture', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--limit', type=int, default=4)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(LAB / 'local') or args.limit < 1:
        parser.error('Use a directory under experiments/audio_lab/local and a positive limit')
    if args.action == 'freeze':
        freeze(directory)
    elif args.action == 'verify':
        verify(directory)
    else:
        lifetime = None
        with threadpool_limits(limits=1):
            if args.action == 'decode':
                decode_retained(directory, lifetime)
            elif args.action == 'compare':
                compare(directory, args.limit, lifetime)
            else:
                capture_current(directory, args.limit, lifetime)


if __name__ == '__main__':
    main()
