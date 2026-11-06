"""Background sampling, completed-song cards and durable click-to-label judgments."""
import json
from pathlib import Path
import re
from uuid import uuid4

from playwright.sync_api import expect
import pytest

from tests.test_audio_vibes import sample
from yt_playlist.rec import home_vibe

EXT = Path(__file__).resolve().parents[2] / 'extension'

DESCRIPTIONS = {key: preset['attributes'] for key, preset in home_vibe.PRESETS.items()}
RUN = 'tuneconsole.audio-vibes.run.1'
VIEW = 'tuneconsole.audio-vibes.view.1'
CARDS = 'tuneconsole.audio-vibes.cards.1'


@pytest.fixture
def vibes_enabled(monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')


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
    # Controlled source/transport for card tests. Sample replies are synthetic;
    # the real collection worker, storage, delivery and card UI remain active.
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
