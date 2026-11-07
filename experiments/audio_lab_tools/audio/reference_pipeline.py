"""Independent native/JavaScript/browser checks on supplied human-rated audio.

This research harness never captures playback or changes the application. Its
fixed-PCM comparison separates model parity from decoding and capture policy.
"""
import argparse
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import urllib.request

import numpy as np

from ..support.external_experiment import LAB, MODEL, OUT, ROOT, digest, pcm, probe, save, stamp
from ..support.runtime_snapshot import ensure as frozen_runtime

WORK = OUT / 'checkpoint-1/reference-pipeline'
NATIVE = LAB / 'local/reference-python'
MODELS = 'https://essentia.upf.edu/models/'


def checkpoint_runtime():
    global MODEL
    MODEL, worker = frozen_runtime()
    return worker


def fetch_assets():
    """Pin the original version-one graphs, outside the browser bundle."""
    checkpoint_runtime()
    assets = {'musicnn.pb': MODELS + 'feature-extractors/musicnn/msd-musicnn-1.pb'}
    for head in json.loads((MODEL / 'mood_models.json').read_text()):
        metadata = json.loads((MODEL / 'vendor' / head['id'] / 'metadata.json').read_text())
        assets[head['id'] + '.pb'] = metadata['link']
    previous = WORK / 'acquisition.json'
    known = json.loads(previous.read_text())['files'] if previous.exists() else {}
    files = {}
    for name, url in assets.items():
        path = WORK / name
        if path.exists() and name in known:
            if digest(path) != known[name]['sha256']:
                raise ValueError('Native reference graph changed: ' + name)
        else:
            with urllib.request.urlopen(url, timeout=45) as response:
                data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024 or len(data) < 100:
                raise ValueError('Unexpected graph size: ' + name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        files[name] = {'url': url, 'bytes': path.stat().st_size, 'sha256': digest(path)}
        save(previous, {'created_at': stamp(), 'files': {**known, **files},
                        'browser_bundle_sha256': digest(MODEL / 'vendor/bundle.json')})
        print(json.dumps({'reference_graph': name, **files[name]}), flush=True)
    return files


def native(request_path):
    """Run in an isolated child; use the published C++ frontend and .pb graphs."""
    checkpoint_runtime()
    sys.path.insert(0, str(NATIVE))
    import essentia
    import essentia.standard as es

    request = json.loads(Path(request_path).read_text())
    signal = np.fromfile(request['model_pcm'], dtype='<f4')
    started = time.perf_counter()
    frontend = es.TensorflowInputMusiCNN()
    frames = (len(signal) - 512) // 256 + 1
    patches = frames // 187
    mel = np.asarray([frontend(signal[i * 256:i * 256 + 512])
                      for i in range(patches * 187)], dtype=np.float32)
    pool = essentia.Pool()
    pool.set('model/Placeholder', mel.reshape(patches, 1, 187, 96))
    outputs = ['model/Sigmoid', 'model/dense/BiasAdd']
    model = es.TensorflowPredict(graphFilename=str(WORK / 'musicnn.pb'),
        inputs=['model/Placeholder'], outputs=outputs, isTrainingName='model/Placeholder_1')
    result = model(pool)
    tags = np.asarray(result[outputs[0]]).reshape(patches, 50)
    embedding = np.asarray(result[outputs[1]]).reshape(patches, 200)
    traits = {}
    for head in json.loads((MODEL / 'mood_models.json').read_text()):
        metadata = json.loads((MODEL / 'vendor' / head['id'] / 'metadata.json').read_text())
        input_name = metadata['schema']['inputs'][0]['name']
        output_name = metadata['schema']['outputs'][0]['name']
        pool = essentia.Pool()
        pool.set(input_name, embedding.reshape(patches, 1, 1, 200))
        predictor = es.TensorflowPredict(graphFilename=str(WORK / (head['id'] + '.pb')),
            inputs=[input_name], outputs=[output_name])
        values = np.asarray(predictor(pool)[output_name]).reshape(patches, 2)
        labels = [head['class']] if head.get('class') else metadata['classes']
        for label in labels:
            traits[label] = float(values[:, metadata['classes'].index(label)].mean())
    matched_seconds = time.perf_counter() - started
    # Also retain the published wrapper's own framing/hop behavior. Agreement
    # under a deliberately matched policy cannot establish default-policy parity.
    policies = {}
    for name, options in (('published_default', {}), ('nonoverlapping_patches', {'patchHopSize': 0})):
        predictor = es.TensorflowPredictMusiCNN(graphFilename=str(WORK / 'musicnn.pb'),
            output='model/dense/BiasAdd', isTrainingName='model/Placeholder_1', **options)
        values = np.asarray(predictor(signal)).reshape(-1, 200)
        policies[name] = {'patches': len(values), 'embedding': values.mean(0).tolist()}
    save(request['native_output'], {'essentia_version': essentia.__version__,
        'distribution_version': importlib.metadata.version('essentia-tensorflow'),
        'frontend_policy': '512-sample frames beginning at zero, hop 256; 187 frames per nonoverlapping full patch',
        'patches': patches, 'embedding': embedding.mean(0).tolist(),
        'scores': dict(zip(json.loads((MODEL / 'vendor/musicnn/metadata.json').read_text())['classes'],
                           tags.mean(0).tolist())), 'traits': traits, 'policies': policies,
        'matched_policy_wall_seconds': matched_seconds,
        'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})


def examples():
    rows = json.loads((OUT / 'emotify-artist.features.json').read_text())['rows']
    # Fixed source IDs provide four real musical genres, not selected successes.
    wanted = {'emotify:1', 'emotify:101', 'emotify:201', 'emotify:301'}
    return [row for row in rows if row['id'] in wanted]


def process_sample(roots):
    """Linux browser process-tree totals; PSS avoids shared-page double counting."""
    pending, visited, rss, pss, cpu = list(roots), set(), 0, 0, 0.
    ticks = os.sysconf('SC_CLK_TCK')
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        base = Path('/proc') / str(pid)
        try:
            fields = (base / 'stat').read_text().rpartition(')')[2].split()
            cpu += (int(fields[11]) + int(fields[12])) / ticks
            rss += int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
            for line in (base / 'smaps_rollup').read_text().splitlines():
                if line.startswith('Pss:'):
                    pss += int(line.split()[1]) * 1024
            pending.extend(map(int, (base / f'task/{pid}/children').read_text().split()))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return {'rss_bytes': rss, 'pss_bytes': pss, 'cpu_seconds': cpu,
            'processes_seen': len(visited)}


@contextmanager
def local_origin():
    class Files(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            original = Path(super().translate_path(path))
            production = MODEL / original.relative_to(LAB)
            return str(production if production.is_file() else original)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Files, directory=str(LAB)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def browser_compare(requests):
    from playwright.sync_api import sync_playwright

    rows, network = [], []
    with local_origin() as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--mute-audio'])
        context = browser.new_context()
        def route(request):
            if request.request.url.startswith(origin + '/'):
                request.continue_()
            else:
                network.append(request.request.url)
                request.abort()
        context.route('**/*', route)
        page = context.new_page()
        page.goto(origin + '/reference.html')
        session = browser.new_browser_cdp_session()
        roots = [row['id'] for row in session.send('SystemInfo.getProcessInfo')['processInfo']
                 if row['type'] == 'browser']
        page.evaluate("window.parityWorker = new Worker('model_worker.js')")
        try:
            for request in requests:
                stopped, samples = Event(), []
                def sample():
                    while not stopped.is_set():
                        samples.append(process_sample(roots))
                        stopped.wait(.1)
                sampler = Thread(target=sample, daemon=True)
                sampler.start()
                begin = time.perf_counter()
                try:
                    result = page.evaluate("""async (request) => {
                      const bytes = await fetch(request.url).then(r => r.arrayBuffer());
                      const signal = new Float32Array(bytes);
                      return await new Promise((resolve, reject) => {
                        const deadline = setTimeout(() => reject(Error('Reference worker timed out')), 90000);
                        parityWorker.onerror = event => {clearTimeout(deadline); reject(Error(event.message));};
                        parityWorker.onmessage = ({data}) => {clearTimeout(deadline); resolve(data);};
                        parityWorker.postMessage({id: request.id, rate:16000, samples:signal}, [signal.buffer]);
                      });
                    }""", {'id': request['id'], 'url': '/' + str(Path(request['model_pcm']).relative_to(LAB))})
                finally:
                    stopped.set()
                    sampler.join(timeout=5)
                if result.get('error'):
                    raise RuntimeError(result['error'])
                samples.append(process_sample(roots))
                rows.append({'id': request['id'], 'result': result,
                    'wall_seconds': time.perf_counter() - begin,
                    'process_tree_peak_pss_bytes': max(row['pss_bytes'] for row in samples),
                    'process_tree_peak_rss_bytes': max(row['rss_bytes'] for row in samples),
                    'sampled_process_tree_cpu_seconds': max(0., samples[-1]['cpu_seconds'] - samples[0]['cpu_seconds']),
                    'resource_samples': len(samples)})
        finally:
            page.evaluate('parityWorker.terminate()')
            version = browser.version
            browser.close()
    if network:
        raise RuntimeError('Browser attempted nonlocal network requests: ' + repr(network))
    return {'chromium_version': version, 'rows': rows, 'external_network_requests': network,
            'scope': 'Ordinary headless Chromium web worker; exact decoded 16 kHz PCM. No tab capture, browser resampling, MV3 lifecycle, battery or listening judgments.',
            'memory_policy': '100 ms sampling of the browser process tree. Peak PSS apportions shared pages; summed RSS double counts shared pages. Shorter peaks may be missed.'}


def maximum_delta(left, right):
    if isinstance(left, dict):
        if set(left) != set(right):
            raise ValueError('Compared prediction keys differ')
        return max(abs(float(left[key]) - float(right[key])) for key in left)
    return float(np.max(np.abs(np.asarray(left) - np.asarray(right))))


def evaluate(lifetime=None):
    worker = checkpoint_runtime()
    required = [WORK / name for name in ('musicnn.pb', 'relaxed.pb', 'aggressive.pb', 'happy.pb', 'sad.pb', 'deam.pb')]
    if not all(path.exists() for path in required):
        raise ValueError('Fetch the pinned native graphs with --fetch-only first')
    environment = {**os.environ, 'TF_NUM_INTRAOP_THREADS': '1', 'TF_NUM_INTEROP_THREADS': '1',
                   'OMP_NUM_THREADS': '1', 'TF_CPP_MIN_LOG_LEVEL': '2', 'EXTERNAL_TFJS_NATIVE': '1'}
    requests, cases = [], []
    with tempfile.TemporaryDirectory(prefix='pcm-', dir=WORK) as scratch:
        for index, row in enumerate(examples()):
            path = Path(scratch) / (str(index) + '.f32')
            pcm(row['audio'], row['sample_start'], 16000, 1, path, lifetime_fd=lifetime)
            request = {'id': row['id'], 'model_pcm': str(path.resolve()),
                       'native_output': str(WORK / (row['id'].replace(':', '-') + '.native.json'))}
            request_path = WORK / 'request.json'
            save(request_path, request)
            start = time.perf_counter()
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            with (WORK / 'native.log').open('a') as log:
                subprocess.run([sys.executable, '-B', '-m',
                    'experiments.audio_lab_tools.audio.reference_pipeline', '--native', str(request_path)],
                    env=environment, stdout=log, stderr=log, check=True, timeout=120,
                    pass_fds=() if lifetime is None else (lifetime,))
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            reference = json.loads(Path(request['native_output']).read_text())
            reference['child_wall_seconds_including_startup'] = time.perf_counter() - start
            reference['child_cpu_seconds'] = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
            with (WORK / 'node.log').open('a') as log:
                output = subprocess.run(['node', str(worker)],
                    input=json.dumps(request) + '\n', text=True, capture_output=False, stdout=subprocess.PIPE,
                    stderr=log, env=environment, check=True, timeout=120,
                    pass_fds=() if lifetime is None else (lifetime,)).stdout
            result = json.loads(next(line[7:] for line in output.splitlines() if line.startswith('RESULT ')))
            if result.get('error'):
                raise RuntimeError(result['error'])
            prediction = result['prediction']
            deltas = {'embedding': maximum_delta(reference['embedding'], prediction['embedding']['values']),
                      'tags': maximum_delta(reference['scores'], prediction['scores']),
                      'moods': maximum_delta(reference['traits'],
                                            {key: value['score'] for key, value in prediction['moods']['traits'].items()})}
            cases.append({'id': row['id'], 'artist': row['artist'], 'title': row['title'],
                'audio_sha256': row['audio_sha256'], 'pcm_sha256': digest(path),
                'sample_start': row['sample_start'], 'sample_seconds': 10,
                'native': reference, 'node': result, 'native_node_maximum_absolute_delta': deltas})
            requests.append(request)
        browser = browser_compare(requests)
        for case, result in zip(cases, browser['rows']):
            prediction = result['result']['prediction']
            case['browser_node_maximum_absolute_delta'] = {
                'embedding': maximum_delta(case['node']['prediction']['embedding']['values'], prediction['embedding']['values']),
                'tags': maximum_delta(case['node']['prediction']['scores'], prediction['scores']),
                'moods': maximum_delta(
                    {key: value['score'] for key, value in case['node']['prediction']['moods']['traits'].items()},
                    {key: value['score'] for key, value in prediction['moods']['traits'].items()})}
    report = {'created_at': stamp(), 'script_sha256': digest(__file__),
        'native_graphs': json.loads((WORK / 'acquisition.json').read_text()),
        'worker_sha256': digest(MODEL / 'model_worker.js'),
        'runtime_snapshot_sha256': digest(MODEL.parent / 'manifest.json'),
        'native_package': 'essentia-tensorflow==2.1b6.dev1438', 'cases': cases, 'browser': browser,
        'limitations': ['Four supplied Emotify recordings are a bounded numerical/resource probe, not a held-out listening study.',
            'Native C++ and WASM builds have different release versions; observed numerical agreement is measured rather than assumed.',
            'Fixed-PCM parity does not verify browser capture, downmixing or resampling against the reference decoder.',
            'Published wrapper policy results retain their own framing and overlap; only matched-policy outputs are compared as numerical equivalents.',
            'No battery observation or trait adoption. All assets remain local research inputs.']}
    save(WORK / 'report.json', report)
    print(json.dumps({'reference_pipeline_report': str(WORK / 'report.json'),
        'cases': [{key: row[key] for key in ('id', 'native_node_maximum_absolute_delta', 'browser_node_maximum_absolute_delta')}
                  for row in cases]}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch-only', action='store_true')
    parser.add_argument('--native', type=Path)
    args = parser.parse_args()
    if args.native:
        native(args.native)
    elif args.fetch_only:
        fetch_assets()
    else:
        evaluate()


if __name__ == '__main__':
    main()
