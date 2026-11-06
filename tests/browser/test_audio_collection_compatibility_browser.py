"""Actual installed legacy transport must leave immediate human cards usable."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket

from playwright.sync_api import expect
import pytest

from tests.browser.test_audio_vibes_browser import DESCRIPTIONS, RUN, playback_route, visible_card, vibes_enabled


INSTALLED = Path('/home/rfirmin/.config/google-chrome/Profile 1/Extensions/'
                 'opmmbolbdgdkphfocffpjpgmelkkjkcn/0.1.23_0')
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def legacy_collection_browser(vibes_enabled, live_app, playwright, browser_type_launch_args, tmp_path):
    if not INSTALLED.is_dir():
        pytest.skip('Actual installed legacy0.1.23 package is unavailable')
    extension = tmp_path / 'legacy-extension'
    shutil.copytree(INSTALLED, extension)
    manifest = json.loads((extension / 'manifest.json').read_text())
    assert manifest['version'] == '0.1.23'
    assert not (extension / 'worker/audio_collection.js').exists()
    # Only identity and private backend coordinates change. Execute the actual
    # installed worker/preview modules, rather than a simulated legacy reply.
    manifest['key'] = json.loads((ROOT / 'extension/manifest.json').read_text())['key']
    manifest['host_permissions'] = [live_app + '/*' if value == 'http://127.0.0.1:8765/*' else value
                                    for value in manifest['host_permissions']]
    (extension / 'manifest.json').write_text(json.dumps(manifest))
    with socket.socket() as unused:
        unused.bind(('127.0.0.1', 0))
        unused_port = unused.getsockname()[1]
    config = extension / 'worker/config.js'
    config.write_text(config.read_text().replace('appOrigin: "http://127.0.0.1:8765"',
        'appOrigin: ' + json.dumps(live_app)).replace('ws://127.0.0.1:8765',
        f'ws://127.0.0.1:{unused_port}'))
    hashes = {}
    for name in ('background.js', 'worker/audio_preview.js', 'nowplaying_main.js'):
        assert (extension / name).read_bytes() == (INSTALLED / name).read_bytes()
        hashes[name] = hashlib.sha256((extension / name).read_bytes()).hexdigest()
    context = playwright.chromium.launch_persistent_context(
        tmp_path / 'legacy-profile', **{**browser_type_launch_args, 'channel': 'chromium',
            'env': {**browser_type_launch_args.get('env', os.environ),
                    'XDG_CONFIG_HOME': str(tmp_path / 'config'),
                    'XDG_CACHE_HOME': str(tmp_path / 'cache')}},
        viewport={'width': 1440, 'height': 1000}, ignore_default_args=['--mute-audio'], args=[
            f'--disable-extensions-except={extension}', f'--load-extension={extension}',
            '--disable-audio-output'])
    try:
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
        assert worker.url.endswith('/background.js')
        assert worker.evaluate('chrome.runtime.getManifest().version') == '0.1.23'
        yield context, hashes
    finally:
        context.close()


def test_installed_legacy_handshake_keeps_zero_check_feedback_and_pending_cards(
        legacy_collection_browser, live_app, store):
    context, hashes = legacy_collection_browser
    exercise_legacy_cards(context, live_app, store, hashes, 'installed')


def test_legacy_module_with_controlled_ports_keeps_cards_usable(vibes_enabled, browser, live_app, store):
    if not INSTALLED.is_dir():
        pytest.skip('Actual installed legacy0.1.23 package is unavailable')
    source = (INSTALLED / 'worker/audio_preview.js').read_text()
    bundle = json.loads((INSTALLED / 'audio_model/vendor/bundle.json').read_text())
    # Execute the installed legacy module, with controlled transport. This
    # independently exercises the rendered fallback even if full Chrome cannot
    # start; it does not establish an installed service worker or real capture.
    with browser.new_context(viewport={'width': 1440, 'height': 1000}) as context:
        context.route('**/legacy-installed-bundle.json', lambda route: route.fulfill(json=bundle))
        context.add_init_script('(() => {\n' + source + '\n' + '''
          window.chrome ||= {};
          const preview=createAudioPreview({appOrigin:location.origin,
            pickTab(){throw Error('Legacy fallback must not request capture');}});
          chrome.runtime={getURL:()=>location.origin+'/legacy-installed-bundle.json',
            connect(_id,options){
              const messages=[],remoteMessages=[],localCloses=[],remoteCloses=[];
              let closed=false;
              const port={postMessage:data=>remoteMessages.forEach(fn=>fn(data)),
                onMessage:{addListener:fn=>messages.push(fn)},
                onDisconnect:{addListener:fn=>localCloses.push(fn)},
                disconnect(){closed=true;remoteCloses.forEach(fn=>fn());}};
              const remote={name:options.name,sender:{url:location.href,frameId:0},
                postMessage:data=>{if(!closed) messages.forEach(fn=>fn(data));},
                onMessage:{addListener:fn=>remoteMessages.push(fn)},
                onDisconnect:{addListener:fn=>remoteCloses.push(fn)},
                disconnect(){closed=true;localCloses.forEach(fn=>fn());}};
              queueMicrotask(()=>preview.connect(remote));
              return port;
            }};
        })();''')
        exercise_legacy_cards(context, live_app, store,
            {'worker/audio_preview.js': hashlib.sha256(source.encode()).hexdigest()}, 'controlled')


def exercise_legacy_cards(context, live_app, store, hashes, transport):
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script('''(() => {
      window.collectionTransport = [];
      const connect = chrome.runtime.connect.bind(chrome.runtime);
      chrome.runtime.connect = (...args) => {
        const port = connect(...args);
        port.onMessage.addListener(data => collectionTransport.push({event:'message',data}));
        port.onDisconnect.addListener(() => collectionTransport.push({event:'disconnected'}));
        const disconnect = port.disconnect.bind(port);
        port.disconnect = () => {collectionTransport.push({event:'local-disconnect'});disconnect();};
        return port;
      };
    })();''')
    state = {'video_id': 'legacy_first', 'title': 'Legacy first song', 'position': 1, 'duration': 240}
    context.route('**/bridge/status', lambda route: playback_route(route, state))
    try:
        page.goto(live_app + '/playlists')
        card = visible_card(page)
        expect(card).to_be_visible(timeout=6000)
        expect(card.locator('[data-vibe-track]')).to_have_text('Legacy first song')
        expect(card.locator('[data-vibe-checks] [data-complete="true"]')).to_have_count(0)
        expect(card.locator('[data-vibe-prediction]')).to_contain_text('Update')
        expect(card.get_by_role('link', name='Update extension')).to_have_attribute(
            'href', 'https://chromewebstore.google.com/detail/opmmbolbdgdkphfocffpjpgmelkkjkcn')
        first = card.get_attribute('data-run-id')
        card.get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True).click()
        expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved')
        assert store.audio_vibes.feedback(first)['sample_ids'] == []
        assert store.audio_vibes.feedback(first)['prediction'] is None
        page.goto(live_app + '/artists')
        expect(visible_card(page).get_by_role('button', name=DESCRIPTIONS['cruise'], exact=True)).to_have_attribute(
            'aria-pressed', 'true')
        page.reload()
        expect(visible_card(page).locator('[data-vibe-track]')).to_have_text('Legacy first song')
        state.update(video_id='legacy_second', title='Legacy second song')
        page.evaluate('playbackStatus.refresh()')
        expect(page.locator('[data-vibe-count]')).to_have_text('1 of 2 songs')
        assert visible_card(page).get_attribute('data-run-id') == first
        page.get_by_role('button', name='Next song · 1 more').click()
        expect(visible_card(page).locator('[data-vibe-track]')).to_have_text('Legacy second song')
        second = visible_card(page).get_attribute('data-run-id')
        assert second != first
        visible_card(page).get_by_role('button', name=DESCRIPTIONS['relax'], exact=True).click()
        expect(visible_card(page).locator('[data-vibe-feedback-status]')).to_have_text('Saved')
        assert store.audio_vibes.feedback(second)['sample_ids'] == []
        # A natural wrap of the same song is another occurrence, with the old
        # pending judgment still attached to its original occurrence.
        state['position'] = 220
        page.evaluate('playbackStatus.refresh()')
        page.wait_for_function('JSON.parse(localStorage.getItem("tuneconsole.audio-vibes.run.1")).last_position===220')
        state['position'] = 1
        page.evaluate('playbackStatus.refresh()')
        expect(page.locator('[data-vibe-count]')).to_have_text('2 of 3 songs')
        page.get_by_role('button', name='Next song · 2 more').click()
        expect(page.locator('[data-vibe-count]')).to_have_text('3 of 3 songs')
        third = visible_card(page).get_attribute('data-run-id')
        assert third not in (first, second)
        assert store.audio_vibes.feedback(second)['vibes'] == ['relax']
        assert not store.conn.execute('SELECT 1 FROM sample_attributes').fetchone()
        assert not errors, errors
    finally:
        page.screenshot(path=f'/tmp/tuneconsole-1060-legacy-{transport}-card.png', full_page=True)
        Path(f'/tmp/tuneconsole-1060-legacy-{transport}-browser.json').write_text(json.dumps({
            'transport_kind': transport,
            'extension_version': '0.1.23', 'installed_hashes': hashes, 'browser': page.evaluate('navigator.userAgent'),
            'transport': page.evaluate('window.collectionTransport'), 'errors': errors,
            'card_text': page.locator('[data-audio-vibe-banner]').inner_text(),
            'run': page.evaluate('localStorage.getItem(' + json.dumps(RUN) + ')'),
            'entry_urls': page.evaluate('performance.getEntriesByType("resource").map(row=>row.name)'
                '.filter(url=>url.includes("audio-vibe"))')}, indent=2))
