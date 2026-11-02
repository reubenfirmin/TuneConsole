"""Completed audio analyses remain in the browser outbox across maintenance."""
import json

from playwright.sync_api import expect

from tests.test_audio_vibes import sample


def test_completed_audio_sample_retries_after_maintenance_and_page_reload(live_app, page, store):
    payload = sample()
    outbox = 'tuneconsole.audio-vibes.outbox.1'
    attempts = []
    store._evidence_recovery_pending = True
    page.add_init_script('if (!localStorage.getItem(' + json.dumps(outbox) + ')) '
        'localStorage.setItem(' + json.dumps(outbox) + ',' + json.dumps(json.dumps([payload])) + ');')
    page.on('response', lambda response: attempts.append(response.status)
        if response.url.endswith('/audio-vibes/samples') else None)
    page.goto(live_app + '/playlists')
    expect(page.locator('#storage-maintenance')).to_have_text('Updating library storage; browsing and playback remain available.')
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "true"')
    page.wait_for_function('window.performance.getEntriesByType("resource").some('
                          'r => r.name.endsWith("/audio-vibes/samples"))')
    assert 503 in attempts
    assert page.evaluate('(key) => JSON.parse(localStorage.getItem(key))', outbox) == [payload]
    assert not store.conn.execute('SELECT 1 FROM sample_attributes').fetchone()
    page.reload()
    expect(page.locator('#storage-maintenance')).to_have_text('Updating library storage; browsing and playback remain available.')
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "true"')
    assert page.evaluate('(key) => JSON.parse(localStorage.getItem(key))', outbox) == [payload]
    store._evidence_recovery_pending = False
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "false"')
    expect(page.locator('#storage-maintenance')).to_be_hidden()
    page.wait_for_function('(key) => JSON.parse(localStorage.getItem(key)).length === 0', arg=outbox,
                           timeout=15000)
    assert 200 in attempts
    assert store.conn.execute('SELECT count(*) FROM sample_attributes').fetchone()[0] == 1
    assert store.audio_vibes.samples(payload['run_id'])[0]['id'] == payload['id']
    response = page.request.post(live_app + '/audio-vibes/samples', data=payload)
    assert response.status == 200
    assert store.conn.execute('SELECT count(*) FROM sample_attributes').fetchone()[0] == 1
