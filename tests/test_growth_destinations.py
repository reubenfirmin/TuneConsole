"""Festival availability follows real serving outputs, including partial preparation."""
import html
import json
import re
from tests.enrichment_support import complete_entries, complete_discovered
from tests.derived_rating_support import seed_dislike

import pytest

from tests.conftest import seed_catalog_track
from tests.onboarding_support import publish_personal_mix
from tests.test_tv_stream import video
from yt_playlist.rec import account_growth, choice_evidence, home_prepared, into_recently, onboarding, tv


def publish_destinations(store, now=1000):
    """Controlled usable output for each new destination; no readiness overrides."""
    tracks = [{'key': f'new {i}|new artist {i}', 'video_id': f'new-song-{i}', 'title': f'New song {i}',
               'artist': f'New artist {i}', 'genre': 'Jazz'} for i in range(10)]
    for track in tracks:
        store.discovery.upsert_discovered_track(track['key'], track['video_id'], track['title'],
            track['artist'], None, None, track['genre'], None, 'radio:seed', now)
    store.rec_proposals.put_proposals(home_prepared.DISCOVERY_SURFACE, {
        'discovery_albums': [], 'discovery_artists': [], 'discovery_tracks': tracks}, now)
    master = next((identity for identity in store.accounts.get_accounts() if identity.is_master), None)
    listener = master.id if master else store.accounts.upsert_account('Main', 'credential', None, True)
    for i in range(12):
        if not store.conn.execute('SELECT 1 FROM youtube_items WHERE video_id=?', (f'old-song-{i}',)).fetchone():
            seed_catalog_track(store, f'old {i}|artist', f'Old favorite {i}', 'Artist', video_id=f'old-song-{i}')
        store.tracks.record_item_rating(listener, f'old-song-{i}', 'LIKE', source='api', now=now)
    store.rec_proposals.put_proposals(home_prepared.REVISIT_SURFACE,
        {'rediscover_tracks': [{'key': f'old {i}|artist'} for i in range(12)]}, now)
    store.rec_proposals.put_proposals(tv.SURFACE, {'schema': tv.SCHEMA, 'key': 'ready',
        'at': now, 'attempted': [], 'items': [video(i) for i in range(12)]}, now)
    card = {'kind': 'artist', 'subject': 'artist:New artist', 'display': 'New artist', 'title': 'New artist',
            'extract': 'Jazz brings improvisation into the music.', 'thumbnail': '/static/icon.png',
            'color': '#4269aa', 'seed': 1, 'genre': 'Jazz', 'depth': 0,
            'url': 'https://en.wikipedia.org/wiki/Jazz', 'songs': tracks[:3]}
    store.rec_proposals.put_proposals(into_recently.SURFACE,
        {'schema': into_recently.SCHEMA, 'built_at': now, 'cards': [
            {**card, 'subject': f'artist:New artist {i}', 'title': f'New artist {i}',
             'display': f'New artist {i}'} for i in range(3)]}, now)


def publish_journey_prefix(store, now=1000):
    from tests.test_growth_journey import early_journey
    from tests.test_growth_readiness import publish_vibe_menus

    if not store.conn.execute("SELECT 1 FROM recordings WHERE identity_key='first|artist'").fetchone():
        early_journey(store)
    if not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='prepared-0'").fetchone():
        publish_personal_mix(store, now=now)
    publish_vibe_menus(store)


def finish_full_journey(store, now=1000):
    publish_destinations(store, now=now)
    store.settings.set_setting('radio_seed_offer', json.dumps({
        'menu_id': 'current-radio', 'tracks': [
            {'key': f'prepared {i}|artist', 'video_id': f'prepared-{i}'} for i in range(4)]}))
    complete_entries(store, [row[0] for row in store.conn.execute('SELECT id FROM tracks')], now)
    complete_discovered(store, [row[0] for row in store.conn.execute(
        'SELECT identity_key FROM discovered_tracks')], now)


def publish_full_journey(store, now=1000):
    publish_journey_prefix(store, now=now)
    finish_full_journey(store, now=now)


@pytest.mark.parametrize('count', [1, 3, 4])
def test_four_eligible_mixes_are_required_everywhere(store, count):
    from tests.test_onboarding_routes import _client

    _, client = _client(store)
    from tests.test_growth_journey import early_journey
    early_journey(store)
    store.settings.set_setting('last_sync_at', '1')
    publish_personal_mix(store, count=count)
    expected = count == 4
    for _ in range(2):
        home = client.get('/')
        state = client.get('/home/growth').json()
        setup = client.get('/home/onboard/status').json()['setup']
        assert state['ready_formats']['mixes'] is expected
        assert state['ready_areas']['directions'] is expected
        assert setup['mixes_ready'] is expected
        assert setup['complete'] is True  # Long play is an earlier usable format.
        assert 'id="onboarding-feed"' not in home.text
    if expected:
        seed_dislike(store, 'prepared 0|artist', until=None, now=1000)
        # Feedback schedules new preparation; serving keeps the published board.
        assert onboarding.prepared_mixes_ready(store, 1000)
        assert client.get('/home/growth').json()['ready_formats']['mixes']
        assert client.get('/home/onboard/status').json()['setup']['mixes_ready']


def test_new_destinations_require_usable_outputs_and_recheck_current_eligibility(store):
    def areas():
        return account_growth.view(store, 1000)['ready_areas']

    assert not any(areas().values())
    publish_destinations(store)
    ready = areas()
    assert not any(ready.values()), 'Later output must not skip the foundation'
    assert not ready['directions'] and not ready['radio']
    publish_full_journey(store)
    assert all(areas().values())
    store.rec_model.record_feedback('for_you', 'new 0|new artist 0', 'dismiss', now=1000)
    seed_dislike(store, 'old 0|artist', until=None, now=1000)
    store.rec_proposals.put_proposals(tv.SURFACE, {'schema': tv.SCHEMA, 'key': 'empty',
        'at': 1000, 'attempted': [], 'items': []}, 1000)
    store.rec_proposals.put_proposals(into_recently.SURFACE,
        {'schema': into_recently.SCHEMA, 'built_at': 1000, 'cards': [{'extract': 'Incomplete'}]}, 1000)
    unavailable = areas()
    assert not any(unavailable[key] for key in ('tv', 'discover', 'rediscover', 'recent'))


def test_only_next_connected_destinations_are_preparing(store):
    seed_catalog_track(store, 'arriving|artist', 'Arriving', 'Artist', video_id='arriving')
    def nodes():
        return {node['id']: node for node in account_growth.view(store, 1000)['nodes']}

    initial = nodes()
    assert initial['foundation']['state'] == 'preparing'
    assert all(initial[key]['state'] == 'untouched'
               for key in ('albums', 'radio', 'vibes', 'tv', 'discover', 'rediscover', 'recent'))
    publish_personal_mix(store)
    next_stops = nodes()
    assert next_stops['foundation']['state'] == 'preparing'
    assert all(next_stops[key]['state'] == 'untouched' for key in ('albums', 'vibes', 'tv', 'rediscover', 'recent'))


def test_prepared_radio_cannot_skip_other_formats_or_reconnect_to_the_entrance(store):
    from tests.test_account_growth import catalog

    catalog(store)
    store.settings.delete_setting('last_sync_at')
    store.settings.set_setting('radio_seed_offer', json.dumps({
        'tracks': [{'video_id': f'video-{i}', 'key': f'song {i}|artist'} for i in range(4)],
        'menu_id': 'prepared-radio'}))
    stops = {node['id']: node for node in account_growth.view(store, 1000)['nodes']}
    assert stops['radio']['usable'] and not stops['radio']['ready']
    assert not stops['listening']['ready'] and not stops['albums']['ready']
    assert stops['radio']['parent'] == 'listening'
    assert stops['first']['ready']


@pytest.mark.parametrize('mixes,albums,radio', [(False, False, False), (False, True, False),
                                              (True, False, False), (True, True, True)])
def test_available_destinations_have_a_continuous_path_even_when_preparation_finishes_out_of_order(
        store, mixes, albums, radio):
    from tests.test_account_growth import catalog, ready_radio

    catalog(store)
    from tests.test_growth_journey import early_journey
    early_journey(store)
    publish_destinations(store)
    state = account_growth.view(store, 1000, mixes_ready=mixes, albums_ready=albums,
                               radio=ready_radio() if radio else {})
    stops = {node['id']: node for node in state['nodes']}
    for stop in stops.values():
        if stop['id'] == 'first' or stop['optional'] or not stop['ready']:
            continue
        ancestors = set()
        current = stop
        while current['id'] != 'first':
            assert current['id'] not in ancestors
            ancestors.add(current['id'])
            current = stops[current['parent']]
            assert current['ready'], (stop['id'], current['id'])
    if albums and not radio:
        assert stops['albums']['parent'] == 'foundation'


@pytest.mark.parametrize('format', [None, 'mixes', 'albums', 'radio', 'vibes', 'tv'])
def test_retained_access_matches_the_existing_journey_without_claiming_ready_output(store, format):
    from tests.home_fixture_support import returning_listener

    if format == 'tv':
        store.settings.set_setting('tv_draw', '1')
    elif format:
        returning_listener(store, format)
    state = account_growth.view(store, 1000)
    for area in ('directions', 'album-choices', 'radio', 'tv', 'discover', 'vibes', 'recent', 'rediscover'):
        assert account_growth.retained_access(store, area) == account_growth.has_access(state, area)
    assert not any(state['ready_areas'].values())
    assert not state['vibes_ready']


def test_earned_mix_guard_does_not_assemble_later_vibe_readiness(store, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.home_fixture_support import publish_menu, returning_listener
    from yt_playlist.web.app import create_app

    publish_menu(store, now=1000)
    returning_listener(store, 'mixes', now=1000)
    monkeypatch.setattr(account_growth, 'view', lambda *args, **kwargs:
                        pytest.fail('An earned Mix fragment guard assembled the whole journey'))
    with TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False),
                    base_url='http://127.0.0.1') as client:
        response = client.get('/home/cards')
    assert response.status_code == 200 and 'gen-wheelhouse' in response.text


def test_vibe_access_matches_journey_and_omits_unrelated_destinations(
        store, monkeypatch, full_journey_prefix, caplog):
    from fastapi.testclient import TestClient
    from yt_playlist.web.app import create_app

    full_journey_prefix.restore(store)
    finish_full_journey(store)
    def agrees(now, radio=None):
        expected = account_growth.has_access(account_growth.view(store, now, radio=radio), 'vibes')
        with monkeypatch.context() as patch:
            patch.setattr(into_recently, 'load', lambda *a, **k: pytest.fail('Vibe access loaded articles'))
            patch.setattr(account_growth.deepseek, 'settings', lambda *a, **k: pytest.fail('Vibe access loaded bonuses'))
            assert account_growth.vibe_access(store, now, radio) is expected
        return expected
    assert agrees(1000)
    saved_offer = store.settings.get_setting('radio_seed_offer')
    for seeds in ([], json.loads(saved_offer)['tracks'][:3]):
        store.settings.set_setting('radio_seed_offer', json.dumps({'menu_id': 'changed', 'tracks': seeds}))
        radio = {'state': {'active': True}, 'seedMenu': 'changed', 'seeds': seeds}
        assert agrees(1000, radio)
        assert not agrees(1000, {**radio, 'state': {'active': False}})
    store.settings.delete_setting('radio_seed_offer')
    radio = {'state': {'active': True}, 'seedMenu': None, 'seeds': []}
    assert agrees(1000, radio)
    assert not agrees(1000, {**radio, 'state': {'active': False}})
    store.settings.set_setting('radio_seed_offer', saved_offer)
    # Downstream Vibe preparation must not skip an unfinished required TV stop.
    store.rec_proposals.put_proposals(tv.SURFACE, {}, 1001)
    assert not agrees(1001)
    # Deliberate prior TV use retains the preceding route even during refresh.
    store.settings.set_setting('tv_draw', '1')
    assert agrees(1002)
    # Capture the actual rendered compact menu, rather than authoring a report
    # or retaining a hidden recipe in a synthetic historical opportunity.
    with TestClient(create_app(store, lambda: {}, now_fn=lambda: 1002,
                              background_work=False), base_url='http://127.0.0.1') as client:
        ordinary = client.get('/')
        assert ordinary.status_code == 200
        ordinary_recipes = [json.loads(html.unescape(value)) for value in
                            re.findall(r"data-recipe='([^']*)'", ordinary.text)]
        assert len(ordinary_recipes) == 4
        for displayed in ordinary_recipes:
            assert displayed.get('opportunity_id')
            captured = choice_evidence.opportunity(store, displayed['opportunity_id'])
            assert captured and 'vibe' not in captured
            assert any(str(row['id']) == displayed['option_id'] for row in captured['options'])
        assert not any('Mix choice capture failed' in record.getMessage() for record in caplog.records)
        ordinary_recipe = ordinary_recipes[0]
        for action in ('choice', 'start'):
            assert choice_evidence.respond(store, ordinary_recipe['opportunity_id'], action, 1002,
                option_id=ordinary_recipe['option_id'], synchronous=True)
        usage = choice_evidence.compile_report(store, 1002)['home_format']
        assert usage['episodes']['mixes'] == 1 and usage['vibe_episodes'] == 0
        response = client.post('/home/vibe', data={'vibe': 'relax'})
        assert response.status_code == 200
        assert client.app.state.ctx.work.wait(20)
        assert client.get('/home/vibe/status').json()['phase'] == 'ready'
        response = client.get('/home/cards')
    assert response.status_code == 200
    recipes = [json.loads(html.unescape(value)) for value in
               re.findall(r"data-recipe='([^']*)'", response.text)]
    assert len(recipes) == 4 and all(recipe['vibe'] == 'relax' for recipe in recipes)
    recipe = recipes[0]
    identity, option = recipe['opportunity_id'], recipe['option_id']
    assert identity != ordinary_recipe['opportunity_id']
    offer = choice_evidence.opportunity(store, identity)
    assert offer['vibe'] == 'relax'
    assert all('descriptor_id' in row and not {'recipe', 'tracks', 'keys'} & row.keys()
               for row in offer['options'])
    assert choice_evidence.respond(store, identity, 'choice', 1002, option_id=option,
                                   synchronous=True)
    assert not choice_evidence.compile_report(store, 1002)['home_format']['vibe_episodes']
    # Historical TV access cannot fabricate usable specialist Vibe output.
    with store.conn:
        store.conn.execute('DELETE FROM recording_vibes')
    assert not agrees(1003)
    # Only confirmed playback earns access; output remains genuinely unready.
    assert choice_evidence.respond(store, identity, 'start', 1004, option_id=option,
                                   synchronous=True)
    assert choice_evidence.compile_report(store, 1004)['home_format']['vibe_episodes'] == 1
    assert account_growth.retained_access(store, 'vibes') and agrees(1004)
    state = account_growth.view(store, 1004)
    vibe = next(node for node in state['nodes'] if node['id'] == 'vibes')
    assert not state['vibes_ready'] and vibe['used'] and vibe['unlocked']
    # Corrections withdraw the real evidence instead of leaving a permanent flag.
    assert choice_evidence.respond(store, identity, 'withdraw', 1005,
        details={'action_id': f'{identity}:choice:{option}'}, synchronous=True)
    assert not choice_evidence.compile_report(store, 1005)['home_format']['vibe_episodes']
    assert not account_growth.retained_access(store, 'vibes') and not agrees(1005)
