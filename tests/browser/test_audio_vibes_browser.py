"""Background sampling, completed-song cards and durable click-to-label judgments."""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from playwright.sync_api import expect
import pytest

from tests.browser.test_audio_preview_browser import EXT, preview_browser, _evaluate, _released, _wait
from tests.browser.test_app_update_browser import update_card
from tests.test_audio_vibes import sample
from yt_playlist.rec import home_vibe
from yt_playlist.core import audio_embedding
from yt_playlist.rec import audio_vibe_model
from experiments.audio_lab_tools import train_vibes

DESCRIPTIONS = {key: preset['attributes'] for key, preset in home_vibe.PRESETS.items()}
RUN = 'tuneconsole.audio-vibes.run.1'
VIEW = 'tuneconsole.audio-vibes.view.1'
CARDS = 'tuneconsole.audio-vibes.cards.1'


@pytest.fixture
def vibes_enabled(monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')


@pytest.fixture
def vibe_browser(vibes_enabled, preview_browser):
    return preview_browser


def playback_route(route, state):
    route.fulfill(json={'connected': True, 'now_playing': {
        'video_id': 'vibe_song', 'title': 'Song for a vibe check', 'artist': 'Observed artist',
        'metadata_verified': False, 'paused': False, 'snapshot_age': 0, **state}})


def fake_result():
    value = sample(electronic=.95, House=.9, dance=.8)
    return {'analysis_location': 'youtube_tab', 'audio_transferred_to_app': False,
        'audio_attributes': value['audio_attributes'], 'source_position_at_start': 30,
        'song_position_at_start': 30,
        'raw_audio_retained': False,
        'prediction': {'scores': value['tags'], 'analysed_seconds': 8.992,
            'model': value['model'], 'model_version': value['model_version'],
            'bundle': {'files': []}, 'moods': {'traits': value['traits'],
                'models': {key: {'version': version} for key, version in value['mood_versions'].items()}}}}


def fake_extension(page, fail_capture=False, result=None, initially_stale=False):
    # Controlled source/transport for card tests. The installed-extension cases
    # below independently establish document-free lifetime and native capture.
    owner_source = (EXT / 'worker/audio_sampler.js').read_text() + '\n' + (
        EXT / 'worker/audio_collection.js').read_text()
    page.add_init_script('(() => {\n' + owner_source + '\n' + '''
      window.vibeCaptures=[];
      window.vibeCollectionMessages=[];
      window.vibeExtensionReady=!INITIALLY_STALE;
      window.Worker=class {constructor(){throw Error('No app-side inference');}};
      window.AudioContext=class {constructor(){throw Error('No audio output');}};
      navigator.mediaDevices.getDisplayMedia=()=>{throw Error('No sharing dialog');};
      navigator.mediaDevices.getUserMedia=()=>{throw Error('No microphone');};
      window.chrome ||= {};
      chrome.runtime={connect() {
        const listeners=[],disconnects=[];
        const port={onMessage:{addListener(fn){listeners.push(fn)}},
          onDisconnect:{addListener(fn){disconnects.push(fn)}},
          disconnect(){disconnects.forEach(fn=>fn())},postMessage(message) {
            if(message.type!=='capture') return;
            if (message.not_before && (playbackStatus.audioSnapshot?.now_playing?.position ??
                playbackStatus.snapshot?.now_playing?.position ?? 0) < message.not_before) {
              const waiting = setInterval(() => {
                if ((playbackStatus.audioSnapshot?.now_playing?.position ??
                    playbackStatus.snapshot?.now_playing?.position ?? 0) >= message.not_before) {
                  clearInterval(waiting); this.postMessage({...message, not_before:null});
                }
              }, 50);
              return;
            }
            vibeCaptures.push(message);
            if (!FAIL_CAPTURE || vibeCaptures.length > 1)
              setTimeout(()=>listeners.forEach(fn=>fn({id:message.id,type:'analysing'})),10);
            const result=FAIL_CAPTURE && vibeCaptures.length===1
              ? {id:message.id,error:'The excerpt could not be decoded.'}
              : {id:message.id,type:'result',result:{...RESULT,
                song_position_at_start: playbackStatus.audioSnapshot?.now_playing?.position ??
                  playbackStatus.snapshot?.now_playing?.position ?? 30,
                source_position_at_start: playbackStatus.audioSnapshot?.now_playing?.position ??
                  playbackStatus.snapshot?.now_playing?.position ?? 30}};
            setTimeout(()=>listeners.forEach(fn=>fn(result)),60);
          }};
        setTimeout(()=>window.vibeExtensionReady
          ? listeners.forEach(fn=>fn({type:'ready'})) : port.disconnect(),0);
        return port;
      }};
      const sourceConnect=chrome.runtime.connect;
      let observerConnected=false;
      const storageKey='fixture.extension.audio-storage';
      const capture={previewOpen:false,allowed:()=>true,onCollectionChanged(){},
        openCollection(receive){const source=sourceConnect();source.onMessage.addListener(receive);
          queueMicrotask(()=>receive({type:'ready'}));return source;}};
      const storage={get:async()=>JSON.parse(localStorage.getItem(storageKey)||'{}'),
        set:async value=>localStorage.setItem(storageKey,JSON.stringify(value))};
      const worker=createAudioCollection({appOrigin:location.origin,capture,storage});
      function observe() {
        if(!window.vibeExtensionReady || !observerConnected) return;
        const snapshot=window.playbackStatus?.audioSnapshot ?? window.playbackStatus?.snapshot;
        const playing=snapshot?.connected && snapshot.now_playing;
        if(playing) worker.observe({type:'now-heartbeat',videoId:playing.video_id,
          title:playing.title,artist:playing.artist,metadataVerified:playing.metadata_verified,
          paused:playing.paused,position:playing.position,duration:playing.duration,
          muted:playing.muted,tabMuted:playing.tab_muted,volume:playing.volume});
      }
      window.addEventListener('bridge-status',observe);
      setInterval(observe,500);
      chrome.runtime.connect=(_id,options)=>{
        if(options.name!=='tc-audio-collection') return sourceConnect();
        const listeners=[],commands=[],disconnects=[];
        const external={name:'tc-audio-collection',sender:{},postMessage:data=>{
          // Exercise an older compatible owner's actual diagnostic payload at
          // the app boundary while keeping real capture, outbox and delivery.
          if (sessionStorage.getItem('fixture.legacy-collection-status') === '1' &&
              data.type==='collection' && data.status?.phase==='unavailable')
            data={...data,status:{...data.status,message:'TuneConsole rejected an audio check (HTTP 422). Captured checks remain in the extension; update TuneConsole before retrying.'}};
          vibeCollectionMessages.push(structuredClone(data));
          listeners.forEach(fn=>fn(data));},
          onMessage:{addListener:fn=>commands.push(fn)},onDisconnect:{addListener:fn=>disconnects.push(fn)}};
        const port={postMessage:data=>commands.forEach(fn=>fn(data)),
          onMessage:{addListener:fn=>listeners.push(fn)},onDisconnect:{addListener:fn=>disconnects.push(fn)},
          disconnect(){disconnects.forEach(fn=>fn())}};
        setTimeout(()=>{
          if(window.vibeExtensionReady) {observerConnected=true;worker.connect(external);}
          else port.disconnect();
        },0);
        return port;
      };
    })();
    '''.replace('INITIALLY_STALE', json.dumps(initially_stale))
        .replace('FAIL_CAPTURE', json.dumps(fail_capture)).replace('RESULT', json.dumps(result or fake_result())))


def wait_samples(page, count, timeout=15000):
    page.wait_for_function('''count => JSON.parse(localStorage.getItem(
      'tuneconsole.audio-vibes.view.1'))?.samples.length === count''', arg=count, timeout=timeout)


def test_stale_extension_explains_zero_checks_and_recovers_after_reload(vibes_enabled, live_app, page, store):
    fake_extension(page, initially_stale=True)
    page.route('**/bridge/status', lambda route: playback_route(route, {'position': 35, 'duration': 240}))
    page.goto(live_app + '/playlists')
    expect(page.locator('[data-vibe-card]:visible [data-vibe-prediction]')).to_have_text(
        'Audio checks are paused. You can still label this song.')
    assert not page.evaluate('vibeCaptures')
    assert not store.conn.execute('SELECT 1 FROM sample_attributes').fetchone()
    page.evaluate('window.vibeExtensionReady=true')
    wait_samples(page, 1)
    expect(page.locator('[data-vibe-card]:visible [data-vibe-prediction]')).to_have_text('1 of 3 audio checks complete')
    assert len(page.evaluate('vibeCaptures')) == 1


def test_rejected_collection_keeps_quiet_zero_check_labels_and_durable_delivery(
        vibes_enabled, live_app, page, store, record_property):
    page.set_viewport_size({'width': 1440, 'height': 1000})
    fake_extension(page)
    page.route('**/bridge/status', lambda route: playback_route(route, {'position': 35, 'duration': 240}))
    rejected = True
    saves, answers, assets = [], [], []
    page.on('response', lambda response: assets.append(response)
            if '/static/audio-vibe-' in response.url and '.js' in response.url else None)
    page.on('request', lambda request: answers.append(request.post_data_json)
            if request.url.endswith('/audio-vibes/feedback') else None)

    def deliver(route):
        saves.append(route.request.post_data_json)
        if rejected:
            route.fulfill(status=422, json={'detail': 'Controlled metadata delivery refusal'})
        else:
            route.continue_()

    page.route('**/audio-vibes/samples', deliver)
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    page.wait_for_function('''() => vibeCollectionMessages.some(message =>
      message.status?.phase === 'unavailable')''')
    expect(card.locator('[data-vibe-track]')).to_have_text('Song for a vibe check')
    expect(card.locator('[data-vibe-prediction]')).to_have_text(
        'Audio checks are paused. You can still label this song.')
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    expect(card.get_by_role('link', name='Update extension')).to_be_hidden()
    assert_quiet_audio_card(page)
    run_id = card.get_attribute('data-run-id')
    queued = page.evaluate('''() => JSON.parse(localStorage.getItem(
      'fixture.extension.audio-storage'))['tuneconsole.audio-collection.1'].outbox''')
    assert len(queued) == 1 and queued[0]['run_id'] == run_id
    card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    answer = store.audio_vibes.feedback(run_id)
    assert answer['vibes'] == ['relax'] and answer['sample_ids'] == []
    assert not store.audio_vibes.samples(run_id)
    card.evaluate('node => Promise.all(node.getAnimations().map(animation => animation.finished))')
    page.screenshot(path='/tmp/tuneconsole-879-audio-collection-current.png')

    # Keep the same pending capture while the next document observes the
    # compatible older owner's diagnostic payload at the app boundary.
    page.evaluate("sessionStorage.setItem('fixture.legacy-collection-status', '1')")
    page.reload()
    page.wait_for_function('''() => vibeCollectionMessages.some(message =>
      message.status?.phase === 'unavailable')''')
    assert visible_card(page).get_attribute('data-run-id') == run_id
    assert page.evaluate('''() => vibeCollectionMessages.some(message =>
      message.protocol === 1 && message.status?.message.includes('HTTP 422'))''')
    expect(visible_card(page).locator('[data-vibe-prediction]')).to_have_text(
        'Audio checks are paused. You can still label this song.')
    expect(visible_card(page).locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    expect(visible_card(page).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute(
        'aria-pressed', 'true')
    assert page.evaluate('''() => JSON.parse(localStorage.getItem(
      'fixture.extension.audio-storage'))['tuneconsole.audio-collection.1'].outbox''') == queued
    assert_quiet_audio_card(page)
    assert len(answers) == 1 and store.audio_vibes.feedback(run_id) == answer
    assert page.evaluate('vibeCaptures.length') == 0
    visible_card(page).evaluate('node => Promise.all(node.getAnimations().map(animation => animation.finished))')
    page.screenshot(path='/tmp/tuneconsole-879-audio-collection-compatible.png')
    rejected = False
    wait_samples(page, 1)
    expect(visible_card(page).locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(1)
    page.wait_for_function('''() => JSON.parse(localStorage.getItem(
      'fixture.extension.audio-storage'))['tuneconsole.audio-collection.1'].outbox.length === 0''')
    assert [value['id'] for value in store.audio_vibes.samples(run_id)] == [queued[0]['id']]
    assert all(value == queued[0] for value in saves)
    assert page.evaluate('vibeCaptures.length') == 0, 'Reload must deliver the retained check without recapturing'
    assert len(answers) == 1 and store.audio_vibes.feedback(run_id) == answer
    assert_quiet_audio_card(page)
    page.screenshot(path='/tmp/tuneconsole-879-audio-collected.png')
    static = Path(__file__).resolve().parents[2] / 'src/yt_playlist/web/static'
    loaded = {}
    for name in ('audio-vibe-collection.js', 'audio-vibe-cards.js'):
        response = next(response for response in reversed(assets) if name + '?' in response.url)
        digest = hashlib.sha256(response.body()).hexdigest()
        assert digest == hashlib.sha256((static / name).read_bytes()).hexdigest()
        loaded[name] = {'url': response.url, 'sha256': digest}
    record_property('quiet_audio_collection', json.dumps({'assets': loaded,
        'diagnostics': ['current', 'compatible'], 'sample_id': queued[0]['id'],
        'deliveries': len(saves), 'answers': len(answers)}))


def test_normal_collection_predicts_and_propagates_without_rating_ui(live_app, page, store, monkeypatch):
    from tests.test_audio_vibe_embeddings import with_embedding
    from tests.rec.test_vibe_prediction import drain, fits, track
    from yt_playlist.rec import (audio_vibe_cruise, audio_vibe_energize, audio_vibe_focus,
                                 audio_vibe_relax, audio_vibe_release, audio_vibe_uplift)

    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
    monkeypatch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(audio_vibe_model.DEFAULT_PATH))
    specialists = {'cruise': audio_vibe_cruise, 'energize': audio_vibe_energize,
                   'concentrate': audio_vibe_focus, 'relax': audio_vibe_relax,
                   'release': audio_vibe_release, 'uplift': audio_vibe_uplift}
    for specialist in specialists.values():
        monkeypatch.setenv(specialist.ENVIRONMENT_KEY, str(specialist.DEFAULT_PATH))
        monkeypatch.setenv(specialist.MODE_KEY, 'active')
    _tid, measured = track(store, 'vibe_song', artist='Observed artist')
    _tid, neighbour = track(store, 'unplayed_song', artist='Observed artist')
    value = with_embedding(sample())
    value['artifact_hashes'] = {
        'musicnn/model.json': '2df238355538b680e70f06e30fb04df52f133da6ea1915530ec8c590da073762',
        'musicnn/group1-shard1of1.bin': '7ebfb970c7389b6276ffc34f0d7fc80ba2765e5310829f3618ad5b977326667d'}
    result = fake_result()
    result['prediction']['embedding'] = value['embedding']
    result['prediction']['bundle']['files'] = [
        {'path': key, 'sha256': digest} for key, digest in value['artifact_hashes'].items()]
    fake_extension(page, result=result)
    # A stale developer click queue must remain dormant when ratings are off.
    page.add_init_script('localStorage.setItem(' + json.dumps(CARDS) + ', '
                         + json.dumps(json.dumps([{'id': str(uuid4()), 'dirty': True, 'draft': ['release']}])) + ');')
    state = {'position': 30, 'duration': 240}
    page.route('**/bridge/status', lambda route: playback_route(route, state))
    urls = []
    page.on('request', lambda request: urls.append(request.url))
    page.goto(live_app + '/')
    wait_samples(page, 1)
    expect(page.locator('[data-audio-vibe-banner]')).to_have_count(0)
    expect(page.locator('[data-vibe-choice]')).to_have_count(0)
    state['position'] = 120
    wait_samples(page, 2)
    page.goto(live_app + '/playlists')
    state['position'] = 160
    wait_samples(page, 3)
    run_id = page.evaluate('JSON.parse(localStorage.getItem(' + json.dumps(RUN) + ')).id')
    samples = store.audio_vibes.samples(run_id)
    assert len(samples) == 3
    # Each bundled specialist owns its target and aggregation contract. The
    # retired shared model's six outputs no longer describe production scores.
    excerpts = [sample['embedding']['values'] for sample in samples]
    expected = {vibe: specialist.score(specialist.load(specialist.DEFAULT_PATH), excerpts)['score']
                for vibe, specialist in specialists.items()}
    drain(store)
    assert {key: fit['score'] for key, fit in fits(store, measured).items()} == pytest.approx(expected)
    assert {fit['source'] for fit in fits(store, measured).values()} == {'audio_measurement'}
    assert {fit['source'] for fit in fits(store, neighbour).values()} == {'artist_genre'}
    assert all(fit['recordings'] == 1 for fit in fits(store, neighbour).values())
    assert not store.conn.execute('SELECT 1 FROM audio_annotations').fetchone()
    assert not any(url.endswith('/audio-vibes/feedback') or '/audio-vibe-cards.js' in url
                   or '/audio-vibe-banner.js' in url for url in urls)


# Source native inference is independently exercised by natural_offset below;
# packaged bytes also prove no-app delivery, restart and disabled-rating safety.
@pytest.mark.parametrize('preview_browser', ['packaged_sampling'], indirect=True)
def test_installed_extension_collects_native_embedding_with_ratings_disabled(
        preview_browser, live_app, store, monkeypatch, request, record_property):
    from fastapi import HTTPException

    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
    journey_started = phase_started = time.perf_counter()
    context, worker, source, source_cdp, isolated = preview_browser
    delivery = {'offline': True, 'attempts': []}
    save_sample = store.audio_vibes.save_sample

    def deliver(value, now):
        delivery['attempts'].append(value['id'])
        if delivery['offline']:
            raise HTTPException(503, 'Controlled metadata delivery outage')
        return save_sample(value, now)

    monkeypatch.setattr(store.audio_vibes, 'save_sample', deliver)
    context.route('**/bridge/status', lambda route: playback_route(route, {
        'position': _evaluate(source_cdp, 'previewSongPosition()'), 'duration': 195}))
    page = context.new_page()
    errors, loaded = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: loaded.append(request.url))
    page.add_init_script('window.Worker=class {constructor(){throw Error("No app-side inference");}};')
    # Start in preview, then use the real intercepted links in both directions.
    # Preview preempts the collector's future reservation without a second owner.
    page.goto(live_app + '/audio-preview')
    page.evaluate('window.originalDocument=document')
    expect(page.locator('[data-preview-status]')).to_contain_text('Listening automatically', timeout=15000)
    page.locator('main').get_by_role('link', name='On Air', exact=True).click()
    expect(page.locator('[data-audio-preview]')).to_have_count(0)
    page.get_by_role('button', name='Tools', exact=True).click()
    page.get_by_role('link', name='Audio analysis preview', exact=True).click()
    expect(page.locator('[data-preview-status]')).to_contain_text('Listening automatically', timeout=15000)
    page.get_by_role('link', name='Playlists', exact=True).first.click()
    expect(page.locator('[data-audio-preview]')).to_have_count(0)
    assert page.evaluate('window.originalDocument===document')
    expect(page.locator('[data-audio-vibe-collection]')).to_have_count(1)
    _released(source_cdp, isolated)
    source.bring_to_front()
    # Close every app document while the native thirty-second excerpt is still
    # recording. The extension must finish and retain its metadata independently.
    _wait(source_cdp, 'previewStreams.some(stream=>stream.getTracks().some(track=>track.readyState==="live"))',
          timeout=40, context_id=isolated)
    page.close()
    record_property('native_preview_navigation_and_recording_start_seconds', time.perf_counter() - phase_started)
    phase_started = time.perf_counter()
    key = 'tuneconsole.audio-collection.1'
    deadline = time.monotonic() + 35
    pending = None
    while time.monotonic() < deadline:
        pending = worker.evaluate('async key=>(await chrome.storage.local.get(key))[key]', key)
        if pending['outbox'] and delivery['attempts']:
            break
        source.wait_for_timeout(100)
    assert pending and len(pending['outbox']) == 1 and delivery['attempts']
    record_property('native_recording_inference_and_rejected_delivery_seconds', time.perf_counter() - phase_started)
    queued = pending['outbox'][0]
    run_id = queued['run_id']
    assert not store.audio_vibes.samples(run_id)
    assert all(candidate.url.startswith('https://music.youtube.com') or candidate.url == 'about:blank'
               for candidate in context.pages), [candidate.url for candidate in context.pages]

    # Stop this fixture's real service worker, then let the music-tab heartbeat
    # wake it again. Its stored sample UUID must survive the new worker instance.
    phase_started = time.perf_counter()
    worker_url = worker.url
    versions = []
    source_cdp.on('ServiceWorker.workerVersionUpdated', lambda event: versions.extend(
        value['runningStatus'] for value in event['versions'] if value['scriptURL'] == worker_url))
    source_cdp.send('ServiceWorker.enable')
    marker = worker.evaluate('globalThis.__collectionRestartMarker = crypto.randomUUID()')
    targets = source_cdp.send('Target.getTargets')['targetInfos']
    target, = [value for value in targets if value['type'] == 'service_worker' and value['url'] == worker_url]
    assert source_cdp.send('Target.closeTarget', {'targetId': target['targetId']})['success']
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        # Chromium can reuse the target ID, and Playwright then retains its
        # Worker object without emitting another serviceworker event. Require
        # the real stopped/running transition and a fresh JS global instead.
        if 'stopped' in versions and 'running' in versions[versions.index('stopped') + 1:]:
            break
        source.wait_for_timeout(100)
    assert 'stopped' in versions and 'running' in versions[versions.index('stopped') + 1:], versions
    replacement, = [value for value in context.service_workers if value.url == worker_url]
    assert replacement.evaluate('globalThis.__collectionRestartMarker') != marker
    source_cdp.send('ServiceWorker.disable')
    restored = replacement.evaluate('async key=>(await chrome.storage.local.get(key))[key]', key)
    assert [value['id'] for value in restored['outbox']] == [queued['id']]
    record_property('native_worker_restart_seconds', time.perf_counter() - phase_started)
    phase_started = time.perf_counter()
    delivery['offline'] = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not store.audio_vibes.samples(run_id):
        source.wait_for_timeout(100)
    values = store.audio_vibes.samples(run_id)
    assert len(values) == 1
    # Server storage alone cannot prove that Chromium could read its receipt.
    # Require durable acknowledgement cleanup in the restarted extension too.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        acknowledged = replacement.evaluate('async key=>(await chrome.storage.local.get(key))[key]', key)
        if not acknowledged['outbox'] and acknowledged['run']['points'].get('thirty') == 'saved':
            break
        source.wait_for_timeout(100)
    assert acknowledged['run']['id'] == run_id
    assert not acknowledged['outbox'] and acknowledged['run']['points']['thirty'] == 'saved'
    record_property('native_retried_delivery_acknowledgement_seconds', time.perf_counter() - phase_started)
    value = values[0]
    assert value['point'] == 'thirty' and value['position_seconds'] == pytest.approx(30, abs=5)
    embedding = audio_embedding.validate(value['embedding'])
    assert embedding['patches'] == 3 and min(embedding['values']) < 0
    model = json.loads(audio_vibe_model.DEFAULT_PATH.read_text())
    assert audio_embedding.signature(value) == model['embedding_signature']
    assert len(value['tags']) == 50 and len(value['audio_attributes']['values']) == 27
    assert value['audio_transferred_to_app'] is False and value['raw_audio_retained'] is False
    assert set(delivery['attempts']) == {queued['id']} and len(delivery['attempts']) >= 2
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: loaded.append(request.url))
    page.goto(live_app + '/playlists')
    wait_samples(page, 1)
    expect(page.locator('[data-audio-vibe-banner]')).to_have_count(0)
    assert not store.conn.execute('SELECT 1 FROM audio_annotations').fetchone()
    assert not any('/audio-vibe-banner.js' in url or '/audio-vibe-cards.js' in url
                   or url.endswith('/audio-vibes/feedback') for url in loaded)
    # A document observer closing leaves future scheduling with the extension,
    # while all completed native capture buffers have already been released.
    page.close()
    _released(source_cdp, isolated)
    assert _evaluate(source_cdp, '!document.querySelector("video").paused')
    assert not errors
    record_property('native_capture_decode_message_times', json.dumps(_evaluate(source_cdp,
        '({recordings:previewRecordings,...previewNativeCosts})', isolated)))
    variant = request.node.callspec.params['preview_browser']
    Path('/tmp/tuneconsole-882-native-' + variant + '.json').write_text(json.dumps({
        'sample': value, 'delivery_attempts': delivery['attempts'], 'loaded_assets': loaded,
        'acknowledged_owner': acknowledged,
        'model_signature': model['embedding_signature']}, indent=2))
    record_property('native_single_sample_journey_seconds', time.perf_counter() - journey_started)


def seed_samples(store, count=3, **changes):
    values = []
    run_id = str(uuid4())
    for index, point in enumerate(('thirty', 'middle', 'two_thirds')[:count]):
        value = {**sample(run_id=run_id, point=point), **changes}
        store.audio_vibes.save_sample(value, index + 1)
        values.append(value)
    return values


def saved_banner(page, live_app, store, count=3, **changes):
    values = seed_samples(store, count, **changes)
    first = values[0]
    cached = {'id': first['run_id'], 'video_id': first['video_id'], 'title': first['title'],
             'artist': first['artist'], 'duration': 240, 'last_position': 35,
             'points': {value['point']: 'saved' for value in values},
             'attempts': {}, 'retry_after': {}, 'dismissed': False}
    page.add_init_script('if (!localStorage.getItem(' + json.dumps(RUN) + ')) localStorage.setItem(' + json.dumps(RUN) + ', JSON.stringify('
                         + json.dumps(cached)[:-1] + ',"updated_at":Date.now()}));')
    state = {'position': 35, 'duration': 240, 'video_id': first['video_id'], 'title': first['title']}
    page.route('**/bridge/status', lambda route: playback_route(route, state))
    return values, state


def publish_run(page, run_id):
    page.evaluate('''async id => {
      const value = await (await fetch('/audio-vibes/runs/' + id)).json();
      const records = JSON.parse(localStorage.getItem('tuneconsole.audio-vibes.cards.1') || '[]');
      if (!records.some(row => row.id === id)) {
        // A real new occurrence is announced by playback before its sample
        // response. A late VIEW response alone must never admit an old card.
        const playback = value.playback || value.samples[0];
        localStorage.setItem('tuneconsole.audio-vibes.run.1', JSON.stringify({id, ...playback}));
        window.dispatchEvent(new StorageEvent('storage', {key:'tuneconsole.audio-vibes.run.1'}));
      }
      localStorage.setItem('tuneconsole.audio-vibes.view.1', JSON.stringify(value));
      window.dispatchEvent(new StorageEvent('storage', {key:'tuneconsole.audio-vibes.view.1'}));
    }''', run_id)


def visible_card(page):
    return page.locator('[data-vibe-card]:visible')


def assert_quiet_audio_card(page):
    expect(visible_card(page)).not_to_contain_text(re.compile(
        r'\bHTTP\s*\d{3}\b|server rejected|restore those checks|reconnect|restart|browser storage|'
        r'Controlled metadata delivery refusal|Connection lost|Invalid audio evidence', re.I))
    expect(page.get_by_role('alert')).to_have_count(0)
    expect(page.get_by_role('alertdialog')).to_have_count(0)


def test_retained_card_waits_for_saved_answer_and_retries_after_reconnect(
        vibes_enabled, live_app, page, store):
    values = seed_samples(store)
    first = values[0]
    store.audio_vibes.save_feedback(first['run_id'], [], ['cruise'], 'audio-session-targets-1', None, 10)
    frozen = store.audio_vibes.feedback(first['run_id'])
    record = {'id': first['run_id'], 'dismissed': False, 'draft': None, 'dirty': False,
              'sample_ids': [], 'playback': {key: first[key] for key in ('video_id', 'title', 'artist')}}
    page.add_init_script('localStorage.setItem(' + json.dumps(CARDS) + ', '
                         + json.dumps(json.dumps([record])) + ');')
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': False}))
    attempts = []
    offline = True

    def load(route):
        attempts.append(page.evaluate('Date.now()') / 1000)
        if offline:
            route.fulfill(status=503, json={'detail': 'Temporary connection failure'})
        else:
            route.continue_()

    page.route('**/audio-vibes/runs/' + first['run_id'] + '**', load)
    page.clock.install()
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    expect(card.locator('[data-vibe-track]')).to_have_text(first['title'])
    expect(card.locator('[data-vibe-choice]')).to_have_count(6)
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    expect(card.get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)).to_be_disabled()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Loading saved answer…')
    assert store.audio_vibes.feedback(first['run_id']) == frozen
    # Normal ticks retry a retained earlier song, even with no current playback,
    # new sample, navigation or event from another tab. Failures back off.
    # Run every normal tick while checking backoff; skipping interval callbacks
    # here could conceal an overly frequent retry loop.
    page.clock.run_for(6500)
    assert 2 <= len(attempts) <= 3
    assert all(later - earlier >= 1.9 for earlier, later in zip(attempts, attempts[1:]))
    offline = False
    with page.expect_response(lambda response: '/audio-vibes/runs/' + first['run_id'] in response.url
                              and response.ok):
        page.clock.fast_forward(10000)
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(3, timeout=12000)
    expect(card.get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)).to_have_attribute('aria-pressed', 'true')
    expect(card.get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)).to_be_enabled()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(first['run_id']) == frozen
    restored_attempts = len(attempts)
    page.clock.run_for(4500)
    assert len(attempts) == restored_attempts


@pytest.mark.parametrize('count', [0, 2, 3])
def test_returning_tab_restores_current_song_without_waiting_for_sampling_owner(
        vibes_enabled, live_app, page, store, count):
    values = seed_samples(store, count=count)
    run_id = values[0]['run_id'] if values else str(uuid4())
    playback = {'video_id': 'vibe_song', 'title': 'Song in the background tab', 'artist': 'Observed artist'}
    if values:
        playback = {key: values[0][key] for key in playback}
    cached = {'id': run_id, **playback, 'duration': 240, 'last_position': 35,
              'points': {value['point']: 'saved' for value in values}, 'attempts': {},
              'retry_after': {}, 'updated_at': int(time.time() * 1000)}
    owner = page.context.new_page()
    owner.goto(live_app + '/static/favicon.svg')
    owner.evaluate('''cached => {
      localStorage.setItem('tuneconsole.audio-vibes.run.1', JSON.stringify(cached));
      navigator.locks.request('tuneconsole-audio-vibes', async () => {
        window.holdingSamplingLock = true;
        await new Promise(() => {});
      });
    }''', cached)
    owner.wait_for_function('window.holdingSamplingLock')
    # An older/hidden owner has not published a card and will send no storage
    # events or new analyses. Loading the app must still restore this occurrence.
    page.route('**/bridge/status', lambda route: playback_route(route, {'position': 35, **playback}))
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    expect(card.locator('[data-vibe-track]')).to_have_text(playback['title'])
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(count)
    assert card.get_attribute('data-run-id') == run_id
    card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(run_id)['sample_ids'] == [value['id'] for value in values]
    card.get_by_role('button', name='Dismiss vibe check for this song').click()
    page.reload()
    expect(page.locator('[data-audio-vibe-banner]')).to_be_hidden()
    owner.close()


def test_upload_backlog_does_not_block_feedback_for_new_songs(vibes_enabled, live_app, page, store):
    state = {'position': 0, 'duration': 195, 'title': 'First song while uploads are offline'}
    page.context.route('**/bridge/status', lambda route: playback_route(route, state))
    page.context.route('**/audio-vibes/samples', lambda route: route.abort())
    backlog = [sample() for _ in range(12)]
    page.add_init_script('if (!localStorage.getItem("tuneconsole.audio-vibes.outbox.1")) '
                         + 'localStorage.setItem("tuneconsole.audio-vibes.outbox.1", '
                         + json.dumps(json.dumps(backlog)) + ');')
    fake_extension(page)
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    expect(card.locator('[data-vibe-track]')).to_have_text(state['title'])
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    first_id = card.get_attribute('data-run-id')
    state.update(video_id='next-song', title='Second song while uploads are offline')
    page.evaluate('playbackStatus.refresh()')
    expect(page.locator('[data-vibe-count]')).to_have_text('1 of 2 songs')
    assert store.audio_vibes.feedback(first_id) is None
    page.goto(live_app + '/artists')
    expect(card.locator('[data-vibe-track]')).to_have_text('First song while uploads are offline')
    card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(first_id)['vibes'] == ['relax']
    assert store.audio_vibes.feedback(first_id)['sample_ids'] == []
    assert store.audio_vibes.feedback(first_id)['prediction'] is None
    other = page.context.new_page()
    other.goto(live_app + '/playlists')
    expect(visible_card(other).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute(
        'aria-pressed', 'true')
    visible_card(other).get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True).click()
    expect(visible_card(other).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    expect(card.get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)).to_have_attribute('aria-pressed', 'true')
    assert set(store.audio_vibes.feedback(first_id)['vibes']) == {'relax', 'cruise'}
    page.get_by_role('button', name='Next song · 1 more').click()
    expect(card.locator('[data-vibe-track]')).to_have_text(state['title'])
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    second_id = card.get_attribute('data-run-id')
    assert second_id != first_id
    card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(second_id)['vibes'] == ['energize']
    assert store.audio_vibes.feedback(second_id)['sample_ids'] == []
    assert store.audio_vibes.feedback(second_id)['prediction'] is None
    page.get_by_role('button', name='Next song · 1 more').click()
    expect(card.locator('[data-vibe-track]')).to_have_text('First song while uploads are offline')
    card.get_by_role('button', name='Dismiss vibe check for this song').click()
    page.reload()
    expect(visible_card(page).locator('[data-vibe-track]')).to_have_text(state['title'])
    assert store.audio_vibes.samples(first_id) == []
    assert store.audio_vibes.samples(second_id) == []
    page.screenshot(path='/tmp/tuneconsole-461-zero-analysis-1440.png')
    assert page.evaluate('vibeCaptures.length') == 0
    assert page.evaluate('JSON.parse(localStorage.getItem("tuneconsole.audio-vibes.outbox.1")).length') == 0
    assert page.evaluate('''Object.values(JSON.parse(localStorage.getItem(
        'fixture.extension.audio-storage')))[0].outbox.length''') == 12
    other.close()


@pytest.mark.parametrize('count', [0, 2])
def test_feedback_freezes_checks_at_click_while_another_tab_holds_storage_lock(
        vibes_enabled, live_app, page, store, count):
    acknowledgements = []

    def expect_checks(completed):
        for index, label in enumerate(('30 seconds', 'Halfway', 'Two-thirds')):
            complete = index < completed
            indicator = card.get_by_role(
                'img', name=f'{label}: {"complete" if complete else "pending"}', exact=True)
            expect(indicator).to_have_attribute('data-complete', str(complete).lower())

    def acknowledge(response):
        path = response.url.removeprefix(live_app)
        if response.request.method == 'POST' and path in ('/audio-vibes/runs', '/audio-vibes/feedback'):
            acknowledgements.append((path, response.status))

    page.on('response', acknowledge)
    page.route('**/bridge/status', lambda route: playback_route(route, {'position': 0, 'duration': 240}))
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    expect(card.locator('[data-vibe-choice]')).to_have_count(6)
    run_id = card.get_attribute('data-run-id')
    values = [{**sample(run_id=run_id, point=point), 'video_id': 'vibe_song'}
              for point in ('thirty', 'middle', 'two_thirds')]
    for value in values[:count]:
        store.audio_vibes.save_sample(value, 1)
    if count:
        publish_run(page, run_id)
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(count)
    expect_checks(count)
    click_ids = {value['id'] for value in store.audio_vibes.samples(run_id)}
    assert click_ids == {value['id'] for value in values[:count]}
    owner = page.context.new_page()
    owner.goto(live_app + '/static/favicon.svg')
    owner.evaluate('''() => {
      navigator.locks.request('tuneconsole-audio-vibe-cards', async () => {
        window.holdingCardLock = true;
        await new Promise(resolve => { window.releaseCardLock = resolve; });
      });
    }''')
    owner.wait_for_function('window.holdingCardLock')
    try:
        card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
        expect(card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute('aria-pressed', 'true')
        expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saving…')
        # A real later check arrives before the click can update localStorage.
        # It must light up without becoming part of the earlier judgment.
        if count == 2:
            # A pointer over the card cannot freeze its final prediction.
            page.mouse.move(1100, 650)
        store.audio_vibes.save_sample(values[count], 2)
        publish_run(page, run_id)
        expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(count + 1)
        expect_checks(count + 1)
        if count == 2:
            expect(card.locator('[data-vibe-prediction]')).to_have_text(
                'Prediction: Forceful, hard-hitting, cathartic')
        owner.evaluate('window.releaseCardLock()')
        expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
        assert acknowledgements == [('/audio-vibes/runs', 200), ('/audio-vibes/feedback', 200)]
        frozen = store.audio_vibes.feedback(run_id)
        assert set(frozen['sample_ids']) == click_ids
        assert frozen['prediction'] is None
        assert frozen['vibes'] == ['relax']
        expect(card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute('aria-pressed', 'true')
        assert store.audio_vibes.feedback(run_id) == frozen, 'Analysis cannot rewrite an earlier human judgment'
    finally:
        owner.close()


def test_saved_server_answer_fills_placeholder_without_resaving(vibes_enabled, live_app, page, store):
    labels = ['relax', 'cruise']
    values, _ = saved_banner(page, live_app, store)
    run_id = values[0]['run_id']
    store.audio_vibes.save_feedback(run_id, [row['id'] for row in values], labels,
                                    'audio-session-targets-1', None, 10)
    frozen = store.audio_vibes.feedback(run_id)
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    for key, description in DESCRIPTIONS.items():
        expect(card.get_by_role('button', name=description, exact=True)).to_have_attribute(
            'aria-pressed', str(key in labels).lower())
    assert store.audio_vibes.feedback(run_id) == frozen
    page.reload()
    expect(visible_card(page).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(run_id) == frozen


def test_replayed_song_keeps_previous_answer_selected_through_sampling_until_explicit_edit(
        vibes_enabled, live_app, page, store):
    earlier = seed_samples(store, video_id='vibe_song')
    previous_run = earlier[0]['run_id']
    store.audio_vibes.save_feedback(previous_run, [row['id'] for row in earlier], ['cruise', 'concentrate'],
                                    'audio-session-targets-1', None, 10)
    frozen = store.audio_vibes.feedback(previous_run)
    state = {'position': 0, 'duration': 240}
    page.route('**/bridge/status', lambda route: playback_route(route, state))
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Previous answer')
    for key in ('cruise', 'concentrate'):
        expect(card.get_by_role('button', name=DESCRIPTIONS[key], exact=True)).to_have_attribute('aria-pressed', 'true')
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    new_run = card.get_attribute('data-run-id')
    assert new_run != previous_run
    assert store.audio_vibes.feedback(new_run) is None
    values = seed_samples(store, run_id=new_run, video_id='vibe_song')
    publish_run(page, new_run)
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(3)
    assert store.audio_vibes.feedback(new_run) is None
    assert store.audio_vibes.feedback(previous_run) == frozen
    page.reload()
    card = visible_card(page)
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Previous answer')
    for key in ('cruise', 'concentrate'):
        expect(card.get_by_role('button', name=DESCRIPTIONS[key], exact=True)).to_have_attribute('aria-pressed', 'true')
    card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    answer = store.audio_vibes.feedback(new_run)
    assert set(answer['vibes']) == {'cruise', 'concentrate', 'relax'}
    assert set(answer['sample_ids']) == {row['id'] for row in values}
    assert store.audio_vibes.feedback(previous_run) == frozen


def test_normal_app_collects_then_shows_one_compact_card_and_autosaves(vibes_enabled, live_app, page, store):
    state = {'position': 0, 'duration': 240}
    page.set_viewport_size({'width': 1400, 'height': 900})
    page.route('**/bridge/status', lambda route: playback_route(route, state))
    fake_extension(page)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app + '/#directions')
    banner = page.locator('[data-audio-vibe-banner]')
    expect(banner).to_be_visible()
    expect(visible_card(page).locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    assert page.evaluate('vibeCaptures.length') == 0
    for index, position in enumerate((30, 120, 160), start=1):
        state['position'] = position
        page.evaluate('playbackStatus.refresh()')
        wait_samples(page, index)
        if index < 3:
            expect(banner).to_be_visible()
            expect(visible_card(page).locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(index)
    card = visible_card(page)
    expect(card.locator('[data-vibe-prediction]')).to_have_text('Prediction: Forceful, hard-hitting, cathartic')
    expect(card.locator('[data-vibe-choice]')).to_have_count(6)
    assert banner.get_by_role('button', name='Save labels', exact=True).count() == 0
    assert banner.locator('[data-vibe-held-status], progress').count() == 0
    for width, height in ((1400, 900), (800, 450)):
        page.set_viewport_size({'width': width, 'height': height})
        for description in DESCRIPTIONS.values():
            expect(card.get_by_role('button', name=description, exact=True)).to_be_in_viewport(ratio=1)
        assert banner.evaluate('(node) => node.scrollHeight <= node.clientHeight')
        box = banner.bounding_box()
        assert box['x'] >= 0 and box['y'] >= 0 and box['y'] + box['height'] <= height
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        banner.evaluate('node => Promise.all(node.getAnimations().map(animation => animation.finished))')
        page.screenshot(path=f'/tmp/tuneconsole-882-card-{width}-{height}.png')
    assert page.evaluate('vibeCaptures.length') == 3
    run_id = page.evaluate('JSON.parse(localStorage.getItem(' + json.dumps(RUN) + ')).id')
    card.get_by_role('button', name=DESCRIPTIONS['release'], exact=True).click()
    card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert set(store.audio_vibes.feedback(run_id)['vibes']) == {'release', 'energize'}
    assert len(store.audio_vibes.feedback(run_id)['sample_ids']) == 3
    assert store.settings.get_setting(home_vibe.SETTING) is None
    page.reload()
    card = visible_card(page)
    expect(card.get_by_role('button', name=DESCRIPTIONS['release'], exact=True)).to_have_attribute('aria-pressed', 'true')
    assert page.evaluate('vibeCaptures.length') == 0
    card.get_by_role('button', name=DESCRIPTIONS['release'], exact=True).click()
    card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(run_id)['vibes'] == []
    empty_answer = store.audio_vibes.feedback(run_id)
    posts = []
    page.on('request', lambda request: posts.append(request)
            if request.url.endswith('/audio-vibes/feedback') else None)
    page.reload()
    card = visible_card(page)
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    for description in DESCRIPTIONS.values():
        expect(card.get_by_role('button', name=description, exact=True)).to_have_attribute('aria-pressed', 'false')
    assert store.audio_vibes.feedback(run_id) == empty_answer
    assert posts == []
    assert page.evaluate('vibeCaptures.length') == 0
    card.get_by_role('button', name='Dismiss vibe check for this song').click()
    expect(banner).to_be_hidden()
    page.reload()
    expect(banner).to_be_hidden()
    state.update(video_id='another_song', position=0)
    page.evaluate('playbackStatus.refresh()')
    expect(banner).to_be_visible()
    assert errors == []


def test_cards_stack_without_changing_a_song_being_labeled(vibes_enabled, live_app, page, store, tmp_path):
    page.set_viewport_size({'width': 1280, 'height': 720})
    traits = sample(relaxed=.3, aggressive=.55)['traits']
    for key, score in {'happy': .2, 'sad': .6, 'arousal': 5.8}.items():
        traits[key] = {'score': score, 'per_patch': [score] * 3}
    values, state = saved_banner(page, live_app, store, traits=traits)
    # Keep the measurement fixture independent of the weak trait scores.
    values[1]['audio_attributes']['values'].update(tempo_bpm=100.0, integrated_lufs=-18.0, spectral_centroid_hz=None)
    values[1].update(capture_reason='catch_up', position_seconds=140.0)
    values[2]['audio_attributes'] = None
    with store.conn:
        from yt_playlist.core import sample_files
        for value in values[1:]:
            filename = sample_files.publish(sample_files.directory(store.conn), value)
            store.conn.execute('UPDATE sample_attributes SET filename=? WHERE id=?', (filename, value['id']))
    page.goto(live_app + '/playlists')
    first = visible_card(page)
    expect(first.locator('[data-vibe-track]')).to_have_text(values[0]['title'])
    expect(first.locator('[data-vibe-prediction]')).to_have_text(
        'Prediction: Easygoing, moderately lively, comfortable company')
    for description in DESCRIPTIONS.values():
        expect(first.get_by_role('button', name=description, exact=True)).to_be_in_viewport(ratio=1)
    first.get_by_role('button', name='Audio details').click()
    dialog = page.get_by_role('dialog', name='Song audio details')
    summary = dialog.locator('[data-vibe-measurement-summary]')
    expect(summary).to_contain_text('2 of 3 excerpts include audio measurements; 53 of 81 scalar values available.')
    expect(summary).to_contain_text('Tempo across excerpts: 100 to 124 BPM (estimates vary).')
    expect(summary).to_contain_text('Excerpt loudness: -18 to -14.2 LUFS.')
    expect(summary).to_contain_text('1 replacement excerpt captured after the planned position.')
    expect(dialog.locator('[data-vibe-evidence]')).to_contain_text('Tension / force signal')
    assert dialog.locator('tr').count() == 2 * 27 + 3 * (6 + 50)
    page.keyboard.press('Escape')
    expect(dialog).to_be_hidden()
    expect(first.locator('[data-vibe-choice]')).to_have_count(6)
    first.get_by_role('button', name='Audio details').click()
    page.evaluate('window.originalSample=document.querySelector("[data-vibe-sample]")')
    state.update(video_id='next_song', title='The next completed song', position=0)
    page.evaluate('playbackStatus.refresh()')
    expect(page.locator('[data-vibe-count]')).to_have_text('1 of 2 songs')
    next_run = page.evaluate('JSON.parse(localStorage.getItem(' + json.dumps(RUN) + ')).id')
    second = seed_samples(store, run_id=next_run, title='The next completed song', video_id='next_song')
    publish_run(page, second[0]['run_id'])
    expect(first.locator('[data-vibe-track]')).to_have_text(values[0]['title'])
    assert page.evaluate('originalSample === document.querySelector("[data-vibe-sample]")')
    expect(page.locator('[data-vibe-count]')).to_have_text('1 of 2 songs')
    page.get_by_role('button', name='Close audio details').click()
    first.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
    expect(first.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    feedback = store.audio_vibes.feedback(values[0]['run_id'])
    assert feedback['sample_ids'] == [value['id'] for value in values]
    assert feedback['vibes'] == ['energize']
    assert feedback['prediction']['selection_method'] == 'best_available'
    assert store.audio_vibes.feedback(second[0]['run_id']) is None
    page.screenshot(path=str(tmp_path / 'audio-vibe-card-stack.png'))
    page.get_by_role('button', name='Next song · 1 more').click()
    expect(visible_card(page).locator('[data-vibe-track]')).to_have_text('The next completed song')
    visible_card(page).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    expect(visible_card(page).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(second[0]['run_id'])['vibes'] == ['relax']
    page.reload()
    expect(page.locator('[data-vibe-count]')).to_have_text('1 of 2 songs')
    expect(visible_card(page).locator('[data-vibe-track]')).to_have_text(values[0]['title'])
    visible_card(page).get_by_role('button', name='Dismiss vibe check for this song').click()
    expect(visible_card(page).locator('[data-vibe-track]')).to_have_text('The next completed song')


def test_rapid_clicks_and_unsynced_dismissed_card_survive_reload(vibes_enabled, live_app, page, store):
    values, _ = saved_banner(page, live_app, store)
    blocked = True
    requests = []

    def feedback(route):
        requests.append(route.request.post_data_json)
        if blocked:
            route.fulfill(status=503, json={'detail': 'Temporarily unavailable'})
        else:
            route.continue_()

    page.route('**/audio-vibes/feedback', feedback)
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    # This scenario checks retrying a fully analysed judgment. An immediately
    # usable placeholder may legitimately carry zero checks before restoration.
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(3)
    for key in ('release', 'energize', 'release', 'relax'):
        card.get_by_role('button', name=DESCRIPTIONS[key], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved here · retrying sync…')
    card.get_by_role('button', name='Dismiss vibe check for this song').click()
    blocked = False
    with page.expect_response(lambda response: response.url.endswith('/audio-vibes/feedback') and response.status == 200):
        page.reload()
    page.wait_for_function('''() => JSON.parse(localStorage.getItem(
      'tuneconsole.audio-vibes.cards.1')).every(row => !row.dirty)''')
    assert set(store.audio_vibes.feedback(values[0]['run_id'])['vibes']) == {'energize', 'relax'}
    expect(page.locator('[data-audio-vibe-banner]')).to_be_hidden()
    assert requests
    assert all(set(value['sample_ids']) == {sample['id'] for sample in values} for value in requests)


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'temporary'])
def test_failed_answer_does_not_block_other_songs_and_keeps_original_checks(
        vibes_enabled, live_app, page, store, failure):
    values, state = saved_banner(page, live_app, store)
    run_id = values[0]['run_id']
    original_ids = [value['id'] for value in values]
    filename = Path(store.conn.execute('SELECT filename FROM sample_attributes WHERE id=?',
                                       (original_ids[0],)).fetchone()[0])
    original_file = filename.read_bytes()
    other_values = seed_samples(store, video_id='independent-song', title='Independent valid answer')
    other_id = other_values[0]['run_id']
    pending, sent, statuses = [], [], []
    hold = True
    repaired = False

    def feedback(route):
        body = route.request.post_data_json
        sent.append(body)
        if body['run_id'] == run_id and hold:
            pending.append(route)
        elif body['run_id'] == run_id and failure == 'temporary' and not repaired:
            route.fulfill(status=503, json={'detail': 'Temporarily unavailable'})
        else:
            route.continue_()

    page.context.route('**/audio-vibes/feedback', feedback)
    page.context.route('**/bridge/status', lambda route: playback_route(route, state))
    page.context.on('response', lambda response: statuses.append(
        (response.request.post_data_json['run_id'], response.status))
        if response.url.endswith('/audio-vibes/feedback') else None)
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(3)
    card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    page.wait_for_function('''() => JSON.parse(localStorage.getItem(
      'tuneconsole.audio-vibes.cards.1'))[0].dirty''')
    publish_run(page, other_id)
    page.get_by_role('button', name='Next song · 1 more').click()
    expect(card.locator('[data-vibe-track]')).to_have_text('Independent valid answer')
    card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
    page.wait_for_function('''id => JSON.parse(localStorage.getItem(
      'tuneconsole.audio-vibes.cards.1')).find(row => row.id === id)?.dirty''', arg=other_id)
    other = page.context.new_page()
    try:
        other.goto(live_app + '/playlists')
        expect(visible_card(other).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute(
            'aria-pressed', 'true')
        assert len(pending) == 1
        if failure == 'missing':
            filename.unlink()
        elif failure == 'corrupt':
            filename.write_bytes(b'Invalid audio evidence')
        hold = False
        if failure == 'temporary':
            pending[0].fulfill(status=503, json={'detail': 'Temporarily unavailable'})
        else:
            pending[0].continue_()
        expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved', timeout=10000)
        independent_answer = store.audio_vibes.feedback(other_id)
        assert independent_answer['vibes'] == ['energize']
        assert independent_answer['sample_ids'] == [value['id'] for value in other_values]
        assert store.audio_vibes.feedback(run_id) is None
        assert (run_id, 503 if failure == 'temporary' else 409) in statuses
        # Both tabs see the same per-answer failure. Dismissing it must retain
        # the original IDs even though a fresh GET can now read only two checks.
        status = 'Saved here · retrying sync…' if failure == 'temporary' else 'Saved here · needs attention'
        expect(visible_card(other).locator('[data-vibe-feedback-status]')).to_have_text(status)
        assert_quiet_audio_card(other)
        if failure != 'temporary':
            expect(visible_card(other).locator('[data-vibe-sync-detail]')).to_have_text(
                'Your labels are kept here and have not synced yet.')
        visible_card(other).get_by_role('button', name='Dismiss vibe check for this song').click()
        page.reload()
        expect(visible_card(page).locator('[data-vibe-track]')).to_have_text('Independent valid answer')
        record = page.evaluate('''id => JSON.parse(localStorage.getItem(
          'tuneconsole.audio-vibes.cards.1')).find(row => row.id === id)''', run_id)
        assert record['dirty'] and record['dismissed'] and record['draft'] == ['relax']
        assert record['sample_ids'] == original_ids
        if failure != 'temporary':
            assert record['sync_error'] == {'status': 409, 'rejected': True}
        assert_quiet_audio_card(page)
        if failure != 'temporary':
            notice = page.get_by_role('button', name='1 saved answer needs attention', exact=True)
            expect(notice).to_be_visible()
            before = len([body for body in sent if body['run_id'] == run_id])
            page.wait_for_timeout(2200)
            assert len([body for body in sent if body['run_id'] == run_id]) == before
            notice.click()
            expect(visible_card(page).locator('[data-vibe-feedback-status]')).to_have_text(status)
            expect(visible_card(page).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute(
                'aria-pressed', 'true')
            expect(visible_card(page).locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(2)
            assert_quiet_audio_card(page)
            if failure == 'missing':
                page.set_viewport_size({'width': 1280, 'height': 800})
                page.screenshot(path='/tmp/tuneconsole-884-rejected.png')
            visible_card(page).get_by_role('button', name='Dismiss vibe check for this song').click()
            expect(notice).to_be_visible()
        filename.write_bytes(original_file)
        repaired = True
        # Transfer HTTP ownership to the other tab. The old tab's close must
        # neither erase its retained answer nor prevent a later explicit retry.
        page.close()
        if failure != 'temporary':
            other.get_by_role('button', name='1 saved answer needs attention', exact=True).click()
            expect(visible_card(other).locator('[data-vibe-feedback-status]')).to_have_text(status)
            assert_quiet_audio_card(other)
            visible_card(other).get_by_role('button', name='Retry sync', exact=True).click()
        other.wait_for_function('''() => JSON.parse(localStorage.getItem(
          'tuneconsole.audio-vibes.cards.1')).every(row => !row.dirty)''', timeout=15000)
        answer = store.audio_vibes.feedback(run_id)
        assert answer['vibes'] == ['relax'] and answer['sample_ids'] == original_ids
        assert store.audio_vibes.feedback(other_id) == independent_answer
        assert all(body['sample_ids'] == original_ids and body['vibes'] == ['relax']
                   for body in sent if body['run_id'] == run_id)
        assert len([body for body in sent if body['run_id'] == other_id]) == 1
        expect(other.get_by_role('button', name='1 saved answer needs attention', exact=True)).to_be_hidden()
        assert_quiet_audio_card(other)
    finally:
        filename.write_bytes(original_file)
        other.close()


@pytest.mark.parametrize('other_dismisses', [False, True])
def test_failed_storage_keeps_explicit_edits_until_retry_merges_other_tab(
        vibes_enabled, live_app, page, store, monkeypatch, other_dismisses):
    update, _ = update_card(monkeypatch, page, store)
    values, _ = saved_banner(page, live_app, store, count=2)
    run_id = values[0]['run_id']
    page.goto(live_app + '/#notices')
    card = visible_card(page)
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(2)
    card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    card.get_by_role('button', name=DESCRIPTIONS['concentrate'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    other = page.context.new_page()
    other.goto(live_app + '/playlists')
    expect(visible_card(other).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute(
        'aria-pressed', 'true')
    expect(visible_card(other).get_by_role('button', name=DESCRIPTIONS['concentrate'], exact=True)).to_have_attribute(
        'aria-pressed', 'true')
    page.evaluate('''key => {
      const original = Storage.prototype.setItem;
      window.failCardWrites = true;
      Storage.prototype.setItem = function(name, value) {
        if (name === key && window.failCardWrites) throw new DOMException('Quota exhausted', 'QuotaExceededError');
        return original.call(this, name, value);
      };
    }''', CARDS)
    try:
        # Independent add and remove operations share the same failed queue.
        # Concentrate stays untouched so an accidental replacement loses data.
        card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
        expect(card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True)).to_have_attribute(
            'aria-pressed', 'true')
        card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
        expect(card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute(
            'aria-pressed', 'false')
        # Additional quick edits must remain ordered even when every write fails.
        for vibe in ('release', 'release'):
            card.get_by_role('button', name=DESCRIPTIONS[vibe], exact=True).click()
        expect(card.locator('[data-vibe-feedback-status]')).to_have_text(
            'Not saved yet · keep this page open.')
        expect(card.get_by_role('button', name=DESCRIPTIONS['concentrate'], exact=True)).to_have_attribute(
            'aria-pressed', 'true')
        expect(card.get_by_role('button', name=DESCRIPTIONS['release'], exact=True)).to_have_attribute(
            'aria-pressed', 'false')
        expect(card.get_by_role('button', name='Dismiss vibe check for this song')).to_be_disabled()
        # An unfocused, unhovered card still blocks a real updater reload.
        page.evaluate('window.originalDocument = true; document.activeElement.blur()')
        page.mouse.move(0, 0)
        page.wait_for_function('() => Alpine.$data(document.querySelector("#update-nudge")).instance === "old-process"')
        update.update(instance='restarted-process', phase='current', message='Up to date.')
        page.evaluate('Alpine.$data(document.querySelector("#update-nudge")).refresh()')
        page.wait_for_function('() => !Alpine.$data(document.querySelector("#update-nudge")).pending')
        assert page.evaluate('window.originalDocument')
        assert page.evaluate('Alpine.$data(document.querySelector("#update-nudge")).instance') == 'old-process'
        # The successful tab may save a different label while this intent lives
        # only in memory. Retry must apply operations to that latest shared draft.
        visible_card(other).get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True).click()
        expect(visible_card(other).locator('[data-vibe-feedback-status]')).to_have_text('Saved', timeout=10000)
        expect(card.get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)).to_have_attribute('aria-pressed', 'true')
        expect(card.locator('[data-vibe-feedback-status]')).to_have_text(
            'Not saved yet · keep this page open.')
        if other_dismisses:
            visible_card(other).get_by_role('button', name='Dismiss vibe check for this song').click()
            expect(other.locator('[data-audio-vibe-banner]')).to_be_hidden()
            page.wait_for_function('key => JSON.parse(localStorage.getItem(key))[0].dismissed', arg=CARDS)
            expect(card).to_be_visible()
            expect(card.get_by_role('button', name='Retry saving', exact=True)).to_be_visible()
            expect(card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True)).to_have_attribute(
                'aria-pressed', 'true')
            expect(card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute(
                'aria-pressed', 'false')
        page.set_viewport_size({'width': 1280, 'height': 800})
        page.screenshot(path=f'/tmp/tuneconsole-885-unsaved-{other_dismisses}.png')
        # A later excerpt is visible by retry time but was absent at every click.
        later = sample(run_id=run_id, point='two_thirds')
        store.audio_vibes.save_sample(later, 3)
        publish_run(page, run_id)
        # A remotely dismissed occurrence may ignore later sampler updates; its
        # click-time checks and the recovery form must still remain available.
        if not other_dismisses:
            expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(3)
        expect(card).to_be_visible()
        page.evaluate('window.failCardWrites = false')
        card.get_by_role('button', name='Retry saving', exact=True).click()
        expected = {'energize', 'concentrate', 'cruise'}
        for tab in (page, other):
            expect(visible_card(tab).locator('[data-vibe-feedback-status]')).to_have_text('Saved', timeout=10000)
            for vibe in DESCRIPTIONS:
                expect(visible_card(tab).get_by_role('button', name=DESCRIPTIONS[vibe], exact=True)).to_have_attribute(
                    'aria-pressed', str(vibe in expected).lower())
        answer = store.audio_vibes.feedback(run_id)
        assert set(answer['vibes']) == expected
        assert set(answer['sample_ids']) == {value['id'] for value in values}
        record = page.evaluate('key => JSON.parse(localStorage.getItem(key))[0]', CARDS)
        assert not record['dirty'] and set(record['draft']) == expected
        assert set(record['sample_ids']) == {value['id'] for value in values}
        expect(card.get_by_role('button', name='Retry saving', exact=True)).to_be_hidden()
        card.get_by_role('button', name='Dismiss vibe check for this song').click()
        page.reload()
        expect(page.locator('[data-audio-vibe-banner]')).to_be_hidden()
        assert set(store.audio_vibes.feedback(run_id)['vibes']) == expected
    finally:
        other.close()


@pytest.mark.parametrize('first_status', [200, 409, 503])
def test_new_clicks_are_not_overwritten_by_a_slow_feedback_response(
        vibes_enabled, live_app, page, store, first_status):
    values, _ = saved_banner(page, live_app, store)
    pending = []
    slow = True

    def delay_first(route):
        if slow:
            pending.append(route)
        else:
            route.continue_()

    page.route('**/audio-vibes/feedback', delay_first)
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    card.get_by_role('button', name=DESCRIPTIONS['release'], exact=True).click()
    page.wait_for_function('''() => JSON.parse(localStorage.getItem(
      'tuneconsole.audio-vibes.cards.1'))[0].dirty''')
    # Keep editing while the first HTTP write is still in flight.
    card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
    card.get_by_role('button', name=DESCRIPTIONS['release'], exact=True).click()
    expect(card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True)).to_have_attribute('aria-pressed', 'true')
    expect(card.get_by_role('button', name=DESCRIPTIONS['release'], exact=True)).to_have_attribute('aria-pressed', 'false')
    slow = False
    assert len(pending) == 1
    if first_status == 200:
        pending[0].continue_()
    else:
        pending[0].fulfill(status=first_status, json={'detail': 'The older answer failed'})
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(values[0]['run_id'])['vibes'] == ['energize']


def test_two_tabs_preserve_labels_during_overlapping_local_writes(vibes_enabled, live_app, page, store):
    values, _ = saved_banner(page, live_app, store)
    page.goto(live_app + '/playlists')
    expect(visible_card(page)).to_be_visible()
    other = page.context.new_page()
    other.goto(live_app + '/playlists')
    expect(visible_card(other)).to_be_visible()
    debug = page.context.new_cdp_session(page)
    paused = []
    debug.on('Debugger.paused', lambda event: paused.append(event))
    debug.send('Debugger.enable')
    page.evaluate('''key => {
      const original = Storage.prototype.setItem;
      let pauseNextWrite = true;
      Storage.prototype.setItem = function(name, value) {
        if (name === key && pauseNextWrite) { pauseNextWrite = false; debugger; }
        return original.call(this, name, value);
      };
    }''', CARDS)
    try:
        page.evaluate('''() => setTimeout(() => document.querySelector(
          '[data-vibe-card]:not([hidden]) [data-vibe-choice="relax"]').click(), 0)''')
        for _ in range(20):
            other.wait_for_timeout(25)
            if paused:
                break
        assert paused, 'First tab must pause inside its shared-index mutation'
        visible_card(other).get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
        # The second intent waits for the atomic local mutation, rather than
        # reading a stale draft and silently replacing the first addition.
        other.wait_for_function('''async () => (await navigator.locks.query()).pending.some(
          lock => lock.name === 'tuneconsole-audio-vibe-cards')''')
    finally:
        debug.send('Debugger.resume')
        debug.send('Debugger.disable')
    for tab in (page, other):
        card = visible_card(tab)
        expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
        for key in ('relax', 'energize'):
            expect(card.get_by_role('button', name=DESCRIPTIONS[key], exact=True)).to_have_attribute(
                'aria-pressed', 'true')
    assert set(store.audio_vibes.feedback(values[0]['run_id'])['vibes']) == {'relax', 'energize'}
    other.close()


def test_other_tab_can_remove_labels_while_feedback_is_in_flight(vibes_enabled, live_app, page, store):
    values, _ = saved_banner(page, live_app, store)
    page.goto(live_app + '/playlists')
    other = page.context.new_page()
    other.goto(live_app + '/playlists')
    expect(visible_card(other)).to_be_visible()
    pending = []
    page.route('**/audio-vibes/feedback', lambda route: pending.append(route))
    card = visible_card(page)
    card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    page.wait_for_function('''() => JSON.parse(localStorage.getItem(
      'tuneconsole.audio-vibes.cards.1'))[0].dirty''')
    other_card = visible_card(other)
    expect(other_card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute(
        'aria-pressed', 'true')
    other_card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
    other_card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    expect(other_card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute(
        'aria-pressed', 'false')
    assert len(pending) == 1
    page.unroute('**/audio-vibes/feedback')
    pending[0].continue_()
    for tab in (page, other):
        expect(visible_card(tab).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(values[0]['run_id'])['vibes'] == ['energize']
    other_card.get_by_role('button', name=DESCRIPTIONS['energize'], exact=True).click()
    expect(other_card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(values[0]['run_id'])['vibes'] == []
    other.reload()
    expect(visible_card(other).get_by_role('button', name=DESCRIPTIONS['energize'], exact=True)).to_have_attribute(
        'aria-pressed', 'false')
    other.close()


@pytest.mark.parametrize('failure_phase', ['capturing', 'saving'])
def test_failed_sample_is_logged_and_replaced_in_background(vibes_enabled, live_app, page, store, failure_phase):
    page.route('**/bridge/status', lambda route: playback_route(route, {'position': 30, 'duration': 240}))
    failures, saves = [], []
    page.on('request', lambda request: failures.append(request.post_data_json)
            if request.url.endswith('/audio-vibes/failures') else None)
    if failure_phase == 'saving':
        def reject_first(route):
            saves.append(route.request.post_data)
            if len(saves) == 1:
                route.fulfill(status=422, json={'detail': 'Invalid vibe metadata'})
            else:
                route.continue_()
        page.route('**/audio-vibes/samples', reject_first)
    fake_extension(page, failure_phase == 'capturing')
    page.goto(live_app + '/playlists')
    wait_samples(page, 1)
    expect(page.locator('[data-audio-vibe-banner]')).to_be_visible()
    expect(visible_card(page).locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(1)
    assert len(failures) == 1 and failures[0]['phase'] == failure_phase
    assert failures[0]['attempt'] == 1
    saved = store.audio_vibes.samples(failures[0]['run_id'])
    assert len(saved) == 1
    if failure_phase == 'capturing':
        assert saved[0]['capture_reason'] == 'catch_up'
        assert page.evaluate('vibeCaptures.length') == 2
    else:
        assert len(saves) == 2 and saves[0] == saves[1], 'retry the captured metadata without recording again'
        assert page.evaluate('vibeCaptures.length') == 1


def test_old_prediction_waits_for_current_answers_without_contaminating_feedback(vibes_enabled, live_app, page, store):
    fake_extension(page)
    values, _ = saved_banner(page, live_app, store)
    old = {'run_id': values[0]['run_id'], 'samples': values, 'taxonomy_version': 'audio-vibe-targets-1',
           'choices': {'focus': {'label': 'Instrumental + calm'}}, 'prediction': {'vibe': 'focus'}}
    page.add_init_script('localStorage.setItem(' + json.dumps(VIEW) + ', JSON.stringify(' + json.dumps(old) + '));')
    obsolete = True

    def load(route):
        if obsolete:
            route.fulfill(json=old)
        else:
            route.continue_()

    page.route('**/audio-vibes/runs/*', load)
    page.goto(live_app + '/playlists')
    card = visible_card(page)
    expect(card).to_be_visible()
    expect(card.locator('[data-vibe-prediction]')).to_have_text('0 of 3 audio checks complete')
    expect(card.locator('[data-vibe-choice]')).to_have_count(6)
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    expect(card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_be_disabled()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Loading saved answer…')
    assert store.audio_vibes.feedback(values[0]['run_id']) is None
    obsolete = False
    expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(3, timeout=12000)
    expect(card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_be_enabled()
    card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    feedback = store.audio_vibes.feedback(values[0]['run_id'])
    assert feedback['vibes'] == ['relax']
    assert set(feedback['sample_ids']) == {value['id'] for value in values}
    assert feedback['prediction']['taxonomy_version'] == 'audio-session-targets-1'
    assert not page.evaluate('vibeCaptures')


def test_automatic_app_reload_waits_for_card_interaction(vibes_enabled, live_app, page, store, monkeypatch):
    state, _ = update_card(monkeypatch, page, store)
    saved_banner(page, live_app, store)
    page.goto(live_app + '/#notices')
    expect(visible_card(page)).to_be_visible()
    page.wait_for_function('() => Alpine.$data(document.querySelector("#update-nudge")).instance === "old-process"')
    visible_card(page).get_by_role('button', name='Dismiss vibe check for this song').focus()
    page.evaluate('window.originalDocument = true')
    state.update(instance='restarted-process', phase='current', message='Up to date.')
    page.evaluate('Alpine.$data(document.querySelector("#update-nudge")).refresh()')
    page.wait_for_function('() => !Alpine.$data(document.querySelector("#update-nudge")).pending')
    assert page.evaluate('window.originalDocument')
    assert page.evaluate('Alpine.$data(document.querySelector("#update-nudge")).instance') == 'old-process'
    page.evaluate('document.activeElement.blur()')
    page.mouse.move(0, 0)
    with page.expect_navigation(wait_until='domcontentloaded'):
        page.evaluate('Alpine.$data(document.querySelector("#update-nudge")).refresh()')


@pytest.mark.parametrize('preview_browser', ['packaged_sampling', 'natural_offset'], indirect=True)
def test_three_real_tab_local_samples_on_normal_app_share_one_owner(vibe_browser, live_app, store, tmp_path, record_property):
    journey_started = phase_started = time.perf_counter()
    context, _, source, source_cdp, isolated = vibe_browser
    extension_version = context.service_workers[0].evaluate('chrome.runtime.getManifest().version')
    assert extension_version == json.loads((EXT / 'manifest.json').read_text())['version']
    assert _evaluate(source_cdp, 'previewStreams.length', isolated) == 0
    _evaluate(source_cdp, '''(() => {
      window.previewNextSong = false;
      let ending = false;
      const media = document.querySelector('video');
      media.addEventListener('timeupdate', () => {
        if (previewSongPosition() > 190) ending = true;
        if (ending && previewSongPosition() < 5) previewNextSong = true;
      });
      document.querySelector('#movie_player').getVideoData = () => ({
        video_id: previewNextSong ? 'following_song' : 'vibe_song',
        title: previewNextSong ? 'Following song' : 'Fixture song', author: 'Observed artist'});
    })()''')
    def status(route):
        playback_route(route, {'position': _evaluate(source_cdp, 'previewSongPosition()'),
            'duration': 195, 'snapshot_age': 0,
            **({'video_id': 'following_song', 'title': 'Following song'}
               if _evaluate(source_cdp, 'previewNextSong') else {})})

    context.route('**/bridge/status', status)
    pages = [context.new_page(), context.new_page()]
    uploads, errors, workers = [], [], []
    source.on('worker', lambda worker: workers.append(worker.url))
    context.on('request', lambda request: uploads.append((request.url, request.post_data or ''))
        if request.method == 'POST' and request.url.endswith('/audio-vibes/samples') else None)
    for index, page in enumerate(pages):
        page.set_viewport_size({'width': [1440, 1280][index], 'height': 1000})
        page.add_init_script('window.Worker=class {constructor(){throw Error("No app-side inference");}};')
        page.on('pageerror', lambda error: errors.append(str(error)))
    pages[0].goto(live_app + '/#radio')
    pages[1].goto(live_app + '/playlists')
    banner = pages[1].locator('[data-audio-vibe-banner]')
    expect(banner).to_be_visible()
    expect(visible_card(pages[1]).locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    visible_card(pages[1]).get_by_role('button', name=DESCRIPTIONS['concentrate'], exact=True).click()
    expect(visible_card(pages[1]).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    source.bring_to_front()
    _evaluate(source_cdp, 'history.replaceState({},"","/browse/another-album")')
    # Natural 195-second playback, with both app tabs backgrounded. No seeks,
    # injected sampling timestamps or forced playback-status refreshes.
    wait_samples(pages[1], 1, timeout=70000)
    record_property('native_first_sample_seconds', time.perf_counter() - phase_started)
    phase_started = time.perf_counter()
    pages[0].goto(live_app + '/artists')
    source.bring_to_front()
    try:
        wait_samples(pages[1], 3, timeout=140000)
    except Exception:
        diagnostics = {
            'owner': context.service_workers[0].evaluate(
                'chrome.storage.local.get("tuneconsole.audio-collection.1")'),
            'app_run': pages[1].evaluate('localStorage.getItem(' + json.dumps(RUN) + ')'),
            'app_view': pages[1].evaluate('localStorage.getItem(' + json.dumps(VIEW) + ')'),
            'source_messages': _evaluate(source_cdp,
                'previewMessages.map(({type,id,error,seconds})=>({type,id,error,seconds}))', isolated),
            'song_position': _evaluate(source_cdp, 'previewSongPosition()'),
            'uploads': uploads,
            'errors': errors,
        }
        offset = _evaluate(source_cdp, 'previewMediaOffset')
        report = Path(f'/tmp/tuneconsole-882-three-sample-failure-{offset}.json')
        report.write_text(json.dumps(diagnostics, indent=2))
        raise
    record_property('native_remaining_samples_seconds', time.perf_counter() - phase_started)
    phase_started = time.perf_counter()
    run_id = pages[0].evaluate('JSON.parse(localStorage.getItem(' + json.dumps(RUN) + ')).id')
    assert len(store.audio_vibes.samples(run_id)) == 3
    samples = sorted(store.audio_vibes.samples(run_id), key=lambda value: value['position_seconds'])
    positions = [value['position_seconds'] for value in samples]
    media_positions = [value['source_position_seconds'] for value in samples]
    offset = _evaluate(source_cdp, 'previewMediaOffset')
    assert positions == pytest.approx([30, 97.5, 130], abs=5)
    assert media_positions == pytest.approx([value + offset for value in positions], abs=.1)
    assert store.audio_vibes.feedback(run_id)['sample_ids'] == []
    assert store.audio_vibes.feedback(run_id)['prediction'] is None
    assert len(workers) == 3 and len(uploads) == 3
    for _, body in uploads:
        payload = json.loads(body)
        assert len(payload['tags']) == 50 and len(payload['traits']) == 6
        assert payload['audio_transferred_to_app'] is False and payload['source_identity_verified'] is False
        assert len(payload['audio_attributes']['values']) == 27
        assert payload['audio_attributes']['runtime'] == 'essentia-csp-audio-2'
        assert payload['audio_attributes']['values']['rms'] == pytest.approx(.1 / 2 ** .5, abs=.002)
        assert payload['audio_attributes']['values']['integrated_lufs'] < -20
        embedding = audio_embedding.validate(payload['embedding'])
        assert embedding['patches'] == 3
        assert min(embedding['values']) < 0
        assert all(math.isfinite(value) for value in embedding['values'])
        assert audio_embedding.signature(payload)
        stored = next(value for value in samples if value['id'] == payload['id'])
        assert stored['embedding'] == embedding
        assert not any(key in body for key in ('"pcm"', '"samples"', '"embeddings"', '"spectrogram"'))
        assert len(body) < 65536
    _released(source_cdp, isolated)
    assert _evaluate(source_cdp, 'document.querySelector("video").paused') is False
    card = visible_card(pages[1])
    expect(card.locator('[data-vibe-prediction]')).to_contain_text('Prediction:')
    card.get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert set(store.audio_vibes.feedback(run_id)['vibes']) == {'concentrate', 'relax'}
    snapshot = tmp_path / 'captured-embeddings.db'
    with closing(sqlite3.connect(snapshot)) as destination:
        store.conn.backup(destination)
    records, skipped = train_vibes.examples_from_db(snapshot, require_embeddings=True)
    assert len(records) == 1 and len(records[0]['sample_ids']) == 3, skipped
    learned = train_vibes.fit(records, feature_set='embeddings_only', baseline_weight=0.)
    assert audio_vibe_model.validate(json.loads(json.dumps(learned))) == learned
    values = train_vibes.example_vector(records[0], learned['features'])
    scores = audio_vibe_model.scores(learned, values, records[0]['baseline'])
    assert max(scores, key=scores.get) in records[0]['labels']
    expect(visible_card(pages[0]).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute('aria-pressed', 'true')
    for page, width in zip(pages, [1440, 1280]):
        expect(visible_card(page).locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(3)
        page.screenshot(path=f'/tmp/tuneconsole-423-natural{"-offset" if offset else ""}-{width}.png')
    # Let native media reach the 195-second boundary without seeking. Leave
    # both app tabs behind the source, then return with the first card retained.
    record_property('native_samples_feedback_training_readback_seconds', time.perf_counter() - phase_started)
    phase_started = time.perf_counter()
    source.bring_to_front()
    _wait(source_cdp, 'previewNextSong', timeout=65)
    record_property('native_natural_song_boundary_seconds', time.perf_counter() - phase_started)
    for page in pages:
        page.bring_to_front()
        expect(page.locator('[data-vibe-count]')).to_have_text('1 of 2 songs')
        assert visible_card(page).get_attribute('data-run-id') == run_id
        expect(visible_card(page).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True)).to_have_attribute('aria-pressed', 'true')
        page.get_by_role('button', name='Next song · 1 more').click()
        expect(visible_card(page).locator('[data-vibe-track]')).to_have_text('Following song')
        expect(visible_card(page).locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
    next_id = visible_card(pages[1]).get_attribute('data-run-id')
    assert visible_card(pages[0]).get_attribute('data-run-id') == next_id
    visible_card(pages[1]).get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True).click()
    expect(visible_card(pages[1]).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
    assert store.audio_vibes.feedback(next_id)['sample_ids'] == []
    assert store.audio_vibes.feedback(next_id)['prediction'] is None
    expect(visible_card(pages[0]).get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)).to_have_attribute('aria-pressed', 'true')
    loaded_assets = []
    for page, width in zip(pages, [1440, 1280]):
        for url in page.evaluate(r'''performance.getEntriesByType('resource')
            .map(row => row.name).filter(url => /\/(audio-vibe[^/]*|audio-capture-session|playback-status)\.js/.test(url))'''):
            data = context.request.get(url).body()
            expected = Path(__file__).parents[2] / 'src/yt_playlist/web/static' / url.split('/')[-1].split('?')[0]
            assert data == expected.read_bytes()
            loaded_assets.append({'width': width, 'url': url, 'sha256': hashlib.sha256(data).hexdigest()})
        page.screenshot(path=f'/tmp/tuneconsole-461-boundary{"-offset" if offset else ""}-{width}.png')
    Path(f'/tmp/tuneconsole-423-natural{"-offset" if offset else ""}-playback.json').write_text(json.dumps({
        'positions': positions, 'media_positions': media_positions, 'media_offset': offset,
        'source_position_at_completion': _evaluate(source_cdp, 'previewSongPosition()'),
        'samples': store.audio_vibes.samples(run_id), 'feedback': store.audio_vibes.feedback(run_id),
        'worker_count': len(workers), 'loaded_assets': loaded_assets,
        'extension_version': extension_version,
        'next_run_id': next_id, 'next_feedback': store.audio_vibes.feedback(next_id)}, indent=2))
    assert errors == []
    record_property('native_capture_decode_message_times', json.dumps(_evaluate(source_cdp,
        '({recordings:previewRecordings,...previewNativeCosts})', isolated)))
    record_property('native_three_sample_journey_seconds', time.perf_counter() - journey_started)
