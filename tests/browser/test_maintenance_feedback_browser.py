"""Legacy samples gain durable extension custody before maintenance retries."""
import json
from pathlib import Path
import shutil
import time

from playwright.sync_api import expect
import pytest

from tests.test_audio_vibes import sample

COLLECTION = 'tuneconsole.audio-collection.1'


@pytest.fixture
def collection_browser(playwright, browser_type_launch_args, live_app, tmp_path):
    extension = tmp_path / 'extension'
    shutil.copytree(Path(__file__).resolve().parents[2] / 'extension', extension)
    port = live_app.rsplit(':', 1)[1]
    config = extension / 'worker' / 'config.js'
    config.write_text(config.read_text().replace(':8765', ':' + port))
    manifest = extension / 'manifest.json'
    value = json.loads(manifest.read_text())
    value['host_permissions'] = [permission.replace(':8765', ':' + port)
                                 for permission in value['host_permissions']]
    manifest.write_text(json.dumps(value))
    context = playwright.chromium.launch_persistent_context(
        tmp_path / 'profile', **{**browser_type_launch_args, 'channel': 'chromium'},
        args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}',
              '--disable-audio-output'])
    try:
        context.route('https://tuneconsole.com/**', lambda route: route.fulfill(
            content_type='text/html', body='<h1>Private extension setup</h1>'))
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
        worker.evaluate('() => tuneConsole.audioCollection.loaded')
        yield context, worker, context.new_page()
    finally:
        context.close()


def eventually(page, predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, 'Extension custody did not reach the expected state'
        page.wait_for_timeout(25)


def test_completed_audio_sample_retries_after_maintenance_and_page_reload(live_app, collection_browser, store):
    context, worker, page = collection_browser
    payload = sample()
    outbox = 'tuneconsole.audio-vibes.outbox.1'
    attempts = []
    store.activity_paused = True
    page.add_init_script('if (!localStorage.getItem(' + json.dumps(outbox) + ')) '
        'localStorage.setItem(' + json.dumps(outbox) + ',' + json.dumps(json.dumps([payload])) + ');')
    context.on('response', lambda response: attempts.append(response.status)
        if response.url.endswith('/audio-vibes/samples') else None)
    # Delay the real durable write. No acknowledgement may clear the document
    # outbox until the original chrome.storage.local.set has completed.
    worker.evaluate('''({key, id}) => {
      const storage = chrome.storage.local, original = storage.set.bind(storage);
      globalThis.custodyGate = {waiting: false, released: false};
      storage.set = async values => {
        if (!custodyGate.released && values[key]?.outbox?.some(sample => sample.id === id)) {
          custodyGate.waiting = true;
          await new Promise(resolve => { custodyGate.release = resolve; });
        }
        return original(values);
      };
    }''', {'key': COLLECTION, 'id': payload['id']})
    page.goto(live_app + '/playlists')
    expect(page.locator('#storage-maintenance')).to_have_text(
        'Optimizing app storage; some recommendations are temporarily unavailable.')
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "true"')
    eventually(page, lambda: worker.evaluate('custodyGate.waiting'))
    assert page.evaluate('(key) => JSON.parse(localStorage.getItem(key))', outbox) == [payload]
    assert worker.evaluate('async key => (await chrome.storage.local.get(key))[key].outbox', COLLECTION) == []
    with context.expect_event('response', predicate=lambda response:
            response.url.endswith('/audio-vibes/samples') and response.status == 503):
        worker.evaluate('() => { custodyGate.released = true; custodyGate.release(); }')
    page.wait_for_function('(key) => JSON.parse(localStorage.getItem(key)).length === 0', arg=outbox)
    assert worker.evaluate('async key => (await chrome.storage.local.get(key))[key].outbox', COLLECTION) == [payload]
    assert 503 in attempts
    assert not store.conn.execute('SELECT 1 FROM sample_attributes').fetchone()
    page.reload()
    expect(page.locator('#storage-maintenance')).to_have_text(
        'Optimizing app storage; some recommendations are temporarily unavailable.')
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "true"')
    assert page.evaluate('(key) => JSON.parse(localStorage.getItem(key))', outbox) == []
    assert worker.evaluate('async key => (await chrome.storage.local.get(key))[key].outbox', COLLECTION) == [payload]
    assert not store.conn.execute('SELECT 1 FROM sample_attributes').fetchone()
    with context.expect_event('response', predicate=lambda response:
            response.url.endswith('/audio-vibes/samples') and response.status == 200, timeout=15000):
        store.activity_paused = False
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "false"')
    # This fixture still has independent startup preparation pending. Ending
    # maintenance must clear its message and recording pause, not hide that state.
    expect(page.locator('#storage-maintenance')).not_to_contain_text('Optimizing app storage')
    expect(page.locator('#storage-maintenance')).to_have_attribute('data-warning', 'false')
    eventually(page, lambda: worker.evaluate(
        'async key => (await chrome.storage.local.get(key))[key].outbox.length === 0', COLLECTION))
    assert 200 in attempts
    assert store.conn.execute('SELECT count(*) FROM sample_attributes').fetchone()[0] == 1
    assert store.audio_vibes.samples(payload['run_id'])[0]['id'] == payload['id']
    # Replay the same legacy UUID through the actual handoff and worker uploader.
    page.evaluate('({key, payload}) => localStorage.setItem(key, JSON.stringify([payload]))',
                  {'key': outbox, 'payload': payload})
    with context.expect_event('response', predicate=lambda response:
            response.url.endswith('/audio-vibes/samples') and response.status == 200, timeout=15000):
        page.reload()
    eventually(page, lambda: worker.evaluate(
        'async key => (await chrome.storage.local.get(key))[key].outbox.length === 0', COLLECTION))
    page.wait_for_function('(key) => JSON.parse(localStorage.getItem(key)).length === 0', arg=outbox)
    assert store.conn.execute('SELECT count(*) FROM sample_attributes').fetchone()[0] == 1
    assert attempts.count(200) >= 2
