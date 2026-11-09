"""Festival availability follows real serving outputs, including partial preparation."""
import json
from tests.enrichment_support import complete_entries, complete_discovered
from tests.derived_rating_support import seed_dislike

import pytest

from tests.conftest import seed_catalog_track
from tests.home_fixture_support import (
    home_pages_only, home_page_bytecode, install_home_routes, without_metadata_providers)
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator, native_sql_calls)
from tests.store_fixture_support import file_consumer_store as store
from tests.onboarding_support import publish_personal_album, publish_personal_mix
from tests.test_tv_stream import video
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import account_growth, choice_evidence, home_prepared, into_recently, onboarding, tv


@pytest.fixture(scope='module')
def _pristine_current_schema(vibe_journey_schema):
    assert vibe_journey_schema.execute('SELECT count(*) FROM recordings').fetchone()[0] == 0
    assert vibe_journey_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert vibe_journey_schema._revision_script
    return vibe_journey_schema


def publish_destinations(store, now=1000):
    """Controlled usable output for each new destination; no readiness overrides."""
    with store._lock:
        _publish_destinations(store, now)


def _publish_destinations(store, now):
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


def publish_journey_prefix(store, now=1000, *, vibe_template=None):
    from tests.test_growth_journey import early_journey
    from tests.test_growth_readiness import publish_vibe_menus

    if vibe_template is not None:
        vibe_template.restore(store)
    if not store.conn.execute("SELECT 1 FROM recordings WHERE identity_key='first|artist'").fetchone():
        early_journey(store)
    if not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='prepared-0'").fetchone():
        publish_personal_mix(store, now=now)
    if vibe_template is None:
        publish_vibe_menus(store)
    else:
        from copy import deepcopy
        from tests.recommendation_support import publish_bundles

        # The shared source already owns all 100 real profiles. Add the earlier
        # journey outputs and republish their actual Vibe candidates together;
        # rebuilding numeric profiles would repeat the same completed producer.
        publish_bundles(store, deepcopy(vibe_template.bundles), now, vibes=True)


def finish_full_journey(store, now=1000):
    publish_destinations(store, now=now)
    complete_journey(store, now=now)


def complete_journey(store, now=1000):
    store.settings.set_setting('radio_seed_offer', json.dumps({
        'menu_id': 'current-radio', 'tracks': [
            {'key': f'prepared {i}|artist', 'video_id': f'prepared-{i}'} for i in range(4)]}))
    complete_entries(store, [row[0] for row in store.conn.execute('SELECT id FROM tracks')], now)
    complete_discovered(store, [row[0] for row in store.conn.execute(
        'SELECT identity_key FROM discovered_tracks')], now)


def publish_full_journey(store, now=1000):
    publish_journey_prefix(store, now=now)
    finish_full_journey(store, now=now)


@pytest.fixture
def mix_count_store(tmp_path, monkeypatch, native_sql_calls):
    # These synchronous readiness reads consume committed menus. Snapshot
    # conflict detection and Vibe's genre catalog are independent of mix count.
    with bounded_consumer_schema((('house', 'House'), ('jazz', 'Jazz')), snapshot_guards=False) as schema:
        with _file_consumer_store(tmp_path, schema, snapshot_guards=False) as database:
            with mock_snapshot_coordinator(database, monkeypatch):
                yield database


@pytest.fixture
def growth_web(mix_count_store, home_pages_only, without_metadata_providers, monkeypatch):
    from tests.test_onboarding_routes import _client

    install_home_routes(monkeypatch)
    _, client = _client(mix_count_store)
    with client:
        yield client


@pytest.mark.parametrize('count', [1, 3, 4])
def test_four_eligible_mixes_are_required_everywhere(
        mix_count_store, count, growth_web, monkeypatch, home_pages_only, without_metadata_providers):
    store = mix_count_store
    client = growth_web
    # This gate consumes completed Foundation/Discover prerequisites. Their
    # 48-subject preparation belongs to the journey and readiness tests; keep
    # the earlier playable album format and every counted mix real here.
    monkeypatch.setattr(account_growth.growth_readiness, 'foundation', lambda *args: True)
    monkeypatch.setattr(account_growth.growth_readiness, 'discover', lambda *args: True)
    seed_catalog_track(store, 'first|artist', 'First song', 'Artist', video_id='first')
    publish_personal_album(store, count=2)
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


def test_new_destinations_require_usable_outputs_and_recheck_current_eligibility(store, monkeypatch):
    # Numerical Vibe evidence is a completed lower-layer result here. Its real
    # menu/evidence agreement is owned by test_growth_readiness and the Vibe
    # access test below. Keep actual prerequisite ordering, destination outputs
    # and their live feedback/eligibility reads under test.
    monkeypatch.setattr(type(store.readiness), 'vibe_evidence', lambda *args: (12, 0))
    monkeypatch.setattr(account_growth.growth_readiness, 'vibes', lambda *args: True)
    # Foundation's 48-subject ranked reserve is owned by
    # test_growth_readiness::test_foundation_needs_existing_taste_and_a_sizable_ranked_eligible_reserve.
    # Here its completed result is a prerequisite to the real destination
    # coordinator; late outputs must stay inaccessible until it becomes ready.
    foundation_ready = False
    monkeypatch.setattr(account_growth.growth_readiness, 'foundation',
                        lambda *args: foundation_ready)

    def areas():
        return account_growth.view(store, 1000)['ready_areas']

    assert not any(areas().values())
    publish_destinations(store)
    ready = areas()
    assert not any(ready.values()), 'Later output must not skip the foundation'
    assert not ready['directions'] and not ready['radio']
    seed_catalog_track(store, 'first|artist', 'First song', 'Artist', video_id='first')
    foundation_ready = True
    store.rec_proposals.put_proposals('album_prepared', {
        'built_at': 1000, 'context': {}, 'items': [
            {'browse_id': f'MPRE{i}', 'title': f'Record {i}', 'artist': f'Artist {i}',
             'classification': 'discovery', 'score': .8} for i in range(2)]}, 1000)
    publish_personal_mix(store)
    complete_journey(store)
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


@pytest.fixture(scope='module')
def vibe_journey_schema():
    with bounded_consumer_schema((('house', 'House'), ('jazz', 'Jazz'))) as schema:
        yield schema


@pytest.fixture
def vibe_journey_store(vibe_journey_schema):
    from yt_playlist.core.store import Store

    store = Store(':memory:')
    try:
        initialize_consumer(store, vibe_journey_schema)
        yield store
    finally:
        store.conn.close()


@pytest.fixture
def vibe_journey(vibe_journey_store, monkeypatch):
    from tests.home_fixture_support import publish_menu
    from tests.musical_support import prepare_profiles
    from tests.recommendation_support import seed_publication_subjects

    store = vibe_journey_store
    # This coordinator consumes completed Foundation and Vibe pool decisions.
    # Their independent real capacity/selection owners live in growth_readiness.
    # Keep current SQL sound evidence, every intervening destination and their
    # eligibility reads real, rather than rebuilding 100 unchanged profiles.
    monkeypatch.setattr(account_growth.growth_readiness, 'foundation', lambda *args: True)
    monkeypatch.setattr(account_growth.growth_readiness, 'vibes', lambda *args: True)
    publish_menu(store, tracks_per_card=1)
    seed_catalog_track(store, 'measured|artist', 'Measured', 'Artist', video_id='measured',
                       genre='Jazz', energy=.1, mood_relaxed=.9, mood_happy=.8)
    prepare_profiles(store)
    store.rec_proposals.put_proposals('album_prepared', {'built_at': 1000, 'context': {}, 'items': [
        {'browse_id': f'MPRE{i}', 'title': f'Record {i}', 'artist': f'Artist {i}',
         'classification': 'discovery', 'score': .8} for i in range(2)]}, 1000)
    tracks = [{'key': f'new {i}|artist', 'video_id': f'new-{i}', 'title': f'New {i}',
               'artist': 'Artist', 'genre': 'Jazz'} for i in range(10)]
    seed_publication_subjects(store, tracks, discoveries=[track['key'] for track in tracks])
    store.rec_proposals.put_proposals(home_prepared.DISCOVERY_SURFACE,
        {'discovery_albums': [], 'discovery_artists': [], 'discovery_tracks': tracks}, 1000)
    store.rec_proposals.put_proposals(tv.SURFACE, {'schema': tv.SCHEMA, 'key': 'ready',
        'at': 1000, 'attempted': [], 'items': [video(i) for i in range(tv.QUEUE_SIZE)]}, 1000)
    complete_journey(store)
    return store


def assert_vibe_access_agrees(store, monkeypatch, expected, now=1000, radio=None):
    state = account_growth.view(store, now, radio=radio)
    assert account_growth.has_access(state, 'vibes') is expected
    with monkeypatch.context() as patch:
        patch.setattr(into_recently, 'load', lambda *a, **k: pytest.fail('Vibe access loaded articles'))
        patch.setattr(account_growth.deepseek, 'settings', lambda *a, **k: pytest.fail('Vibe access loaded bonuses'))
        assert account_growth.vibe_access(store, now, radio) is expected


@pytest.mark.parametrize('radio_case', ['prepared', 'empty-offer', 'short-offer', 'no-offer'])
def test_vibe_access_matches_journey_and_omits_unrelated_destinations(vibe_journey, monkeypatch, radio_case):
    store = vibe_journey
    if radio_case == 'prepared':
        assert_vibe_access_agrees(store, monkeypatch, True)
        return
    saved_offer = store.settings.get_setting('radio_seed_offer')
    seeds = json.loads(saved_offer)['tracks'][:3] if radio_case == 'short-offer' else []
    if radio_case == 'no-offer':
        store.settings.delete_setting('radio_seed_offer')
    else:
        store.settings.set_setting('radio_seed_offer', json.dumps({'menu_id': 'changed', 'tracks': seeds}))
    radio = {'state': {'active': True}, 'seedMenu': None if radio_case == 'no-offer' else 'changed', 'seeds': seeds}
    assert_vibe_access_agrees(store, monkeypatch, True, radio=radio)
    assert_vibe_access_agrees(store, monkeypatch, False, radio={**radio, 'state': {'active': False}})


def test_tv_history_keeps_prefix_but_never_invents_vibe_output(vibe_journey, monkeypatch):
    store = vibe_journey
    assert_vibe_access_agrees(store, monkeypatch, True)
    store.rec_proposals.put_proposals(tv.SURFACE, {}, 1001)
    assert_vibe_access_agrees(store, monkeypatch, False, now=1001)
    store.settings.set_setting('tv_draw', '1')
    assert_vibe_access_agrees(store, monkeypatch, True, now=1002)
    with store.conn:
        store.conn.execute('DELETE FROM recording_vibes')
    assert_vibe_access_agrees(store, monkeypatch, False, now=1003)


def test_real_vibe_choice_start_and_withdrawal_control_earned_access(vibe_journey_store, monkeypatch):
    store = vibe_journey_store
    option = {'id': 'chosen', 'label': 'A Jazz mix', 'intent': 'energy_arc', 'genres': ['Jazz']}
    ordinary = choice_evidence.issue(store, 'mixes', [option], 1000, context={})
    assert 'vibe' not in choice_evidence.opportunity(store, ordinary)
    for action in ('choice', 'start'):
        assert choice_evidence.respond(store, ordinary, action, 1001, option_id='chosen', synchronous=True)
    usage = choice_evidence.compile_report(store, 1001)['home_format']
    assert usage['episodes']['mixes'] == 1 and usage['vibe_episodes'] == 0
    identity = choice_evidence.issue(store, 'mixes', [option], 1002, context={}, vibe='relax')
    assert identity != ordinary
    offer = choice_evidence.opportunity(store, identity)
    assert offer['vibe'] == 'relax'
    assert all('descriptor_id' in row and not {'recipe', 'tracks', 'keys'} & row.keys() for row in offer['options'])
    assert choice_evidence.respond(store, identity, 'choice', 1002, option_id='chosen', synchronous=True)
    assert not choice_evidence.compile_report(store, 1002)['home_format']['vibe_episodes']
    assert_vibe_access_agrees(store, monkeypatch, False, now=1003)
    assert choice_evidence.respond(store, identity, 'start', 1004, option_id='chosen', synchronous=True)
    assert choice_evidence.compile_report(store, 1004)['home_format']['vibe_episodes'] == 1
    assert account_growth.retained_access(store, 'vibes')
    assert_vibe_access_agrees(store, monkeypatch, True, now=1004)
    state = account_growth.view(store, 1004)
    vibe = next(node for node in state['nodes'] if node['id'] == 'vibes')
    assert not state['vibes_ready'] and vibe['used'] and vibe['unlocked']
    assert choice_evidence.respond(store, identity, 'withdraw', 1005,
        details={'action_id': f'{identity}:choice:chosen'}, synchronous=True)
    assert not choice_evidence.compile_report(store, 1005)['home_format']['vibe_episodes']
    assert not account_growth.retained_access(store, 'vibes')
    assert_vibe_access_agrees(store, monkeypatch, False, now=1005)
