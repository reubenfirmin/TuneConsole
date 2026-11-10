"""Growth reports describe actual music state, independently of notice dismissal."""
from tests.conftest import seed_catalog_track
from tests.enrichment_support import complete_entries, complete_discovered
from tests.test_artist_start import Catalog, client_for
from yt_playlist.rec import account_growth, home_preferences


def catalog(store, processed=9, total=10):
    store.settings.set_setting('last_sync_at', '900')
    for index in range(total):
        entry = seed_catalog_track(store, f'song {index}|artist', f'Song {index}', 'Artist',
                                   video_id=f'video-{index}')
        if index < processed:
            complete_entries(store, [entry], 1)


def ready_radio():
    return {'seeds': [{'video_id': f'video-{i}', 'key': f'song {i}|artist'} for i in range(4)],
            'seedMenu': 'real-menu', 'state': {}}


def nodes(state):
    return {node['id']: node for node in state['nodes']}


def test_guidance_follows_artist_picks_then_owned_music_without_demanding_more_listens(store):
    from yt_playlist.library.playlist_generated import create_generated_playlist
    from yt_playlist.rec import artist_start

    def guidance():
        return nodes(account_growth.view(store, 1000, mixes_ready=False, albums_ready=False, radio={}))

    assert 'Choose 3 artists' in guidance()['albums']['how_to']
    fake = Catalog()
    account = store.accounts.upsert_account('main', 'credential', None, True)
    for page in fake._artists.values():
        for song in page['songs']['results']:
            fake._catalog[song['videoId']] = song
    artists = artist_start.ArtistCatalog(lambda: {account: fake})
    for index in range(3):
        artists.select(store, f'UCartist{index}', True, 1000.)
    # Mirroring the generated set must not switch an empty-library listener to
    # instructions that presume an existing personal collection.
    create_generated_playlist(store, 'My first set', store.discovery.starter_tracks(), fake, 1000., account)
    picked = guidance()
    assert 'artists you chose' in picked['albums']['how_to']
    assert 'Play your first set' in picked['albums']['how_to']
    assert 'Save playlists or albums' in picked['rediscover']['how_to']
    assert 'saved playlists and albums' not in picked['albums']['how_to']

    seed_catalog_track(store, 'owned|artist', 'Owned song', 'Artist', video_id='owned')
    owned = guidance()
    for identity in ('foundation', 'albums', 'discover', 'listening', 'radio'):
        assert 'saved playlists and albums' in owned[identity]['how_to']
        assert 'Play your first set' not in owned[identity]['how_to']
        assert 'Choose 3 artists' not in owned[identity]['how_to']
    assert 'Keep TuneConsole open' in owned['albums']['how_to']
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 0
    for state in (picked, owned):
        for node in state.values():
            copy = ' '.join(node[field] for field in ('detail', 'how_to', 'status')).lower()
            assert 'taste model' not in copy and 'prepar' not in copy
            assert 'connect the extension' not in copy


def test_guidance_uses_saved_albums_even_before_their_songs_arrive(store):
    store.collection.add_saved_album({'browse': 'MPREready', 'title': 'My record', 'artist': 'Artist'})
    state = nodes(account_growth.view(store, 1000, mixes_ready=False, albums_ready=False, radio={}))
    assert 'saved playlists and albums' in state['albums']['how_to']
    assert 'Choose 3 artists' not in state['foundation']['how_to']


def test_home_inputs_reuse_journey_and_invalidate_on_supplied_or_committed_changes(file_backed_store, monkeypatch):
    store = file_backed_store
    catalog(store, processed=10, total=10)
    collections = {field: [] for field in ('discovery_tracks', 'discovery_albums',
        'rediscover', 'rediscover_albums', 'rediscover_tracks')}
    original = account_growth._view
    calls = []

    def observe(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(account_growth, '_view', observe)

    def read(**overrides):
        supplied = {'mixes_ready': False, 'albums_ready': False, 'radio': {},
                    'collections': collections, **overrides}
        with store.read_view(tracked=True) as reader:
            return account_growth.view(reader, 1000, **supplied)

    first = read()
    assert read() == first
    assert len(calls) == 1
    assert not nodes(first)['listening']['usable']
    assert nodes(read(mixes_ready=True))['listening']['usable']
    assert len(calls) == 2
    discovered = {**collections, 'discovery_tracks': [{}] * 10}
    assert nodes(read(collections=discovered))['discover']['usable']
    assert len(calls) == 3
    assert nodes(read(radio={'state': {'active': True}}))['radio']['usable']
    assert len(calls) == 4
    # A committed library change must invalidate a result with identical supplied
    # inputs, and a caller cannot mutate the detached cached journey.
    first['health']['total'] = -1
    store.tracks.upsert_track('new-owned', 'New song', 'Artist', None, 200)
    assert read()['health']['total'] == 11
    assert len(calls) == 5


def test_health_counts_distinct_scoped_recordings_from_empty_through_new_additions(store):
    def health():
        value = account_growth.view(store, 1000, mixes_ready=False, albums_ready=False, radio={})['health']
        return tuple(value[name] for name in ('total', 'processed', 'percent', 'label'))

    assert health() == (0, 0, 0, 'Waiting for music')
    owned = store.tracks.upsert_track('owned', 'Owned', 'Artist', None, 200)
    complete_entries(store, [owned], 10)
    owned_key = store.tracks.identity_key_for_video('owned')
    store.discovery.upsert_discovered_track(
        owned_key, 'owned', 'Owned', 'Artist', None, None, None, None, 'radio:review', 20)
    discovered_key = store.discovery.upsert_discovered_track(
        'discovered|artist', 'discovered', 'Discovered', 'Artist',
        None, None, None, None, 'radio:review', 20)
    # Cached catalog entries outside both memberships must not enter the count.
    cached = store.tracks.observe_track_entry('cached', 'Cached', 'Artist', now=20)
    complete_entries(store, [cached], 21)
    assert store.conn.execute('SELECT COUNT(*) FROM library_memberships').fetchone()[0] == 1
    assert store.conn.execute('SELECT COUNT(*) FROM discovery_memberships').fetchone()[0] == 2
    assert health() == (2, 1, 50, 'Getting to know your music')

    complete_discovered(store, [discovered_key], 30)
    # Processing coverage can be complete while listening menus still prepare.
    assert health() == (2, 2, 100, 'Ready to keep growing')
    store.tracks.upsert_track('new', 'New song', 'Artist', None, 200)
    assert health() == (3, 2, 66, 'Getting to know your music')


def test_health_excludes_available_provider_refresh_jobs_until_they_finish(store):
    def health():
        value = account_growth.view(store, 1000, mixes_ready=False, albums_ready=False, radio={})['health']
        return tuple(value[name] for name in ('total', 'processed', 'percent'))

    state = store.enrichment_state
    state.reconcile_provider_configuration({'deezer': {'fingerprint': 'old', 'status': 'available'}})
    entry = store.tracks.upsert_track('review', 'Review song', 'Artist', None, 200)
    key = store.tracks.identity_key_for_video('review')
    recording = store.tracks.catalog_subject(entry)['recording_id']
    state.record_enrichment_attempt(key, 'deezer', {}, now=10)
    complete_entries(store, [entry], 11)
    assert health() == (1, 1, 100)

    state.reconcile_provider_configuration({'deezer': {'fingerprint': 'new', 'status': 'available'}})
    assert state.enrichment_provider_jobs(recording) == ['deezer']
    assert health() == (1, 0, 0)
    state.reconcile_provider_configuration({'deezer': {'fingerprint': 'new', 'status': 'disabled'}})
    assert store.conn.execute(
        "SELECT 1 FROM enrichment_provider_jobs WHERE recording_id=? AND provider='deezer'",
        (recording,)).fetchone()
    assert health() == (1, 1, 100)

    state.reconcile_provider_configuration({'deezer': {'fingerprint': 'new', 'status': 'available'}})
    assert health() == (1, 0, 0)
    state.record_enrichment_attempt(key, 'deezer', {}, retry=True, now=20, error='temporary failure')
    assert health() == (1, 0, 0)
    state.record_enrichment_attempt(key, 'deezer', {}, now=60)
    assert state.enrichment_provider_jobs(recording) == []
    assert state.enrichment_provider_state(recording)['deezer']['status'] == 'no_match'
    assert health() == (1, 1, 100)


def test_readiness_requires_usable_outputs_and_defers_home_choice(store):
    catalog(store)
    state = account_growth.view(store, 1000, mixes_ready=False, albums_ready=False, radio={})
    assert state['health']['processed'] == 9
    assert nodes(state)['radio']['state'] == 'untouched'
    assert not state['home_choice_available']
    from tests.test_growth_journey import early_journey
    early_journey(store)
    complete_entries(store, [row[0] for row in store.conn.execute('SELECT id FROM tracks')], 1)
    complete_discovered(store, [row[0] for row in store.conn.execute(
        'SELECT identity_key FROM discovered_tracks')], 1)
    state = account_growth.view(store, 1000, mixes_ready=True, albums_ready=True, radio=ready_radio())
    assert nodes(state)['radio']['state'] == 'ready', state['health']
    assert nodes(state)['listening']['state'] == 'ready'
    assert nodes(state)['albums']['state'] == 'ready'
    assert state['home_choice_available']
    store.settings.set_setting('home_format', 'albums')
    assert not account_growth.view(store, 1000, mixes_ready=True, albums_ready=True,
                                   radio=ready_radio())['home_choice_available']


def test_soundcheck_uses_arriving_catalog_and_actual_play_without_waiting_for_full_sync(store):
    def first():
        return nodes(account_growth.view(store, 1000, mixes_ready=False, albums_ready=False, radio={}))['first']

    assert first()['state'] == 'untouched'
    store.tracks.observe_track_entry('unverified', 'Unverified', 'Artist', now=1)
    assert first()['state'] == 'untouched'
    seed_catalog_track(store, 'first|artist', 'First song', 'Artist', video_id='first')
    assert first()['state'] == 'ready'
    identity = store.accounts.upsert_account('main', 'credential', None, True)
    store.history.import_play_events(identity, [('first|artist', 'first', 1)])
    assert first()['state'] == 'ready'
    store.history.record_play_event(identity, 'first|artist', 'first', 1)
    assert first()['state'] == 'explored'
    assert store.settings.get_setting('last_sync_at') is None
    assert store.settings.get_setting('home_format') is None


def test_incidental_live_play_without_a_playable_first_set_does_not_light_soundcheck(store):
    from types import SimpleNamespace
    from yt_playlist.library.live_plays import handle_play_event

    store.accounts.upsert_account('main', 'credential', None, True)
    assert handle_play_event(SimpleNamespace(store=store), {
        'type': 'play', 'title': 'Incidental', 'artist': 'Artist',
        'videoId': 'incidental', 'brandId': '', 'likeStatus': 'INDIFFERENT',
    }, 1.)
    assert store.conn.execute('SELECT 1 FROM playable_library_tracks').fetchone() is None
    state = account_growth.view(store, 1000, mixes_ready=False, albums_ready=False, radio={})
    assert nodes(state)['first']['state'] == 'untouched'


def test_deliberate_use_is_distinct_and_continuing_growth_does_not_reset_it(store):
    catalog(store)
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {
        'episodes': {'mixes': 1, 'albums': 0, 'radio': 1}}}, 1000)
    state = account_growth.view(store, 1000, mixes_ready=True, albums_ready=True, radio=ready_radio())
    assert nodes(state)['radio']['state'] == 'explored'
    assert nodes(state)['listening']['state'] == 'explored'
    assert nodes(state)['albums']['state'] == 'ready'
    seed_catalog_track(store, 'new|artist', 'New song', 'Artist', video_id='new')
    state = account_growth.view(store, 1001, mixes_ready=True, albums_ready=True, radio=ready_radio())
    assert state['health']['processed'] == 9 and state['health']['total'] == 11
    assert nodes(state)['radio']['state'] == 'explored'


def test_albums_precede_mixes_even_when_mixes_finish_preparing_first(store):
    catalog(store)
    from tests.test_growth_journey import early_journey
    early_journey(store)
    state = account_growth.view(store, 1000, mixes_ready=True, albums_ready=False, radio={})
    assert not nodes(state)['listening']['ready']
    assert not nodes(state)['albums']['ready']
    assert nodes(state)['listening']['href'] == ''
    assert nodes(state)['first']['href'] == ''
    state = account_growth.view(store, 1000, mixes_ready=False, albums_ready=True, radio={})
    assert not nodes(state)['listening']['ready']
    assert nodes(state)['albums']['ready']
    assert nodes(state)['albums']['href'] == '/#album-choices'


def test_keys_and_empty_imports_are_not_completed_bonuses(store, monkeypatch):
    catalog(store)
    monkeypatch.setattr(account_growth.lastfm, 'available', lambda _store: True)
    monkeypatch.setattr(account_growth.deepseek, 'settings', lambda _store, _now:
                        {'configured': True, 'enabled': True, 'status': None})
    state = account_growth.view(store, 1000, mixes_ready=True, albums_ready=True, radio=ready_radio())
    assert nodes(state)['lastfm']['state'] == 'preparing'
    assert nodes(state)['lastfm']['connected']
    assert not nodes(state)['lastfm']['ready']
    assert nodes(state)['deepseek']['state'] == 'preparing'
    assert nodes(state)['takeout']['state'] == 'untouched'
    assert all(nodes(state)[name]['bonus'] for name in ('lastfm', 'deepseek', 'takeout'))


def test_lastfm_connection_tracks_configuration_without_waiting_for_coverage(store, monkeypatch):
    monkeypatch.setattr(account_growth.lastfm, 'api_key',
                        lambda source: source.settings.get_setting('lastfm_api_key') or None)

    def music_details():
        return nodes(account_growth.view(store, 1000, mixes_ready=False, albums_ready=False, radio={}))['lastfm']

    assert not music_details()['connected']
    store.settings.set_setting('lastfm_api_key', 'controlled-key')
    assert music_details()['connected']
    assert not music_details()['ready']
    store.settings.set_setting('enrichment_order', '[{"name":"lastfm","enabled":false}]')
    assert not music_details()['connected']
    store.settings.set_setting('enrichment_order', '[]')
    assert music_details()['connected']
    store.settings.set_setting('lastfm_api_key', '')
    assert not music_details()['connected']


def test_lastfm_bonus_requires_current_refresh_and_real_coverage(store, monkeypatch):
    catalog(store)
    monkeypatch.setattr(account_growth.enrichment, 'provider_configuration', lambda *_args:
                        {'lastfm': {'status': 'available', 'fingerprint': 'current'}})
    with store.conn:
        store.conn.execute("INSERT INTO enrichment_provider_config VALUES ('lastfm','current','available')")
    for index in range(9):
        store.enrichment_state.record_enrichment_attempt(f'song {index}|artist', 'lastfm', {}, now=1000)
    # Completing the enclosing pass advances its scheduling timestamp after
    # provider answers were recorded. It doesn't erase successful coverage.
    with store.conn:
        store.conn.execute('UPDATE recordings SET last_enriched_at=1001 WHERE first_enriched_at IS NOT NULL')
    state = account_growth.view(store, 1000, mixes_ready=True, albums_ready=True, radio=ready_radio())
    assert nodes(state)['lastfm']['state'] == 'explored'
    from yt_playlist.core import provider_frontiers
    with store.conn:
        provider_frontiers.start(store.conn, 'lastfm', 0, 'current')
    assert nodes(account_growth.view(store, 1000))['lastfm']['state'] == 'preparing'
    with store.conn:
        provider_frontiers.clear(store.conn, 'lastfm')
    with store.conn:
        store.conn.execute("UPDATE enrichment_provider_config SET fingerprint='previous' WHERE provider='lastfm'")
    assert nodes(account_growth.view(store, 1000))['lastfm']['state'] == 'preparing'


def test_home_choice_endpoint_requires_readiness_and_preserves_navigation_distinction(store, monkeypatch):
    client = client_for(store, Catalog())
    store.settings.set_setting('home_format', 'albums')
    assert client.post('/home/growth/format', data={'format': 'radio'}).status_code == 409
    assert store.settings.get_setting('home_format') == 'albums'
    monkeypatch.setattr(account_growth, 'view', lambda *_args, **_kwargs:
                        {'ready_formats': {'mixes': True, 'albums': True, 'radio': True},
                         'ready_areas': {'radio': True}})
    assert client.post('/home/growth/format', data={'format': 'mixes'}).status_code == 204
    assert store.settings.get_setting('home_format') == 'mixes'
    client.post('/home/format', data={'panel': 'radio'})
    assert store.settings.get_setting('home_format') == 'mixes'
    assert client.post('/home/growth/format', data={'format': 'vibes'}).status_code == 422


def test_vibe_use_requires_a_choice_and_confirmed_start_of_the_actual_vibe_offer():
    offers = {'menu': {'id': 'menu', 'format': 'mixes', 'episode_id': 'episode', 'at': 1,
                        'vibe': 'relax', 'options': [{'id': 'relax-set', 'descriptor_id': 1}]}}
    choice = {'id': 'choice', 'kind': 'choice', 'opportunity_id': 'menu', 'option_id': 'relax-set', 'at': 2}
    start = {'id': 'start', 'kind': 'start', 'opportunity_id': 'menu', 'option_id': 'relax-set', 'at': 3}
    assert home_preferences.compile_usage(offers, [start], 10, 30)['vibe_episodes'] == 0
    assert home_preferences.compile_usage(offers, [choice], 10, 30)['vibe_episodes'] == 0
    assert home_preferences.compile_usage(offers, [choice, start], 10, 30)['vibe_episodes'] == 1
    # Old compact choices with no frozen purpose cannot establish historical use.
    del offers['menu']['vibe']
    assert home_preferences.compile_usage(offers, [choice, start], 10, 30)['vibe_episodes'] == 0
    offers['menu']['options'][0]['recipe'] = {'vibe': 'relax'}
    assert home_preferences.compile_usage(offers, [choice, start], 10, 30)['vibe_episodes'] == 1


def test_vibe_readiness_combines_current_sound_evidence_with_pool_readiness(store, monkeypatch):
    from yt_playlist.core import vibe_versions
    from tests.musical_support import prepare_profiles
    from tests.test_tv_stream import video
    from yt_playlist.rec import tv

    # Account growth coordinates the current SQL evidence with the lower
    # readiness owner's output. Actual four-card pool selection is covered by
    # test_growth_readiness.test_vibe_gate_checks_real_menus_for_every_preset_and_current_exclusions.
    available, requests = False, []

    def pools(reader, now):
        requests.append(now)
        return available

    monkeypatch.setattr(account_growth.growth_readiness, 'vibes', pools)
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {'episodes': {'radio': 1}}}, 1000)
    store.rec_proposals.put_proposals(tv.SURFACE, {'schema': tv.SCHEMA, 'key': 'ready',
        'at': 1000, 'attempted': [], 'items': [video(i) for i in range(tv.QUEUE_SIZE)]}, 1000)

    def growth(now=1000, *, mixes=True):
        return account_growth.view(store, now, mixes_ready=mixes, albums_ready=False, radio={})

    assert not growth()['vibes_ready']
    assert not requests
    seed_catalog_track(store, 'measured|artist', 'Measured', 'Artist', video_id='measured',
                       genre='Jazz', energy=.1, mood_relaxed=.9, mood_happy=.8)
    prepare_profiles(store)
    assert not growth()['vibes_ready']
    assert requests == [1000]
    available = True
    state = growth(1060)
    assert requests == [1000, 1060]
    assert state['health']['measured'] > 0
    assert state['vibes_ready'] and nodes(state)['vibes']['state'] == 'ready'
    assert not growth(1060, mixes=False)['vibes_ready']
    assert requests == [1000, 1060]
    with store.conn:
        column = vibe_versions.column(store.conn, 'recording_vibes')
        store.conn.execute(f"UPDATE recording_vibes SET {column}=?",
                           (vibe_versions.stored(store.conn, 'outdated'),))
    state = growth(1060)
    assert state['health']['measured'] == 0
    assert not state['vibes_ready']
    assert nodes(state)['vibes']['state'] == 'preparing'
    assert requests == [1000, 1060]
