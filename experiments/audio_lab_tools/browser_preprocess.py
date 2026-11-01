"""Separate decoder/downmix effects using the exact retained browser captures."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
from threadpoolctl import threadpool_limits

from .browser_capture import WORK
from .external_checkpoint import RUN
from .external_experiment import digest, save, stamp
from .heldout_cohort import candidate_predictions
from .reference_pipeline import maximum_delta
from .runtime_snapshot import ensure


def run(source, lifetime=None, decode_only=False):
    if not decode_only:
        ensure()
    original = json.loads(source.read_text())
    target = WORK / ('preprocessing-' + stamp().replace(':', '').replace('+', '_'))
    target.mkdir()
    plan = json.loads((RUN / 'reserved-cohort/plan.json').read_text())
    candidate = plan['candidates']['checkpoint']
    model_path = Path(candidate['path'])
    if digest(model_path) != candidate['sha256']:
        raise ValueError('Frozen checkpoint changed')
    model = None if decode_only else json.loads(model_path.read_text())
    inherited_fds = () if lifetime is None else (lifetime,)
    environment = {**os.environ, 'TF_NUM_INTRAOP_THREADS': '1', 'TF_NUM_INTEROP_THREADS': '1',
        'OMP_NUM_THREADS': '1', 'TF_CPP_MIN_LOG_LEVEL': '2', 'EXTERNAL_TFJS_NATIVE': '1'}
    rows = []
    result = {'created_at': stamp(), 'script_sha256': digest(__file__),
        'capture_report': str(source), 'capture_report_sha256': digest(source),
        'checkpoint_sha256': candidate['sha256'], 'cases': rows,
        'mode': 'decode_only' if decode_only else 'decode_and_model',
        'model_comparison_executed': not decode_only,
        'policy': 'Reuse captured containers and exact model PCM. Compare default ffmpeg mono against an explicit arithmetic stereo mean and the browser two-stage sample-rate contract. Never shift, scale or pad inputs to improve agreement.',
        'limitations': ['Four supplied recordings, controlled player and the frozen model only.',
            'Independent resampling implementations can differ even with identical channel weights.',
            'Prediction changes measure preprocessing sensitivity, not human accuracy.']}
    save(target / 'report.json', result)
    for index, row in enumerate(original['cases']):
        directory = target / str(index)
        directory.mkdir()
        container = Path(row['browser']['container'])
        browser_pcm = Path(row['browser']['model_pcm'])
        if digest(container) != row['captured_container_sha256'] or digest(browser_pcm) != row['captured_pcm_sha256']:
            raise ValueError('Retained capture changed')
        browser = np.fromfile(browser_pcm, dtype='<f4')
        variants = {
            'default_mono': 'aresample=16000,atrim=end_sample=160000',
            'arithmetic_mono': 'pan=mono|c0=0.5*c0+0.5*c1,aresample=16000,atrim=end_sample=160000',
            'browser_rate_contract': 'aresample=44100,pan=mono|c0=0.5*c0+0.5*c1,aresample=16000,atrim=end_sample=160000'}
        decoded = row['browser']['capture']['decoded']
        if not decoded or any(item['channels'] != 2 for item in decoded):
            raise ValueError('This fixed stereo comparison needs a separate mono protocol')
        if not decode_only:
            native_browser = json.loads((browser_pcm.parent / 'matched_browser_pcm.native.json').read_text())
            checkpoint = candidate_predictions(model, np.asarray([native_browser['embedding']]))['prediction'][0]
        methods = {}
        for name, filters in variants.items():
            path = directory / (name + '.f32')
            start = time.monotonic()
            command = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1',
                '-i', str(container)]
            if name == 'default_mono':
                command += ['-ac', '1']
            subprocess.run([*command, '-af', filters, '-f', 'f32le', str(path)],
                check=True, timeout=45, pass_fds=inherited_fds)
            signal = np.fromfile(path, dtype='<f4')
            if signal.shape != (160000,) or not np.isfinite(signal).all():
                raise ValueError('Independent decode did not supply exactly ten seconds')
            measurement = {'pcm_sha256': digest(path),
                'root_mean_squared_pcm_delta': float(np.sqrt(np.mean((browser - signal) ** 2))),
                'decoded_to_browser_amplitude_ratio': float(np.dot(signal, browser) / np.dot(browser, browser))}
            if decode_only:
                measurement['wall_s'] = time.monotonic() - start
                methods[name] = measurement
                continue
            request = {'id': row['id'], 'model_pcm': str(path),
                'native_output': str(directory / (name + '.native.json'))}
            request_path = directory / 'native-request.json'
            save(request_path, request)
            with (directory / 'native.log').open('a') as log:
                subprocess.run([sys.executable, '-B', '-m', 'experiments.audio_lab_tools.reference_pipeline',
                    '--native', str(request_path)], stdout=log, stderr=log, env=environment,
                    check=True, timeout=120, pass_fds=inherited_fds)
            native = json.loads(Path(request['native_output']).read_text())
            prediction = candidate_predictions(model, np.asarray([native['embedding']]))['prediction'][0]
            methods[name] = {**measurement, 'wall_s': time.monotonic() - start,
                'embedding_maximum_delta': maximum_delta(native_browser['embedding'], native['embedding']),
                'tags_maximum_delta': maximum_delta(native_browser['scores'], native['scores']),
                'checkpoint_scores': prediction.tolist(),
                'checkpoint_maximum_score_delta': maximum_delta(checkpoint, prediction),
                'checkpoint_top_choice_changed': bool(prediction.argmax() != checkpoint.argmax())}
        case = {'id': row['id'], 'container_sha256': digest(container),
            'browser_pcm_sha256': digest(browser_pcm), 'methods': methods}
        if not decode_only:
            case['browser_checkpoint_scores'] = checkpoint.tolist()
        rows.append(case)
        save(target / 'report.json', result)
        print(json.dumps({'preprocessing': row['id'], 'methods': methods}), flush=True)
    result['decode_complete'] = True
    result['complete'] = not decode_only
    if digest(source) != result['capture_report_sha256']:
        raise ValueError('Capture report changed during replay')
    save(target / 'report.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--decode-only', action='store_true',
        help='Run only short PCM comparisons without model inference')
    args = parser.parse_args()
    reports = sorted(path for path in WORK.glob('*/report.json')
                     if 'cases' in json.loads(path.read_text()) and not path.parent.name.startswith('preprocessing-'))
    source = args.report or reports[-1]
    if args.decode_only:
        with threadpool_limits(limits=1):
            run(source, decode_only=True)
    else:
        with threadpool_limits(limits=1):
            run(source)


if __name__ == '__main__':
    main()
