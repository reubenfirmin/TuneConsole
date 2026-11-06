"""Normal app, installed bridge, native capture and actual local CPU inference."""
import io
import hashlib
import json
import math
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time
from urllib.parse import urlparse
import wave
import zipfile

import pytest
from playwright.sync_api import expect

from yt_playlist.web.routes.audio_preview import available


EXT = Path(__file__).resolve().parents[2] / 'extension'


def _evaluate(cdp, expression, context_id=None):
    result = cdp.send('Runtime.evaluate', {
        'expression': expression, 'awaitPromise': True, 'returnByValue': True,
        'userGesture': False, **({'contextId': context_id} if context_id else {}),
    })
    assert 'exceptionDetails' not in result, result
    return result.get('result', {}).get('value')


def _wait(cdp, expression, timeout=15, context_id=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _evaluate(cdp, expression, context_id)
        if result:
            return result
        time.sleep(.1)
    status = _evaluate(cdp, 'document.querySelector("[data-preview-status]")?.textContent')
    pytest.fail(f'Timed out: {expression}; preview status: {status}')


@pytest.fixture
def preview_browser(playwright, browser_type_launch_args, live_app, tmp_path, request, record_property):
    if not available():
        pytest.skip('Prepare the production audio model assets first')
    variant = getattr(request, 'param', None)
    fixture_started = time.perf_counter()
    extension = tmp_path / 'extension'
    if variant in {'packaged', 'packaged_sampling'}:
        build = tmp_path / 'build'
        shutil.copytree(EXT, build / 'extension')
        (build / 'scripts').mkdir()
        shutil.copyfile(EXT.parent / 'scripts/build-extension.sh', build / 'scripts/build-extension.sh')
        shutil.copytree(EXT.parent / 'scripts/audio_model', build / 'scripts/audio_model',
                        ignore=shutil.ignore_patterns('__pycache__'))
        result = subprocess.run(['bash', str(build / 'scripts/build-extension.sh')],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        archive, = (build / 'dist').glob('*.zip')
        with zipfile.ZipFile(archive) as package:
            package.extractall(extension)
        # Establish source/packaged runtime equivalence before fixture-only
        # origin/identity edits. Generated manifests have explicit differences.
        checked = {}
        for installed in extension.rglob('*'):
            if not installed.is_file():
                continue
            relative = installed.relative_to(extension)
            if str(relative) in {'manifest.json', 'audio_model/vendor/bundle.json', 'audio_model/vendor/runtime.json'}:
                continue
            data = installed.read_bytes()
            assert data == (EXT / relative).read_bytes(), relative
            checked[str(relative)] = hashlib.sha256(data).hexdigest()
        assert {'worker/audio_collection.js', 'audio_model/model_worker.js'} <= checked.keys()
        for name in ('bundle.json', 'runtime.json'):
            packaged = json.loads((extension / 'audio_model/vendor' / name).read_text())
            source_manifest = json.loads((EXT / 'audio_model/vendor' / name).read_text())
            assert {key: value for key, value in packaged.items() if key not in {'purpose', 'worker_sources'}} == {
                key: value for key, value in source_manifest.items() if key not in {'purpose', 'worker_sources'}}
        for row in json.loads((extension / 'audio_model/vendor/bundle.json').read_text())['worker_sources']:
            data = (extension / 'audio_model' / row['path']).read_bytes()
            assert len(data) == row['bytes'] and hashlib.sha256(data).hexdigest() == row['sha256']
        record_property('packaged_source_byte_identity', json.dumps(checked, sort_keys=True))
        manifest_path = extension / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        assert 'key' not in manifest
        assert manifest == {key: value for key, value in json.loads((EXT / 'manifest.json').read_text()).items()
                            if key != 'key'}
        assert not {'tabs', 'history'} & set(manifest['permissions'])
        # Give this unpacked ZIP the app's recognized development identity.
        # All inference source, runtime and weights remain the packaged bytes.
        manifest['key'] = json.loads((EXT / 'manifest.json').read_text())['key']
        manifest_path.write_text(json.dumps(manifest))
    else:
        shutil.copytree(EXT, extension)
    shutil.copyfile(EXT.parent / 'experiments/audio_lab/embedding_probe.js', extension / 'audio_model/embedding_probe.js')
    shutil.copyfile(EXT.parent / 'experiments/audio_lab/attributes_probe.js', extension / 'audio_model/attributes_probe.js')
    if getattr(request, 'param', None) == 'tampered_head':
        weights = extension / 'audio_model/vendor/relaxed/group1-shard1of1.bin'
        content = bytearray(weights.read_bytes())
        content[0] ^= 1
        weights.write_bytes(content)
    with socket.socket() as unused:
        unused.bind(('127.0.0.1', 0))
        unused_port = unused.getsockname()[1]
    config = extension / 'worker' / 'config.js'
    origin = '{0.scheme}://{0.netloc}'.format(urlparse(live_app))
    config.write_text(config.read_text().replace('appOrigin: "http://127.0.0.1:8765"',
        f'appOrigin: "{origin}"').replace('ws://127.0.0.1:8765', f'ws://127.0.0.1:{unused_port}'))
    # The private backend uses a random port. Match its host permission as well
    # as appOrigin so Chromium can read delivery acknowledgements on first start.
    manifest_path = extension / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['host_permissions'] = [origin + '/*' if value == 'http://127.0.0.1:8765/*' else value
                                    for value in manifest['host_permissions']]
    manifest_path.write_text(json.dumps(manifest))
    record_property('native_extension_setup_seconds', time.perf_counter() - fixture_started)
    phase_started = time.perf_counter()
    context = playwright.chromium.launch_persistent_context(
        tmp_path / 'profile', **{**browser_type_launch_args, 'channel': 'chromium'},
        accept_downloads=True, ignore_default_args=['--mute-audio'], args=[
            f'--disable-extensions-except={extension}', f'--load-extension={extension}',
            # FakeAudioManager never opens the user's speaker hardware.
            '--disable-audio-output',
        ])
    record_property('native_browser_launch_seconds', time.perf_counter() - phase_started)
    phase_started = time.perf_counter()
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        second = b''.join(struct.pack('<h', int(3276 * math.sin(2 * math.pi * 440 * i / 48000)))
            for i in range(48000))
        duration = 195 if variant in {'natural_sampling', 'natural_offset', 'packaged_sampling'} else (
            180 if getattr(request, 'param', None) == 'scheduled_sampling' else 30)
        audio.writeframes(second * duration)
    record_property('native_audio_fixture_seconds', time.perf_counter() - phase_started)

    def music(route):
        if route.request.url.endswith('.wav'):
            route.fulfill(content_type='audio/wav', body=buffer.getvalue())
        else:
            route.fulfill(content_type='text/html', headers={
                'Content-Security-Policy': "default-src 'self'; script-src 'none'; media-src 'self' blob:",
            }, body='<title>Automatic audio fixture</title><video src="/preview.wav" loop></video>')

    context.route('https://music.youtube.com/**', music)
    context.route('https://tuneconsole.com/**', lambda route: route.fulfill(
        content_type='text/html', body='<h1>Setup fixture</h1>'))
    context.add_init_script('''
      navigator.mediaDevices.getDisplayMedia = () => {throw Error('No sharing picker allowed');};
      navigator.mediaDevices.getUserMedia = () => {throw Error('No microphone allowed');};
      window.AudioContext = class {constructor() {throw Error('No live audio output allowed');}};
    ''')
    try:
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
        source = context.new_page()
        cdp = context.new_cdp_session(source)
        worlds = []
        cdp.on('Runtime.executionContextCreated', lambda event: worlds.append(event['context']))
        cdp.send('Runtime.enable')
        song_id = 'vibe_song' if getattr(request, 'param', None) in {
            'scheduled_sampling', 'natural_sampling', 'natural_offset', 'packaged_sampling'} else 'preview_song'
        source.goto('https://music.youtube.com/')
        deadline = time.monotonic() + 10
        isolated = None
        while time.monotonic() < deadline and isolated is None:
            for world in worlds:
                if world.get('auxData', {}).get('isDefault') is False and world.get('origin', '').startswith('chrome-extension://'):
                    if _evaluate(cdp, '!!window.__tcAudioCapture', world['id']):
                        isolated = world['id']
                        break
            if isolated is None:
                _evaluate(cdp, 'true')
                time.sleep(.05)
        assert isolated is not None
        _evaluate(cdp, 'window.postMessage({__tcSensorVersion:4,__tcSensorRevision:8,__tcNow:{videoId:'
                  + json.dumps(song_id) + ',title:"Fixture song",paused:false}},"*")')
        if variant in {'natural_sampling', 'natural_offset', 'packaged_sampling'}:
            # Real native playback supplies both clocks. Only the exposed raw
            # media timeline is offset, as YTM does when reusing its stream.
            # The production MAIN sensor reads the player's per-song getter.
            offset = 1692 if request.param == 'natural_offset' else 0
            _evaluate(cdp, '''(() => {
              const media=document.querySelector('video');
              const nativeTime=Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,'currentTime').get;
              window.previewSongPosition=()=>nativeTime.call(media);
              window.previewMediaOffset=OFFSET;
              Object.defineProperty(media,'currentTime',{
                get(){return nativeTime.call(this)+OFFSET;},
                set(){throw Error('Natural playback must not seek');}
              });
              const player=document.createElement('div'); player.id='movie_player';
              player.getVideoData=()=>({video_id:'vibe_song',title:'Fixture song',author:'Observed artist'});
              player.getCurrentTime=previewSongPosition;
              player.getPlaylistId=()=>'';
              player.getPlayerResponse=()=>({videoDetails:{
                videoId:player.getVideoData().video_id,lengthSeconds:'195'}});
              document.body.append(player);
            })()'''.replace('OFFSET', str(offset)))
            # DOM wrapper expandos are isolated between JS worlds. Expose the
            # same continuous native timeline to the capture script too.
            _evaluate(cdp, '''(() => {
              const media=document.querySelector('video');
              const nativeTime=Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,'currentTime').get;
              Object.defineProperty(media,'currentTime',{
                get(){return nativeTime.call(this)+OFFSET;},
                set(){throw Error('Natural playback must not seek');}
              });
            })()'''.replace('OFFSET', str(offset)), isolated)
        _evaluate(cdp, '''(() => {
          window.previewStreams=[]; window.previewPCM=[];
          window.previewRecordings=[];
          window.previewMessages=[];
          window.previewNativeCosts={messages:[],decodes:[]};
          chrome.runtime.onConnect.addListener(port=>{
            const send=port.postMessage.bind(port);
            port.postMessage=data=>{
              previewNativeCosts.messages.push({type:data.type,id:data.id,milliseconds:performance.now()});
              previewMessages.push(data);return send(data);
            };
          });
          const capture=HTMLMediaElement.prototype.captureStream;
          HTMLMediaElement.prototype.captureStream=function() {
            const stream=capture.call(this); previewStreams.push(stream); return stream;
          };
          const Offline=OfflineAudioContext;
          const Recorder=MediaRecorder;
          window.MediaRecorder=class extends Recorder {
            constructor(...args) {
              super(...args);
              const record={bytes:0,settings:args[0].getAudioTracks()[0]?.getSettings()};
              previewRecordings.push(record);
              this.addEventListener('start',()=>record.started_ms=performance.now());
              this.addEventListener('stop',()=>record.stopped_ms=performance.now());
              this.addEventListener('dataavailable',event=>record.bytes+=event.data.size);
            }
          };
          window.OfflineAudioContext=class extends Offline {
            async decodeAudioData(...args) {
              const started=performance.now();
              try {
                const pcm=await super.decodeAudioData(...args); previewPCM.push(pcm); return pcm;
              } finally {previewNativeCosts.decodes.push(performance.now()-started);}
            }
          };
          window.AudioContext=class {constructor(){throw Error('No live context allowed');}};
          navigator.mediaDevices.getDisplayMedia=()=>{throw Error('No sharing allowed');};
          navigator.mediaDevices.getUserMedia=()=>{throw Error('No microphone allowed');};
        })()''', isolated)
        if getattr(request, 'param', None) == 'no_activation':
            # Default autoplay permits muted playback without activation.
            _evaluate(cdp, 'document.querySelector("video").muted=true; document.querySelector("video").play()')
            assert _evaluate(cdp, 'navigator.userActivation.hasBeenActive') is False
        else:
            # Normal playback can have its usual activation. There is no
            # additional capture click, and FakeAudioManager still stays silent.
            source.locator('video').evaluate('media=>media.play()')
        # Installing this fixture's bridge also opens its routed setup website.
        # Keep capture checks independent of that separate onboarding surface.
        for candidate in context.pages:
            if candidate.url == 'https://tuneconsole.com/extension-setup':
                candidate.close()
        record_property('native_fixture_total_seconds', time.perf_counter() - fixture_started)
        yield context, worker, source, cdp, isolated
    finally:
        phase_started = time.perf_counter()
        context.close()
        record_property('native_browser_close_seconds', time.perf_counter() - phase_started)


def mock_playback(page):
    started = time.monotonic()
    state = {'video_id': 'preview_song', 'title': 'Preview song', 'artist': 'Preview artist',
             'metadata_verified': True, 'paused': False, 'snapshot_age': 0}

    def status(route):
        route.fulfill(json={'connected': True, 'now_playing': {
            **state, 'position': 20 + time.monotonic() - started}})

    page.route('**/bridge/status', status)
    return state


def _released(cdp, isolated):
    assert _evaluate(cdp, '''previewStreams.every(stream=>stream.getTracks().every(t=>t.readyState==='ended'))
      && previewPCM.every(pcm=>Array.from({length:pcm.numberOfChannels},(_,i)=>
        pcm.getChannelData(i).every(sample=>sample===0)).every(Boolean))''', isolated)
    assert _evaluate(cdp, '!document.querySelector("iframe[src*=audio_capture_frame]")', isolated)


def test_restored_pre_activation_embeddings_preserve_native_scores(preview_browser):
    context, worker, _, _, _ = preview_browser
    page = context.new_page()
    page.goto(worker.url.rsplit('/', 1)[0] + '/audio_capture_frame.html')
    result = page.evaluate('''async () => {
      const worker = new Worker('audio_model/embedding_probe.js');
      try {
        return await new Promise((resolve,reject)=>{
          const timer=setTimeout(()=>reject(Error('Embedding parity timed out')),30000);
          worker.onmessage=({data})=>{clearTimeout(timer);resolve(data)};
          worker.onerror=event=>{clearTimeout(timer);reject(Error(event.message))};
          worker.postMessage({});
        });
      } finally {worker.terminate()}
    }''')
    assert 'error' not in result, result
    assert result['tag_error'] == 0
    assert result['relu_error'] <= 1e-5
    assert result['embedding_shape'] == [1, 200]
    assert result['embedding_min'] < 0, 'The mood heads need pre-activation values, including negative features'


def test_native_attributes_match_known_levels_and_rhythm_without_audio_output(preview_browser):
    context, worker, _, _, _ = preview_browser
    page = context.new_page()
    page.goto(worker.url.rsplit('/', 1)[0] + '/audio_capture_frame.html')
    result = page.evaluate('''async () => {
      const worker = new Worker('audio_model/attributes_probe.js');
      try {
        return await new Promise((resolve,reject)=>{
          const timer=setTimeout(()=>reject(Error('Attribute controls timed out')),30000);
          worker.onmessage=({data})=>{clearTimeout(timer);resolve(data)};
          worker.onerror=event=>{clearTimeout(timer);reject(Error(event.message))};
          worker.postMessage({});
        });
      } finally {worker.terminate()}
    }''')
    assert 'error' not in result, result
    tone, half, stereo, silence, beat = (result[key] for key in ('tone', 'half', 'stereo', 'silence', 'beat'))
    # A mono 1 kHz sine at -23.01 dBFS reads approximately -23.05 LUFS.
    assert tone['integrated_lufs'] == pytest.approx(-23.05, abs=.15)
    assert half['integrated_lufs'] - tone['integrated_lufs'] == pytest.approx(-6.0206, abs=.02)
    assert stereo['integrated_lufs'] - tone['integrated_lufs'] == pytest.approx(3.0103, abs=.02)
    assert tone['rms'] == pytest.approx(.1 / 2 ** .5, abs=.0001)
    assert tone['spectral_centroid_hz'] == pytest.approx(1000, abs=50)
    assert tone['zero_crossings_per_second'] == pytest.approx(2000, abs=1)
    assert half['peak_normalized_rms_dbfs'] == pytest.approx(tone['peak_normalized_rms_dbfs'], abs=.001)
    assert half['spectral_centroid_hz'] == pytest.approx(tone['spectral_centroid_hz'], abs=.01)
    assert silence['rms_dbfs'] is None and silence['spectral_centroid_hz'] is None
    assert beat['tempo_bpm'] == pytest.approx(120, abs=4)
    assert 0 <= beat['tempo_confidence'] <= 5.33
    assert beat['beat_count'] >= 10
    for values in result.values():
        assert len(values) == 27
        assert all(value is None or math.isfinite(value) for value in values.values())


@pytest.mark.parametrize('preview_browser,verified', [
    pytest.param('no_activation', True, id='no_activation'),
    pytest.param('normal_playback', True, id='normal_playback'),
    pytest.param('packaged', False, id='packaged'),
], indirect=['preview_browser'])
def test_app_automatically_captures_and_runs_model_without_capture_click(live_app, preview_browser, tmp_path, verified):
    context, worker, source, source_cdp, isolated = preview_browser
    source_activated = _evaluate(source_cdp, 'navigator.userActivation.hasBeenActive')
    page = context.new_page()
    page.add_init_script('''window.Worker=class {constructor(){throw Error('Audio inference must stay in the YouTube tab');}};''')
    cdp = context.new_cdp_session(page)
    state = mock_playback(page)
    state['metadata_verified'] = verified
    errors, uploads, model_workers, app_assets, source_errors = [], [], [], [], []
    source.on('pageerror', lambda error: source_errors.append(str(error)))
    source.on('console', lambda message: source_errors.append(message.text) if message.type == 'error' else None)
    source.on('worker', lambda worker: model_workers.append(worker.url))
    page.on('request', lambda request: app_assets.append(request.url)
        if '/audio-preview/assets/' in request.url else None)
    page.on('pageerror', lambda error: errors.append(str(error)))
    context.on('request', lambda request: uploads.append((request.url, request.post_data or ''))
        if request.method == 'POST' else None)
    page.goto(live_app + '/audio-preview')
    if not source_activated:
        # Keep the muted zero-activation control visible; the separate normal
        # unmuted case stays behind the app and verifies background music capture.
        source.bring_to_front()
    # Do not use Playwright DOM helpers/evaluation until capture+inference finish;
    # those helpers can create debugger user activation.
    try:
        ready = _wait(cdp, '''(() => {
          if(document.querySelector('[data-preview-result]')?.hidden===false) return {ready:true};
          const status=document.querySelector('[data-preview-status]')?.textContent || '';
          if(/failed|timed out|changed|Invalid|artifact|exceeded|not found|Unsupported/i.test(status)) return {error:status};
        })()''', timeout=50)
        assert ready == {'ready': True}, ready
    except BaseException:
        print('App script errors:', errors)
        print('Source state:', _evaluate(source_cdp, '({paused:document.querySelector("video").paused,time:document.querySelector("video").currentTime,activation:navigator.userActivation.hasBeenActive})'))
        print('Decoded buffers:', _evaluate(source_cdp, 'previewPCM.map(p=>({seconds:p.duration,rate:p.sampleRate,channels:p.numberOfChannels}))', isolated))
        print('Recordings:', _evaluate(source_cdp, 'previewRecordings', isolated))
        print('Source errors:', source_errors)
        raise
    if not source_activated:
        assert _evaluate(cdp, 'navigator.userActivation.hasBeenActive') is False
    expect(page.locator('[data-preview-tags] li')).to_have_count(10)
    expect(page.locator('[data-preview-moods] li')).to_have_count(6)
    expect(page.locator('[data-preview-status]')).to_have_text('Preview ready. Your music keeps playing.')
    with page.expect_download() as download:
        page.get_by_role('button', name='Export result').click()
    payload = json.loads(Path(download.value.path()).read_text())
    assert payload['capture_seconds'] == 10 and payload['sample_count'] == 160000
    assert payload['descriptors']['rms'] == pytest.approx(.1 / 2**.5, abs=.01)
    assert payload['descriptors']['zero_crossings_per_second'] == pytest.approx(880, abs=20)
    assert len(payload['prediction']['scores']) == 50
    embedding = payload['prediction']['embedding']
    assert embedding['version'] == 'musicnn-embedding-1'
    assert embedding['layer'] == 'model/dense/BiasAdd'
    assert embedding['pooling'] == 'mean_full_patches' and embedding['patches'] == 3
    assert len(embedding['values']) == 200
    assert all(math.isfinite(value) for value in embedding['values'])
    assert min(embedding['values']) < 0
    assert payload['prediction']['runtime_version'] == 'essentia-csp-audio-2'
    assert len(payload['audio_attributes']['values']) == 27
    assert payload['analysis_location'] == 'youtube_tab' and payload['audio_transferred_to_app'] is False
    assert payload['prediction']['status'] == 'native_tags_and_moods'
    traits = payload['prediction']['moods']['traits']
    assert set(traits) == {'relaxed', 'aggressive', 'happy', 'sad', 'arousal', 'valence'}
    for name, trait in traits.items():
        assert len(trait['per_patch']) == 3
        assert trait['score'] == pytest.approx(sum(trait['per_patch']) / 3)
        assert math.isfinite(trait['score'])
        assert trait['range'] == ([1, 9] if name in {'arousal', 'valence'} else [0, 1])
        if trait['range'] == [0, 1]:
            assert 0 <= trait['score'] <= 1
    assert len(payload['prediction']['moods']['models']) == 5
    assert payload['prediction']['moods']['reliability'] is None
    assert model_workers == ['chrome-extension://edhplcadobipneepllhkkajckoammpnk/audio_model/model_worker.js']
    assert app_assets == []
    assert payload['observed_playback']['video_id'] == 'preview_song'
    assert payload['observed_playback']['metadata_verified'] is verified
    if not verified:
        expect(page.locator('[data-preview-summary]')).to_contain_text('title and artist are unverified')
    assert payload['source_identity_verified'] is False and payload['recording_association'] is None
    assert payload['recommendation_adoption'] is False and payload['raw_audio_retained'] is False
    assert payload['capture_method'] == 'HTMLMediaElement.captureStream'
    assert payload['trigger'] == 'automatic' and payload['realtime_audio_context_used'] is False
    assert payload['user_activation_at_start']['has_been_active'] is source_activated
    if not source_activated:
        assert payload['user_activation_at_start']['is_active'] is False
    assert 'pcm' not in payload
    outgoing = _evaluate(source_cdp, 'previewMessages', isolated)
    assert not any('pcm' in message or 'samples' in message or 'embeddings' in message for message in outgoing)
    assert [message['type'] for message in outgoing][-1] == 'result'
    for url, body in uploads:
        assert len(body) <= 65536 and '"pcm"' not in body
        if url.startswith(live_app):
            # The shared collection observer reports bounded state even on
            # the diagnostic page; preview still uploads no captured audio.
            assert url == live_app + '/audio-vibes/diagnostics'
            from yt_playlist.web.routes.audio_vibes import BrowserDiagnostic
            diagnostic = BrowserDiagnostic.model_validate_json(body)
            assert all(not isinstance(value, (list, dict))
                       for value in diagnostic.model_dump().values())
    _released(source_cdp, isolated)
    assert _evaluate(source_cdp, 'typeof window.__tcAudioProbe === "undefined"', isolated) is True
    assert _evaluate(source_cdp, '!document.querySelector("video").paused')
    page.screenshot(path=str(tmp_path / 'audio-preview-app.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors


def test_preview_recovers_media_readiness_and_reports_a_persistent_failure(live_app, preview_browser):
    context, _, source, source_cdp, isolated = preview_browser
    # Controlled readiness change on real playing media in the capture world.
    _evaluate(source_cdp, '''Object.defineProperty(document.querySelector('video'),'readyState',{
      configurable:true,get(){return 1}
    })''', isolated)
    page = context.new_page()
    mock_playback(page)
    page.goto(live_app + '/audio-preview')
    _wait(source_cdp, 'previewMessages.some(message=>message.type==="waiting")', context_id=isolated)
    assert _evaluate(source_cdp, 'previewStreams.length', isolated) == 0
    _evaluate(source_cdp, 'delete document.querySelector("video").readyState', isolated)
    _wait(source_cdp, 'previewStreams.length===1', context_id=isolated)
    page.get_by_role('button', name='Stop analysis').click()
    _wait(source_cdp, '!window.__tcAudioCapture.inspect().running', context_id=isolated)
    _released(source_cdp, isolated)

    _evaluate(source_cdp, '''Object.defineProperty(document.querySelector('video'),'readyState',{
      configurable:true,get(){return 1}
    })''', isolated)
    page.get_by_role('button', name='Analyse again').click()
    expect(page.locator('[data-preview-status]')).to_contain_text('music_media_unavailable', timeout=10000)
    expect(page.locator('[data-preview-status]')).to_contain_text('ready=1')
    expect(page.locator('[data-preview-status]')).to_contain_text('tab=')
    assert _evaluate(source_cdp, 'previewStreams.length', isolated) == 1
    _released(source_cdp, isolated)
    assert _evaluate(source_cdp, '!document.querySelector("video").paused')


@pytest.mark.parametrize('end', ['stop', 'track', 'pause', 'seek', 'close', 'source', 'analysis', 'decode', 'reload', 'frame'])
def test_automatic_preview_cancels_and_erases_pcm(live_app, preview_browser, end, tmp_path):
    context, worker, source, source_cdp, isolated = preview_browser
    page = context.new_page()
    page.set_viewport_size({'width': 1280, 'height': 900})
    state = mock_playback(page)
    if end == 'frame':
        _evaluate(source_cdp, '''(() => {
          const observer=new MutationObserver(()=>{
            const frame=document.querySelector('iframe[src*=audio_capture_frame]');
            if(frame){observer.disconnect();frame.src='https://music.youtube.com/counterfeit-frame';}
          });
          observer.observe(document.documentElement,{childList:true});
        })()''')
    elif end == 'decode':
        _evaluate(source_cdp, '''(() => {
          window.__tcAudioProbe?.stop();
          const Offline=OfflineAudioContext;
          window.OfflineAudioContext=class extends Offline {
            async decodeAudioData(...args) {
              const pcm=await super.decodeAudioData(...args);
              await new Promise(resolve=>{window.releasePreviewDecode=resolve;});
              return pcm;
            }
          };
        })()''', isolated)
    page.goto(live_app + '/audio-preview')
    expect(page.locator('[data-preview-status]')).to_contain_text('Listening automatically', timeout=10000)
    if end == 'frame':
        expect(page.locator('[data-preview-status]')).to_contain_text('local analysis frame changed', timeout=15000)
    elif end == 'decode':
        _wait(source_cdp, '!!window.releasePreviewDecode', context_id=isolated)
        page.get_by_role('button', name='Stop analysis').click()
        # Resolve only after the Stop message has disconnected the source.
        _wait(source_cdp, '!window.__tcAudioCapture.inspect().running', context_id=isolated)
        _evaluate(source_cdp, 'releasePreviewDecode()', isolated)
        _wait(source_cdp, 'previewPCM.every(p=>p.getChannelData(0).every(x=>x===0))', context_id=isolated)
    elif end == 'analysis':
        expect(page.locator('[data-preview-status]')).to_contain_text('Analysing inside the YouTube Music tab', timeout=15000)
        page.get_by_role('button', name='Stop analysis').click()
    elif end == 'stop':
        page.get_by_role('button', name='Stop analysis').click()
    elif end == 'track':
        # A paused new track cancels the old one without immediately capturing it.
        _evaluate(source_cdp, '''window.postMessage({__tcSensorVersion:4,__tcSensorRevision:8,
          __tcNow:{videoId:'next_song',title:'Next song',paused:true}},'*')''')
        state.update(video_id='next_song', paused=True)
        page.evaluate('playbackStatus.refresh()')
    elif end == 'source':
        _evaluate(source_cdp, 'document.querySelector("video").src="/another.wav"')
    elif end == 'pause':
        _evaluate(source_cdp, 'document.querySelector("video").pause()')
        expect(page.locator('[data-preview-status]')).to_contain_text('Playback paused before the excerpt finished.')
    elif end == 'seek':
        _evaluate(source_cdp, 'document.querySelector("video").currentTime=15')
        expect(page.locator('[data-preview-status]')).to_contain_text('Playback was moved during the excerpt.')
    elif end == 'reload':
        worker.evaluate('setTimeout(()=>chrome.runtime.reload(),10)')
    else:
        page.close()
    _wait(source_cdp, 'previewStreams.every(s=>s.getTracks().every(t=>t.readyState==="ended"))', context_id=isolated)
    _released(source_cdp, isolated)
    if end != 'close':
        expect(page.get_by_role('button', name='Stop analysis')).to_be_disabled()
        expect(page.locator('[data-preview-result]')).to_be_hidden()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if end == 'stop':
            page.screenshot(path=str(tmp_path / 'audio-preview-desktop.png'), full_page=True)
    if end not in ('source', 'pause'):
        assert _evaluate(source_cdp, '!document.querySelector("video").paused')


@pytest.mark.parametrize('preview_browser', ['tampered_head'], indirect=True)
def test_changed_mood_weights_fail_closed_and_release_audio(live_app, preview_browser):
    context, _, _, cdp, isolated = preview_browser
    page = context.new_page()
    mock_playback(page)
    page.goto(live_app + '/audio-preview')
    expect(page.locator('[data-preview-status]')).to_contain_text(
        'Audio artifact changed: relaxed/group1-shard1of1.bin', timeout=25000)
    expect(page.locator('[data-preview-result]')).to_be_hidden()
    expect(page.get_by_role('button', name='Stop analysis')).to_be_disabled()
    _released(cdp, isolated)


def test_two_previews_do_not_overlap_capture_and_untrusted_page_is_rejected(live_app, preview_browser):
    context, worker, source, cdp, isolated = preview_browser
    first, second = context.new_page(), context.new_page()
    mock_playback(first)
    mock_playback(second)
    first.goto(live_app + '/audio-preview')
    expect(first.locator('[data-preview-status]')).to_contain_text('Listening automatically')
    second.goto(live_app + '/audio-preview')
    expect(second.locator('[data-preview-status]')).to_contain_text('Another audio capture')
    first.get_by_role('button', name='Stop analysis').click()
    # All normal app pages now support automatic vibe collection. Reject a
    # different port even when it matches the manifest's broader origin list.
    rejected = context.new_page()
    rejected.route('http://127.0.0.1:8766/**', lambda route: route.fulfill(
        content_type='text/html', body='<h1>Untrusted local app</h1>'))
    rejected.goto('http://127.0.0.1:8766/')
    assert rejected.evaluate('''() => new Promise(resolve=> {
      const port=chrome.runtime.connect('edhplcadobipneepllhkkajckoammpnk',{name:'tc-audio-preview'});
      port.onMessage.addListener(()=>resolve(false));
      port.onDisconnect.addListener(()=>{void chrome.runtime.lastError;resolve(true)});
    })''')
    _wait(cdp, 'previewStreams.every(s=>s.getTracks().every(t=>t.readyState==="ended"))', context_id=isolated)
    _released(cdp, isolated)
