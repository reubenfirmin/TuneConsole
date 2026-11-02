"""An established account keeps usable destinations through maintenance and refits."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.test_growth_destinations import publish_full_journey
from yt_playlist.core import recommendation_invalidation
from yt_playlist.core.store import Store
from yt_playlist.rec import home_vibe, vibe_preparation


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / 'state.db')
    store.init_schema()
    yield store
    store.conn.close()


@pytest.mark.parametrize('width', [1280])
def test_returning_account_retains_destinations_during_refresh(live_app, store, page, monkeypatch, width):
    publish_full_journey(store, now=1)
    prepared = store.rec_proposals.get_proposals('home_tuned')
    prepared['fingerprint']['breadth'] = {'families': {'house': 1}, 'breadth': 0}
    store.rec_proposals.put_proposals('home_tuned', prepared, 1)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('home_format', 'mixes')
    store.settings.set_setting('onboard_setup_complete', '1')
    store.settings.set_setting(home_vibe.SETTING, 'relax')
    page.set_viewport_size({'width': width, 'height': 900})
    page.route('https://i.ytimg.com/**', lambda route: route.fulfill(status=404))
    page.goto(live_app + '/#directions')

    def destinations():
        for name in ('Mix tapes', 'TV'):
            expect(page.get_by_role('tab', name=name, exact=True)).to_be_visible()
        state = page.request.get(live_app + '/home/growth').json()
        assert state['ready_formats']['mixes'] and state['ready_areas']['tv'] and state['vibes_ready']
        nodes = {node['id']: node for node in state['nodes']}
        assert all(nodes[key]['ready'] for key in ('listening', 'tv', 'vibes'))
        assert page.request.get(live_app + '/home/cards').status == 200
        assert page.request.get(live_app + '/home/tv').json()['ready']
        assert page.request.get(live_app + '/home/onboard/status').json()['setup']['complete']
        assert page.request.get(live_app + '/home/into-recently').status == 200

    destinations()
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    page.get_by_role('button', name='Tune these mixes', exact=True).click()
    expect(page.locator('#fp-vibe-tab')).to_be_visible()
    page.get_by_role('button', name='Close mix controls').click()
    # A newer model is due, but the previous policy-compatible fits still serve.
    monkeypatch.setattr(vibe_preparation.audio_vibe_model, 'identity', lambda: 'replacement-model')
    with store._lock, store.conn.atomic():
        recommendation_invalidation.clear_model_display(store.conn)
        recommendation_invalidation.artist_reconciled(store.conn)
        store._evidence_recovery_pending = True
    page.wait_for_function("document.documentElement.dataset.storageCompacting === 'true'")
    destinations()
    # Re-entering Home during maintenance must preserve the returning account.
    page.reload(wait_until='domcontentloaded')
    destinations()
    with store._lock:
        store._evidence_recovery_failed = True
    assert page.request.get(live_app + '/storage-maintenance/status').json()['warning']
    page.reload(wait_until='domcontentloaded')
    destinations()
    with store._lock:
        store._evidence_recovery_pending = False
        store._evidence_recovery_failed = False
    page.wait_for_function("document.documentElement.dataset.storageCompacting === 'false'")
    page.evaluate('Alpine.$data(document.querySelector(".home-growth")).refresh()')
    destinations()
    assert store.settings.get_setting('home_format') == 'mixes'
    assert store.settings.get_setting(home_vibe.SETTING) == 'relax'
    assert store.settings.get_setting('onboard_setup_complete') == '1'
    # Confirm which frontend the running server actually loaded.
    for asset in ('account-growth.js', 'storage-maintenance.js'):
        response = page.request.get(live_app + '/static/' + asset)
        assert response.body() == (Path(__file__).resolve().parents[2] /
            'src/yt_playlist/web/static' / asset).read_bytes()
