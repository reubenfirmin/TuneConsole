"""Exact native/browser PCM plus frozen acoustic modules for Memo confirmation."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import resource
import subprocess
from threading import Lock
import time

import numpy as np

from .audio_evaluation import origin
from ..datasets.dataset_windows import content_hash
from ..support.external_experiment import digest, pcm, probe, save
from ..focus.focus_experiment import load
from .quality_reference_audio import Extractor, response, provenance as encoder_provenance
from ..support.runtime_snapshot import ensure

WORKER = Path(__file__).with_name('quality_measure_worker.mjs')
STARTS = tuple(range(15, 75, 10))
RUNTIME_LOCK = Lock()

BROWSER = """async request=>{
 const encode=values=>{const bytes=new Uint8Array(values.buffer,values.byteOffset,values.byteLength);
   let result='';for(let i=0;i<bytes.length;i+=8192)result+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(result);};
 if(request.action==='load'){
   const bytes=await fetch(request.url).then(r=>{if(!r.ok)throw Error('Missing source');return r.arrayBuffer();});
   window.memoAudio=await new OfflineAudioContext(2,1,44100).decodeAudioData(bytes);
   if(memoAudio.numberOfChannels>2||memoAudio.length<75*44100)throw Error('Incomplete annotated interval');
   return {channels:memoAudio.numberOfChannels,rate:memoAudio.sampleRate,length:memoAudio.length};
 }
 if(request.action==='release'){window.memoAudio=null;return true;}
 const decoded=window.memoAudio,first=request.start*44100,channels=decoded.numberOfChannels;
 if(first<0||first+441000>decoded.length)throw Error('Incomplete ten-second interval');
 const context=new OfflineAudioContext(1,160000,16000),source=context.createBufferSource();
 source.buffer=decoded;source.connect(context.destination);source.start(0,request.start);
 const rendered=await context.startRendering(),model=rendered.getChannelData(0).slice();
 const measured=new Float32Array(441000*channels);
 for(let i=0;i<441000;i++)for(let c=0;c<channels;c++)measured[i*channels+c]=decoded.getChannelData(c)[first+i];
 if(model.length!==160000||!model.every(Number.isFinite)||!measured.every(Number.isFinite))throw Error('Nonfinite PCM');
 return {model:encode(model),measurement:encode(measured),channels};
}"""


def provenance():
    with RUNTIME_LOCK:
        runtime, _ = ensure()
    return {'encoder': encoder_provenance(), 'acoustic_worker_sha256': digest(WORKER),
        'runtime_files': {str(p): digest(p) for p in [runtime / 'runtime_loader.js', runtime / 'audio_attributes.js',
            runtime / 'vendor/bundle.json', runtime / 'vendor/runtime.json', runtime / 'vendor/meyda.min.js', runtime / 'vendor/essentia-csp.js']},
        'browser_policy': 'Original source decoded by OfflineAudioContext at 44100 Hz; each exact ten seconds separately rendered 16k mono; same original 44.1k mono/stereo measurement samples. No padding, gain or FFmpeg browser surrogate.'}


@contextmanager
def measurements(directory):
    with RUNTIME_LOCK:
        runtime, _ = ensure()
    with (directory / 'acoustic.log').open('a') as log:
        worker = subprocess.Popen(['node', str(WORKER), str(runtime)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1)
        try:
            response(worker, 'READY ')
            yield worker
        finally:
            worker.stdin.close()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)


def infer(encoder, acoustic, model_pcm, measurement_pcm, channels, identifier):
    encoder.worker.stdin.write(json.dumps({'id': identifier, 'pcm': str(model_pcm)}) + '\n')
    encoder.worker.stdin.flush()
    embedded = response(encoder.worker, 'RESULT ')
    acoustic.stdin.write(json.dumps({'id': identifier, 'pcm': str(measurement_pcm), 'channels': channels}) + '\n')
    acoustic.stdin.flush()
    measured = response(acoustic, 'RESULT ')
    if embedded['id'] != identifier or measured['id'] != identifier or embedded['patches'] != 3:
        raise ValueError('Worker returned a different interval')
    encoder.new += 1
    attributes = measured['attributes']['values']
    if len(attributes) != 27 or any(v is not None and not np.isfinite(v) for v in attributes.values()):
        raise ValueError('Invalid acoustic attributes')
    return {'embedding': embedded['embedding'], 'attributes': attributes,
            'model_pcm_sha256': digest(model_pcm), 'measurement_pcm_sha256': digest(measurement_pcm)}


def extract(directory, rows, feature_names):
    if not rows:
        return [], [], {'seconds': 0., 'workers': [], 'new_chunks': 0}, []
    from playwright.sync_api import sync_playwright, Error as BrowserError
    identity_provenance = provenance()  # Initialize assets before starting threads.
    started = time.monotonic()
    retained, rejected, paths, resources = [], [], [], []
    (directory / 'index.html').write_text('<!doctype html><title>Memo preparation</title>')
    with origin(directory) as base:
        def branch(index):
            target = directory / f'worker-{index}'
            target.mkdir(exist_ok=True)
            local_rows, errors, caches = [], [], []
            with Extractor(target, seconds=7200) as encoder, measurements(target) as acoustic, sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--mute-audio'])
                try:
                    context = browser.new_context()
                    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
                    page = context.new_page()
                    page.set_default_timeout(60000)
                    page.goto(base + '/index.html')
                    version = browser.version
                    for row in rows[index::2]:
                        if time.monotonic() - started > 7200:
                            raise RuntimeError('Frozen combined extraction time limit')
                        try:
                            duration, channels = probe(row['audio'])
                            if duration < 75 or channels not in (1, 2) or digest(row['audio']) != row['audio_sha256']:
                                raise ValueError('Incomplete interval, unsupported channels or changed source')
                            decoded = page.evaluate(BROWSER, {'action': 'load', 'url': base + '/audio/' + row['file_name']})
                            chunks = []
                            for start in STARTS:
                                chunk = {'start': start, 'seconds': 10}
                                for prep in ('historical', 'browser'):
                                    identity = {'audio_sha256': row['audio_sha256'], 'preparation': prep, 'start': start, 'seconds': 10, 'provenance': identity_provenance}
                                    cache = directory / 'chunks' / (content_hash(identity) + '.json')
                                    if cache.exists():
                                        record = load(cache)
                                        if record['input'] != identity or content_hash(record['result']) != record['result_sha256']:
                                            raise ValueError('Chunk cache changed')
                                        result = record['result']
                                        encoder.reused += 1
                                    else:
                                        model_pcm = Path(encoder.scratch.name) / 'model.f32'
                                        measure_pcm = Path(encoder.scratch.name) / 'measurement.f32'
                                        if prep == 'historical':
                                            pcm(row['audio'], start, 16000, 1, model_pcm)
                                            pcm(row['audio'], start, 44100, channels, measure_pcm)
                                            prepared_channels = channels
                                        else:
                                            prepared = page.evaluate(BROWSER, {'action': 'chunk', 'start': start})
                                            model_pcm.write_bytes(base64.b64decode(prepared['model'], validate=True))
                                            measure_pcm.write_bytes(base64.b64decode(prepared['measurement'], validate=True))
                                            prepared_channels = prepared['channels']
                                        result = infer(encoder, acoustic, model_pcm, measure_pcm, prepared_channels, row['id'])
                                        save(cache, {'input': identity, 'result': result, 'result_sha256': content_hash(result)})
                                    if sorted(result['attributes']) != feature_names or np.asarray(result['embedding']).shape != (200,):
                                        raise ValueError('Feature schema differs from frozen heads')
                                    chunk[prep] = result
                                    caches.append(cache)
                                chunks.append(chunk)
                            local_rows.append({**row, 'recording_duration': duration, 'decoded': decoded,
                                               'chunks': chunks, 'covered_interval': [15,75]})
                            print(json.dumps({'memo_features': row['id'], 'worker': index, 'completed': len(local_rows)}), flush=True)
                        except (ValueError, OSError, BrowserError) as error:
                            errors.append({'id': row['id'], 'error': str(error)})
                        finally:
                            page.evaluate(BROWSER, {'action': 'release'})
                finally:
                    browser.close()
            return local_rows, errors, caches, {**encoder.resources, 'chromium': version}
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(branch, i) for i in range(2)]
            for future in pending:
                rr, ee, cc, resource_row = future.result()
                retained.extend(rr)
                rejected.extend(ee)
                paths.extend(cc)
                resources.append(resource_row)
    retained.sort(key=lambda r: r['id'])
    return retained, rejected, {'seconds': time.monotonic()-started, 'workers': resources,
        'new_chunks': sum(r['new_chunks'] for r in resources),
        'child_peak_rss_bytes': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024}, paths
