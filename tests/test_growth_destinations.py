"""Festival availability follows real serving outputs, including partial preparation."""
import json

import pytest

from tests.conftest import seed_catalog_track
from tests.onboarding_support import publish_personal_mix
from tests.test_tv_stream import video
from yt_playlist.rec import account_growth, home_prepared, into_recently, onboarding, tv


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


def publish_full_journey(store, now=1000):
    from tests.test_growth_journey import early_journey
    from tests.test_growth_readiness import publish_vibe_menus

    if not store.conn.execute("SELECT 1 FROM recordings WHERE identity_key='first|artist'").fetchone():
        early_journey(store)
    if not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='prepared-0'").fetchone():
        publish_personal_mix(store, now=now)
    publish_vibe_menus(store)
    publish_destinations(store, now=now)
    store.settings.set_setting('radio_seed_offer', json.dumps({
        'menu_id': 'current-radio', 'tracks': [
            {'key': f'prepared {i}|artist', 'video_id': f'prepared-{i}'} for i in range(4)]}))
    store.enrichment_queue.mark_enriched([row[0] for row in store.conn.execute('SELECT id FROM tracks')], now)
    store.discovery.mark_discovered_enriched([row[0] for row in store.conn.execute(
        'SELECT identity_key FROM discovered_tracks')], now)


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
        store.rec_model.record_dislike('prepared 0|artist', until=None, now=1000)
        assert not onboarding.prepared_mixes_ready(store, 1000)
        assert not client.get('/home/growth').json()['ready_formats']['mixes']


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
    store.rec_model.record_dislike('old 0|artist', until=None, now=1000)
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
