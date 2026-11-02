"""Preparation can run ahead; onboarding access follows the same serial path."""
import pytest

from tests.conftest import seed_catalog_track
from tests.onboarding_support import publish_personal_mix
from tests.test_growth_readiness import fresh_reserve, prepared_taste
from yt_playlist.rec import account_growth, home_prepared, home_vibe


def early_journey(store):
    seed_catalog_track(store, 'first|artist', 'First song', 'Artist', video_id='first')
    tracks = fresh_reserve(store)
    prepared_taste(store)
    context = store.rec_proposals.get_proposals(home_prepared.SURFACE)['choice_context']
    store.rec_proposals.put_proposals('album_prepared', {
        'built_at': 1000, 'context': context, 'items': [
            {'browse_id': f'MPRE{i}', 'title': f'Record {i}', 'artist': f'Artist {i}',
             'classification': 'discovery', 'score': .8} for i in range(2)]}, 1000)
    store.rec_proposals.put_proposals(home_prepared.DISCOVERY_SURFACE,
        {'discovery_albums': [], 'discovery_artists': [], 'discovery_tracks': tracks[:10]}, 1000)
    return tracks


def stops(store):
    return {node['id']: node for node in account_growth.view(store, 1000)['nodes']}


@pytest.mark.parametrize('kind', ['unresolved', 'unavailable', 'overlong', 'playable'])
def test_first_sounds_agrees_with_the_actual_owned_playback_selector(store, kind):
    from yt_playlist.rec import onboarding

    video = None if kind == 'unresolved' else 'first'
    seed_catalog_track(store, 'first|artist', 'First song', 'Artist', video_id=video,
                       duration_s=9000 if kind == 'overlong' else None)
    if kind == 'unavailable':
        with store.conn:
            store.conn.execute("UPDATE youtube_items SET available=0 WHERE video_id='first'")
    assert store.rec_query.tracks_total() == 1
    playable = kind == 'playable'
    first = stops(store)['first']
    assert first['usable'] is playable
    assert first['ready'] is playable
    assert first['unlocked'] is playable
    assert bool(onboarding.library_sample(store)) is playable
    # Import counts still describe the actual library, including long recordings.
    assert onboarding.arrival_status(store, 1000)['songs'] == int(kind in ('playable', 'overlong'))


def test_later_output_cannot_skip_foundation_or_albums_and_only_frontier_prepares(store):
    publish_personal_mix(store)
    initial = stops(store)
    assert initial['listening']['usable']
    assert not initial['listening']['ready'] and not initial['listening']['unlocked']
    assert initial['foundation']['state'] == 'preparing'
    assert all(initial[key]['state'] == 'untouched' for key in account_growth.MAIN_PATH[2:])
    assert initial['radio']['parent'] == 'listening'
    assert initial['albums']['parent'] == 'foundation'
    assert not initial['listening']['href']


def test_early_pool_then_small_discover_interlude_precede_four_playable_mixes(store):
    early_journey(store)
    before = stops(store)
    assert all(before[key]['ready'] for key in ('first', 'foundation', 'albums', 'discover'))
    assert before['listening']['state'] == 'preparing'
    assert before['radio']['state'] == 'untouched'
    tracks = publish_personal_mix(store)
    store.rec_model.record_dislike(tracks[0]['key'], until=None, now=1000)
    assert not stops(store)['listening']['ready']
    store.rec_model.clear_dislike(tracks[0]['key'])
    assert stops(store)['listening']['ready']


def test_optional_revisit_and_provider_work_do_not_block_the_main_formats(store):
    early_journey(store)
    publish_personal_mix(store)
    current = stops(store)
    assert current['listening']['ready']
    assert current['rediscover']['optional'] and not current['rediscover']['ready']
    assert current['radio']['state'] == 'preparing'
    assert not any(current[key]['ready'] for key in ('takeout', 'lastfm', 'deepseek'))


def test_routes_and_remembered_vibe_cannot_bypass_the_serial_gate(store):
    from tests.test_onboarding_routes import _client

    _, client = _client(store)
    publish_personal_mix(store)
    store.settings.set_setting('home_format', 'mixes')
    store.settings.set_setting(home_vibe.SETTING, 'relax')
    assert home_vibe.current(store, 1000) == ''
    assert client.get('/home/cards').status_code == 409
    assert client.get('/home/album-choices').status_code == 409
    assert client.get('/home/discovery').status_code == 409
    assert client.get('/home/revisit').status_code == 409
    assert client.get('/home/into-recently').status_code == 409
    assert client.post('/home/tv/play').status_code == 409
    assert client.post('/home/vibe', data={'vibe': 'energize'}).status_code == 409
    assert store.settings.get_setting(home_vibe.SETTING) == 'relax'
    assert client.post('/home/format', data={'panel': 'directions'}).status_code == 409
    assert client.post('/home/format', data={'format': 'radio'}).status_code == 409
    assert client.get('/home/onboard/status').json()['setup']['complete'] is False
    assert 'id="onboarding-feed"' in client.get('/').text


def test_confirmed_existing_format_use_restores_it_without_a_forced_tour(store):
    publish_personal_mix(store)
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {
        'episodes': {'mixes': 1}}}, 1000)
    state = account_growth.view(store, 1000)
    current = {node['id']: node for node in state['nodes']}
    assert current['listening']['ready'] and current['listening']['state'] == 'explored'
    assert not current['albums']['usable']
    assert current['albums']['unlocked']
    assert state['ready_formats']['mixes']
    assert not state['home_choice_available']


@pytest.mark.parametrize('used', ['vibes', 'tv'])
def test_previously_used_destination_is_preparing_when_its_output_is_missing(store, used):
    if used == 'vibes':
        store.rec_proposals.put_proposals('listening_choices', {
            'home_format': {'vibe_episodes': 1}}, 1000)
    else:
        store.settings.set_setting('tv_draw', '1')
    current = stops(store)
    assert current[used]['used']
    assert current[used]['unlocked']
    assert not current[used]['ready'] and not current[used]['usable']
    assert current[used]['state'] == 'preparing'
    assert current[used]['href'] == ('/#directions' if used == 'vibes' else '/#tv')


@pytest.mark.parametrize('used,areas', [
    ('mixes', ('album-choices', 'discover', 'directions')),
    ('radio', ('album-choices', 'discover', 'directions', 'radio')),
    ('vibes', ('album-choices', 'discover', 'directions', 'radio', 'tv', 'vibes')),
])
def test_earned_access_survives_missing_output_and_read_only_maintenance(store, used, areas):
    if used == 'vibes':
        evidence = {'vibe_episodes': 1}
    else:
        evidence = {'episodes': {used: 1}}
    store.rec_proposals.put_proposals('listening_choices', {'home_format': evidence}, 1000)
    store.settings.set_setting(home_vibe.SETTING, 'relax')
    before = store.conn.total_changes
    store.conn.execute('PRAGMA query_only=ON')
    store.activity_paused = True
    try:
        state = account_growth.view(store, 1000)
        assert not any(state['ready_formats'].values())
        assert not state['vibes_ready']
        for area in areas:
            assert account_growth.has_access(state, area), area
        assert not account_growth.has_access(state, 'recent')
        assert not account_growth.has_access(state, 'rediscover')
        assert home_vibe.current(store, 1000) == ('relax' if used == 'vibes' else '')
        assert account_growth.radio_access(store, 1000, radio={}) is (used != 'mixes')
        assert store.conn.total_changes == before
    finally:
        store.activity_paused = False
        store.conn.execute('PRAGMA query_only=OFF')


def test_remembered_preference_alone_does_not_restore_earned_access(store):
    store.settings.set_setting('home_format', 'radio')
    store.settings.set_setting('home_last_panel', 'radio')
    store.settings.set_setting(home_vibe.SETTING, 'relax')
    state = account_growth.view(store, 1000)
    assert not any(account_growth.has_access(state, area)
                   for area in (*state['ready_areas'], 'vibes'))
    assert not account_growth.radio_access(store, 1000, radio={})
    assert home_vibe.current(store, 1000) == ''


def test_saved_vibe_with_confirmed_use_does_not_reassemble_growth_menus(store, monkeypatch):
    from tests.home_fixture_support import returning_listener

    returning_listener(store, 'vibes', now=1)
    store.settings.set_setting(home_vibe.SETTING, 'relax')

    def no_menu_rebuild(*_args, **_kwargs):
        raise AssertionError('Earned Vibe access must not depend on current prepared menus')

    with monkeypatch.context() as patch:
        patch.setattr(account_growth.growth_readiness, 'vibes', no_menu_rebuild)
        patch.setattr(account_growth, 'view', no_menu_rebuild)
        assert home_vibe.current(store, 1000) == 'relax'
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {}}, 1001)
    assert home_vibe.current(store, 1002) == ''


def test_previously_used_mix_remains_home_and_fragment_destination_without_ready_output(store):
    from tests.test_onboarding_routes import _client

    _, client = _client(store)
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {
        'episodes': {'mixes': 1}}}, 1000)
    store.settings.set_setting('home_format', 'mixes')
    html = client.get('/').text
    assert "|| 'directions'" in html
    response = client.get('/home/cards')
    assert response.status_code == 200
    assert client.post('/home/format', data={'panel': 'directions'}).status_code == 204
    assert store.settings.get_setting('home_last_panel') == 'directions'
    # Access does not turn incomplete menus into three ready format choices.
    assert client.post('/home/format', data={'format': 'radio'}).status_code == 409


def test_radio_checkpoint_probe_serializes_with_concurrent_home_reads(store, monkeypatch):
    from yt_playlist.core import radio_storage
    from yt_playlist.web.radio_persistence import RadioPersistence

    persistence = RadioPersistence(store.settings)
    available = radio_storage.available
    def checked_probe(connection):
        assert store._lock.held_by_current_thread
        return available(connection)
    monkeypatch.setattr(radio_storage, 'available', checked_probe)
    assert persistence.load() is None


def test_radio_starter_exception_requires_the_current_offer_and_incomplete_setup(store, monkeypatch):
    from tests.test_onboarding_routes import _client
    from yt_playlist.rec import onboarding

    _, client = _client(store)
    radio = client.app.state.ctx.radio
    monkeypatch.setattr(radio, 'bootstrap', lambda: {
        'state': {'active': False}, 'seeds': [{'key': 'offered|artist', 'video_id': 'offered'}], 'seedMenu': 'current'})
    starts = []
    def start(*args, **kwargs):
        starts.append(args)
        return {'ok': True}
    monkeypatch.setattr(radio, 'start', start)
    offered = {'video_id': 'offered', 'menu_id': 'current'}
    assert client.post('/radio/start', json=offered).status_code == 409
    store.settings.set_setting('onboard_listening_format', 'radio')
    for invalid in ({**offered, 'menu_id': 'stale'}, {**offered, 'video_id': 'unrelated'}):
        assert client.post('/radio/start', json=invalid).status_code == 409
    assert not starts
    assert client.post('/radio/start', json=offered).json()['ok']
    assert len(starts) == 1
    store.settings.set_setting(onboarding.SETUP_COMPLETE, '1')
    assert client.post('/radio/start', json=offered).status_code == 409


def test_radio_energy_cannot_bypass_vibes_but_can_be_reset(store, monkeypatch):
    from tests.test_onboarding_routes import _client

    _, client = _client(store)
    changes = []
    def tune(values):
        changes.append(values)
        return {'ok': True}
    monkeypatch.setattr(client.app.state.ctx.radio, 'tune', tune)
    assert client.post('/radio/tune', json={'energy': .9}).status_code == 409
    assert not changes
    assert client.post('/radio/tune', json={'energy': .5}).json()['ok']
    assert changes == [{'energy': .5}]
