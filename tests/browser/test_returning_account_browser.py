"""An established account keeps usable destinations through maintenance and refits."""
from pathlib import Path
import logging
import threading
import time
from contextlib import contextmanager

import pytest
from playwright.sync_api import expect

from tests.test_growth_destinations import publish_full_journey
from tests.browser.quiet_action_support import assert_quiet_action
from tests.home_fixture_support import browser_store as store
from yt_playlist.core import recommendation_invalidation
from yt_playlist.core.db_rebuild import build
from yt_playlist.rec import choice_evidence, home_cards, home_prepared, home_vibe, vibe_preparation


@contextmanager
def measured(record_property, phase):
    started = time.perf_counter()
    try:
        yield
    finally:
        record_property(phase + '_seconds', time.perf_counter() - started)


@pytest.mark.parametrize('width', [1280])
def test_returning_account_retains_destinations_during_refresh(
        live_app, store, page, monkeypatch, width, record_property):
    with measured(record_property, 'full_journey_preparation'):
        publish_full_journey(store, now=1)
    prepared = store.rec_proposals.get_proposals('home_tuned')
    prepared['fingerprint']['breadth'] = {'families': {'house': 1}, 'breadth': 0}
    store.rec_proposals.put_proposals('home_tuned', prepared, 1)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('home_format', 'mixes')
    store.settings.set_setting('onboard_setup_complete', '1')
    store.settings.set_setting('bridge_paired', '1')
    store.settings.set_setting(home_vibe.SETTING, 'relax')
    # Publish an actual Relax selection from the compatible prepared Vibe pools,
    # rather than remembering Relax over an unrelated generic display fixture.
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))
    with measured(record_property, 'compatible_relax_selection'):
        selected = service.cards(home_cards.CardsRequest(1, record=False))
    assert len(selected.protos) == 4 and not selected.preparing
    assert all(proto['recipe']['vibe'] == 'relax' for proto in selected.protos)
    home_prepared.remember(store, prepared, selected, 1)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 900})
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.route('https://i.ytimg.com/**', lambda route: route.fulfill(status=404))
    page.goto(live_app + '/#directions')
    # This account has already visited its earned destinations.
    for _ in range(page.evaluate('Alpine.$data(document.querySelector(".home-growth")).newNodes.length')):
        page.locator('.growth-unlock-notice').get_by_role('button', name='Mark as seen').click()

    observations = 0

    def destinations():
        nonlocal observations
        observations += 1
        for name in ('Mix tapes', 'TV'):
            expect(page.get_by_role('tab', name=name, exact=True)).to_be_visible()
        with measured(record_property, f'destination_observation_{observations}'):
            responses = page.evaluate('''async () => Object.fromEntries(await Promise.all([
              ['/home/growth', true], ['/home/cards', false], ['/home/tv', true],
              ['/home/onboard/status', true], ['/home/into-recently', false]
            ].map(async ([path, json]) => {
              const started = performance.now();
              const response = await fetch(path);
              const body = json ? await response.json() : await response.text();
              return [path, {status: response.status, body: json ? body : null,
                milliseconds: performance.now() - started}];
            })))''')
        for path, response in responses.items():
            assert response['status'] == 200, (path, response)
            record_property(f'observation_{observations}_{path}_milliseconds', response['milliseconds'])
        state = responses['/home/growth']['body']
        assert state['ready_formats']['mixes'] and state['ready_areas']['tv'] and state['vibes_ready']
        nodes = {node['id']: node for node in state['nodes']}
        assert all(nodes[key]['ready'] for key in ('listening', 'tv', 'vibes'))
        assert responses['/home/tv']['body']['ready']
        assert responses['/home/onboard/status']['body']['setup']['complete']
        expect(page.locator('#home-mode-cards .gen-card')).to_have_count(4)
        expect(page.locator('#home-mode-cards')).to_have_attribute('data-vibe', 'relax')
        expect(page.locator('#home-mode-cards')).to_have_attribute('data-preparing', 'false')

    def wait_copy(*, failed):
        deadline = time.monotonic() + 30
        while True:
            state = page.request.get(live_app + '/storage-maintenance/status').json()
            if not state['active'] and bool(state['error']) == failed:
                return state
            assert time.monotonic() < deadline, state
            page.wait_for_timeout(100)

    def capture(phase):
        with measured(record_property, 'screenshot_' + phase):
            page.screenshot(path=f'/tmp/tuneconsole-978-returning-account/{phase}-{width}.png', full_page=True)

    destinations()
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    expect(page.locator('#fp-vibe-tab')).to_be_visible()
    expect(page.get_by_role('radio', name='Relax', exact=True)).to_be_checked()
    page.get_by_role('button', name='Close mix controls').click()
    page.evaluate('window.returningDocument = document; window.returningPlayer = document.querySelector(".global-now-playing")')
    # A newer model is due, but the previous policy-compatible fits still serve.
    monkeypatch.setattr(vibe_preparation.audio_vibe_model, 'identity', lambda: 'replacement-model')
    with store._lock, store.conn.atomic():
        recommendation_invalidation.clear_model_display(store.conn)
        recommendation_invalidation.artist_reconciled(store.conn)
        store.settings.set_setting(choice_evidence.STORAGE_PENDING, '1')
        store._evidence_recovery_pending = True
    # Observe the browser's actual next poll: cleanup intent leaves foreground
    # recording available and never supplies the active replacement flag.
    with page.expect_response(lambda response: response.url.endswith('/storage-maintenance/status')
                              and response.json()['evidence_maintenance']['pending']) as routine_poll:
        page.wait_for_function("document.documentElement.dataset.storageCompacting === 'false'")
    routine = routine_poll.value.json()
    assert not routine['active'] and routine['recording']
    assert page.request.post(live_app + '/home/format', form={'panel': 'directions'}).status == 204
    page.evaluate('Alpine.$data(document.querySelector(".home-growth")).refresh()')
    destinations()
    assert page.evaluate('returningDocument === document && returningPlayer === document.querySelector(".global-now-playing")')
    capture('routine-cleanup')
    # Re-entering Home during maintenance must preserve the returning account.
    page.reload(wait_until='domcontentloaded')
    destinations()
    page.evaluate('window.warningDocument = document; window.warningPlayer = document.querySelector(".global-now-playing")')
    # Observe the actual frontend poll reaching the warning state. Technical
    # diagnostics stay internal while the current document remains usable.
    with page.expect_response(lambda response: response.url.endswith('/storage-maintenance/status')
                              and response.json()['warning']) as warning_poll:
        with store._lock:
            store._evidence_recovery_failed = True
    warning = warning_poll.value.json()
    assert not warning['active'] and warning['recording']
    expect(page.locator('#storage-maintenance')).to_have_attribute('role', 'status')
    expect(page.locator('#storage-maintenance')).to_have_attribute('aria-live', 'polite')
    expect(page.locator('#storage-maintenance')).to_be_hidden()
    expect(page.locator('#storage-maintenance')).to_have_text('')
    assert_quiet_action(page)
    destinations()
    assert page.evaluate('warningDocument === document && warningPlayer === document.querySelector(".global-now-playing")')
    page.reload(wait_until='domcontentloaded')
    destinations()
    capture('cleanup-retry')

    # Use the same real admission boundary as the missing-output growth case,
    # retaining this fixture's compatible prepared destinations throughout.
    copying, release = threading.Event(), threading.Event()
    attempts = []
    original_build = build.build

    def held_copy(*args, **kwargs):
        attempts.append(True)
        copying.set()
        assert release.wait(30)
        if len(attempts) == 1:
            raise InterruptedError('Controlled returning-account copy failure')
        with measured(record_property, 'actual_database_copy'):
            return original_build(*args, **kwargs)

    monkeypatch.setattr(build, 'build', held_copy)
    try:
        assert page.request.post(live_app + '/storage-maintenance/compact').status == 202
        assert copying.wait(10)
        page.wait_for_function("document.documentElement.dataset.storageCompacting === 'true'")
        assert store.activity_paused
        destinations()
        assert page.request.post(live_app + '/home/format', form={'panel': 'directions'}).status == 503
        capture('active-copy')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function("document.documentElement.dataset.storageCompacting === 'true'")
        destinations()
        release.set()
        stopped = wait_copy(failed=True)
        assert stopped['warning'] and stopped['recording']
        assert 'Controlled returning-account copy failure' in stopped['error']
        assert not store.activity_paused
        assert store.settings.get_setting(choice_evidence.STORAGE_PENDING) == '1'
        page.wait_for_function("document.documentElement.dataset.storageCompacting === 'false'")
        destinations()
        capture('copy-rejected')
        page.reload(wait_until='domcontentloaded')
        destinations()
        assert page.request.post(live_app + '/storage-maintenance/compact').status == 202
        wait_copy(failed=False)
        assert len(attempts) == 2
        assert not store.activity_paused
        assert store.settings.get_setting(choice_evidence.STORAGE_PENDING) == '1'
    finally:
        release.set()

    # Complete the real cleanup retry rather than discarding its durable cursor.
    with measured(record_property, 'evidence_cleanup'):
        result = choice_evidence.maintain_storage(store, 1)
    assert not result.get('deferred'), result
    assert store.settings.get_setting(choice_evidence.STORAGE_PENDING) == '0'
    with store._lock:
        assert not store._evidence_recovery_pending and not store._evidence_recovery_failed
    page.wait_for_function("document.documentElement.dataset.storageCompacting === 'false'")
    expect(page.locator('#storage-maintenance')).to_be_hidden()
    page.evaluate('Alpine.$data(document.querySelector(".home-growth")).refresh()')
    destinations()
    capture('recovered')
    assert store.settings.get_setting('home_format') == 'mixes'
    assert store.settings.get_setting(home_vibe.SETTING) == 'relax'
    assert store.settings.get_setting('onboard_setup_complete') == '1'
    assert not errors, errors
    # Confirm which frontend the running server actually loaded.
    for asset in ('account-growth.js', 'storage-maintenance.js'):
        response = page.request.get(live_app + '/static/' + asset)
        assert response.body() == (Path(__file__).resolve().parents[2] /
            'src/yt_playlist/web/static' / asset).read_bytes()
