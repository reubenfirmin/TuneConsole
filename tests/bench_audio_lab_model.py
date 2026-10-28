"""Explicit CPU-only candidate experiments, not listening validation.

Fetch the optional local research bundle first (experiments/audio_lab/README.md).
uv run pytest -m browser -s tests/bench_audio_lab_model.py --test-lock-wait=60
"""
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import re
from threading import Thread

import pytest
from playwright.sync_api import expect

from tests.browser.test_audio_lab_browser import audio_lab, invoke_toolbar, wait_for_cleanup, LAB

pytestmark = pytest.mark.browser


def test_three_natural_windows_without_model(audio_lab, tmp_path):
    context, worker, page = audio_lab
    page.get_by_role('button', name='Use generated test tone').click()
    expect(page.locator('#identity')).to_have_text(re.compile(r'^sha256:'))
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_contain_text('Analysis on', timeout=10000)
    page.locator('audio').evaluate('(audio) => audio.play()')
    page.wait_for_function("""() => document.querySelectorAll('#results li').length === 3
      || document.getElementById('stop').disabled""", timeout=85000)
    assert page.locator('#results li').count() == 3, page.get_by_role('status').inner_text()
    page.get_by_role('button', name='Stop analysis').click()
    with page.expect_download() as download:
        page.get_by_role('button', name='Export measurements').click()
    output = tmp_path / 'synthetic-capture-report.json'
    download.value.save_as(output)
    report = json.loads(output.read_text())
    assert report['sampling']['capture_coverage']['fraction'] == pytest.approx(.4, abs=.002)
    assert all(row['descriptors']['rms'] == pytest.approx(.1 / 2**.5, abs=.01) for row in report['results'])
    assert all(row['raw_audio_retained'] is False and row['intensity'] is None for row in report['results'])
    assert report['human_judgments'] is None
    print(json.dumps({'artifact': str(output), 'reference': 'synthetic_not_listening_evidence',
        'capture_intervals': [[row['start'], row['end']] for row in report['results']],
        'coverage': report['sampling']['capture_coverage']}), flush=True)


@pytest.mark.parametrize('audio_lab', ['legacy'], indirect=True)
def test_pinned_candidate_is_blocked_by_mv3_csp_and_releases_capture(audio_lab):
    if not (LAB / 'vendor' / 'bundle.json').exists():
        pytest.skip('Fetch the isolated research bundle explicitly before running this benchmark')
    context, worker, page = audio_lab
    network = []
    context.route(re.compile(r'^https?://'), lambda route: (network.append(route.request.url), route.abort()))
    page.get_by_role('button', name='Use generated test tone').click()
    expect(page.locator('#identity')).to_have_text(re.compile(r'^sha256:'))
    page.locator('#model').check()
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_contain_text('Analysis on', timeout=10000)
    page.locator('audio').evaluate('(audio) => audio.play()')
    page.wait_for_function("""() => document.querySelectorAll('#results li').length === 3
      || document.getElementById('stop').disabled""", timeout=95000)
    expect(page.get_by_role('status')).to_contain_text('Content Security Policy')
    assert page.locator('#results li').count() == 0
    assert worker.evaluate("chrome.runtime.getContexts({contextTypes:['OFFSCREEN_DOCUMENT']})") == []
    assert page.locator('audio').evaluate('(audio) => !audio.paused')
    assert network == []


@pytest.fixture
def model_origin():
    class ResearchFiles(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == '/benchmark':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                # Ordinary local web baseline. This deliberately differs from
                # MV3: its result cannot establish extension compatibility.
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self' 'unsafe-eval' 'wasm-unsafe-eval'; worker-src 'self'; connect-src 'self'")
                self.end_headers()
                self.wfile.write(b'<!doctype html><title>Local research CPU baseline</title>')
            else:
                super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(ResearchFiles, directory=str(LAB)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cpu_candidate_on_local_web_baseline(audio_lab, model_origin, tmp_path):
    if not (LAB / 'vendor' / 'bundle.json').exists():
        pytest.skip('Fetch the isolated research bundle explicitly before running this benchmark')
    context, _, page = audio_lab
    external = []

    def local_only(route):
        if route.request.url.startswith(model_origin + '/'):
            route.continue_()
        else:
            external.append(route.request.url)
            route.abort()

    context.route(re.compile(r'^https?://'), local_only)
    page.goto(model_origin + '/benchmark')
    predictions = page.evaluate("""async () => {
      const worker = new Worker('model_worker.js?runtime=legacy');
      try {
        const rows = [];
        for (let index = 0; index < 3; index++) {
          const samples = Float32Array.from({length:160000}, (_, i) => .1 * Math.sin(2 * Math.PI * 440 * i / 16000));
          rows.push(await new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(Error('Local CPU probe exceeded 30 seconds')), 30000);
            worker.onerror = event => { clearTimeout(timeout); reject(Error(event.message)); };
            worker.onmessage = ({data}) => {
              clearTimeout(timeout);
              if (data.error) reject(Error(data.error)); else resolve(data.prediction);
            };
            worker.postMessage({id:'synthetic/' + index, samples, rate:16000}, [samples.buffer]);
          }));
        }
        return rows;
      } finally { worker.terminate(); }
    }""")
    output = tmp_path / 'synthetic-model-cpu-baseline.json'
    output.write_text(json.dumps({'schema': 1, 'environment': 'local_web_not_mv3',
        'reference': 'synthetic_not_listening_evidence', 'human_judgments': None,
        'recommendation_adoption': False, 'predictions': predictions}, indent=2) + '\n')
    assert all(row['backend'] == 'cpu' and row['status'] in {'native_tags_only', 'native_tags_and_moods'} for row in predictions)
    assert all(len(row['scores']) == 50 for row in predictions)
    assert external == [], 'inference must use local assets only'
    print(json.dumps({'artifact': str(output), 'reference': 'synthetic_not_listening_evidence',
        'environment': 'local_web_not_mv3',
        'bundle_bytes': predictions[0]['bundle']['total_bytes'],
        'startup_ms': predictions[0]['startup_ms'],
        'inference_ms': [row['inference_ms'] for row in predictions],
        'tensor_bytes': [row['tensor_bytes'] for row in predictions]}), flush=True)


def require_rebuilt_runtime():
    if not (LAB / 'vendor/runtime.json').exists():
        pytest.skip('Build the optional CSP-compatible research runtime explicitly first')


@pytest.mark.parametrize('audio_lab', ['tampered'], indirect=True)
def test_rebuilt_runtime_rejects_changed_artifact(audio_lab):
    _, _, page = audio_lab
    result = page.evaluate("""async () => {
      const worker = new Worker('runtime_probe.js');
      try {
        return await new Promise((resolve, reject) => {
          const timeout = setTimeout(() => reject(Error('Artifact verification timed out')), 10000);
          worker.onerror = event => { clearTimeout(timeout); reject(Error(event.message)); };
          worker.onmessage = ({data}) => { clearTimeout(timeout); resolve(data); };
          worker.postMessage({frames:[Array(512).fill(0)]});
        });
      } finally { worker.terminate(); }
    }""")
    assert result == {'error': 'Research artifact changed: essentia-csp.js'}


def test_rebuilt_worker_in_mv3_matches_original_predictions(audio_lab, model_origin, tmp_path):
    require_rebuilt_runtime()
    context, _, page = audio_lab
    external = []

    def local_only(route):
        if route.request.url.startswith(model_origin + '/'):
            route.continue_()
        else:
            external.append(route.request.url)
            route.abort()

    context.route(re.compile(r'^https?://'), local_only)
    probe = """async (suffix) => {
      const worker = new Worker('model_worker.js' + suffix);
      try {
        return await new Promise((resolve, reject) => {
          const timeout = setTimeout(() => reject(Error('MV3 worker probe timed out')), 30000);
          worker.onerror = event => { clearTimeout(timeout); reject(Error(event.message)); };
          worker.onmessage = ({data}) => { clearTimeout(timeout); resolve(data); };
          const samples = Float32Array.from({length:160000}, (_, i) => .1 * Math.sin(2 * Math.PI * 440 * i / 16000));
          worker.postMessage({id:'synthetic/mv3', samples, rate:16000}, [samples.buffer]);
        });
      } finally { worker.terminate(); }
    }"""
    result = page.evaluate(probe, '')
    assert 'error' not in result, result.get('error_stack', result)
    page.goto(model_origin + '/benchmark')
    original = page.evaluate(probe, '?runtime=legacy')
    assert 'error' not in original, original.get('error_stack', original)
    assert result['prediction']['scores'] == pytest.approx(original['prediction']['scores'], abs=1e-5, rel=2e-5)
    assert external == []
    artifact = tmp_path / 'synthetic-mv3-worker.json'
    error = max(abs(value - original['prediction']['scores'][key]) for key, value in result['prediction']['scores'].items())
    artifact.write_text(json.dumps({'mv3': result, 'original_web': original, 'max_score_error': error}, indent=2) + '\n')
    print(json.dumps({'artifact': str(artifact), 'startup_ms': result['prediction']['startup_ms'],
                      'inference_ms': result['prediction']['inference_ms'], 'max_score_error': error}), flush=True)


def test_rebuilt_runtime_features_match_original(audio_lab, model_origin, tmp_path):
    require_rebuilt_runtime()
    context, _, page = audio_lab
    page.goto(model_origin + '/benchmark')
    result = page.evaluate("""async () => {
      const frames = [Array(512).fill(0)];
      for (const frequency of [55, 440, 1000, 7900]) {
        frames.push(Array.from({length:512}, (_, i) => .1 * Math.sin(2 * Math.PI * frequency * i / 16000)));
      }
      frames.push(Array.from({length:512}, (_, i) => i === 128 ? 1 : 0));
      let seed = 123456789;
      frames.push(Array.from({length:512}, () => {
        seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
        return .2 * (seed / 4294967296 - .5);
      }));
      frames.push(Array.from({length:512}, (_, i) => .05 * Math.sin(2 * Math.PI * 220 * i / 16000)
        + .02 * Math.cos(2 * Math.PI * 3200 * i / 16000)));
      const results = [];
      for (const suffix of ['?runtime=legacy', '']) {
        const worker = new Worker('runtime_probe.js' + suffix);
        try {
          results.push(await new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(Error('Feature parity probe timed out')), 15000);
            worker.onerror = event => { clearTimeout(timeout); reject(Error(event.message)); };
            worker.onmessage = ({data}) => { clearTimeout(timeout); data.error ? reject(Error(data.error)) : resolve(data); };
            worker.postMessage({frames});
          }));
        } finally { worker.terminate(); }
      }
      return results;
    }""")
    old, new = result
    assert old['runtime_version'] == 'essentia-js-819c00e'
    assert new['runtime_version'] == 'essentia-csp-audio-2'
    assert len(old['bands']) == len(new['bands']) == 8
    errors = []
    for expected, actual in zip(old['bands'], new['bands'], strict=True):
        assert len(expected) == len(actual) == 96
        errors.extend(abs(a - b) for a, b in zip(expected, actual, strict=True))
        assert actual == pytest.approx(expected, abs=2e-5, rel=2e-5)
    artifact = tmp_path / 'runtime-feature-parity.json'
    artifact.write_text(json.dumps({'results': result, 'max_absolute_error': max(errors)}, indent=2) + '\n')
    print(json.dumps({'artifact': str(artifact), 'frames': 8, 'bands': 96, 'max_absolute_error': max(errors)}), flush=True)


def test_rebuilt_model_in_mv3_three_natural_windows(audio_lab, tmp_path):
    require_rebuilt_runtime()
    context, worker, page = audio_lab
    external = []
    context.route(re.compile(r'^https?://'), lambda route: (external.append(route.request.url), route.abort()))
    page.get_by_role('button', name='Use generated test tone').click()
    expect(page.locator('#identity')).to_have_text(re.compile(r'^sha256:'))
    page.locator('#model').check()
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_contain_text('Analysis on', timeout=10000)
    page.locator('audio').evaluate('(audio) => audio.play()')
    page.wait_for_function("""() => document.querySelectorAll('#results li').length === 3
      || document.getElementById('stop').disabled""", timeout=110000)
    assert page.locator('#results li').count() == 3, page.get_by_role('status').inner_text()
    page.get_by_role('button', name='Stop analysis').click()
    wait_for_cleanup(worker)
    with page.expect_download() as download:
        page.get_by_role('button', name='Export measurements').click()
    artifact = tmp_path / 'synthetic-model-mv3.json'
    download.value.save_as(artifact)
    report = json.loads(artifact.read_text())
    predictions = [row['prediction'] for row in report['results']]
    assert all(row['status'] == 'native_tags_only' and row['backend'] == 'cpu'
               and row['runtime_version'] == 'essentia-csp-audio-2' for row in predictions)
    assert all(len(row['scores']) == 50 and row['reliability'] is None for row in predictions)
    assert all(row['analysed_seconds'] == pytest.approx(8.992) for row in predictions)
    assert report['sampling']['capture_coverage']['fraction'] == pytest.approx(.4, abs=.002)
    assert report['sampling']['model_coverage']['fraction'] == pytest.approx(3 * 8.992 / 75)
    assert report['recommendation_adoption'] is False and report['human_judgments'] is None
    assert external == []
    print(json.dumps({'artifact': str(artifact), 'environment': 'installed_mv3',
        'startup_ms': predictions[0]['startup_ms'],
        'inference_ms': [row['inference_ms'] for row in predictions],
        'tensor_bytes': [row['tensor_bytes'] for row in predictions],
        'installed_bundle_bytes': predictions[0]['bundle']['installed_total_bytes']}), flush=True)


@pytest.mark.parametrize('end', ['stop', 'permission', 'identity'])
def test_stop_during_rebuilt_model_analysis_releases_capture(audio_lab, end):
    require_rebuilt_runtime()
    context, worker, page = audio_lab
    page.get_by_role('button', name='Use generated test tone').click()
    expect(page.locator('#identity')).to_have_text(re.compile(r'^sha256:'))
    page.locator('#model').check()
    invoke_toolbar(context, worker, page)
    expect(page.get_by_role('status')).to_contain_text('Analysis on', timeout=10000)
    page.locator('audio').evaluate('(audio) => audio.play()')
    page.wait_for_function("() => document.querySelector('audio').currentTime > 13", timeout=20000)
    assert page.locator('#results li').count() == 0, 'Stop must exercise a pending model job'
    if end == 'stop':
        page.get_by_role('button', name='Stop analysis').click()
    elif end == 'permission':
        page.locator('#permitted').uncheck()
    else:
        page.locator('#title').fill('Changed identity metadata')
    wait_for_cleanup(worker)
    assert page.locator('audio').evaluate('(audio) => !audio.paused')
    page.wait_for_timeout(1000)
    assert page.locator('#results li').count() == 0, 'A cancelled job must not publish late results'
