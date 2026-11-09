"""TV inherits prepared musical choices and resolves only bounded visual editions."""
import json
from contextlib import closing
from tests.enrichment_support import complete_entries, complete_discovered
from tests.derived_rating_support import seed_dislike, seed_like

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.onboarding_support import publish_personal_mix
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, initialize_consumer, native_sql_calls,
    mock_snapshot_coordinator, reuse_reporting_reader)
from yt_playlist.core.store import Store
from yt_playlist.rec import account_growth, tv
from yt_playlist.web.app import create_app


@pytest.fixture(scope='module')
def tv_schema():
    with bounded_consumer_schema((('house', 'House'),)) as schema:
        yield schema


@pytest.fixture(scope='module')
def tv_launch_schema():
    # This HTTP workflow owns ordinary playlist writes. It does not use
    # guarded background snapshots; TV preparation keeps the guarded input.
    with bounded_consumer_schema((('house', 'House'),), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def tv_home_store(tmp_path, tv_launch_schema, monkeypatch, native_sql_calls):
    """Real Home SQL/files below independent snapshot conflict detection."""
    with _file_consumer_store(tmp_path, tv_launch_schema, snapshot_guards=False) as database:
        with mock_snapshot_coordinator(database, monkeypatch):
            yield database


@pytest.fixture
def store(request, tmp_path, monkeypatch):
    if 'tv_journey_inputs' in request.fixturenames:
        yield request.getfixturevalue('store')
        return
    database = Store(tmp_path / 'tv.db')
    with closing(database.conn):
        launch = 'tv_launch_web' in request.fixturenames
        schema = request.getfixturevalue('tv_launch_schema' if launch else 'tv_schema')
        # SQL/files and closed-file persistence stay real. These fixtures do
        # not exercise persistence across power loss.
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, schema, snapshot_guards=not launch)
        # These cases own TV serving and publication, independently of the
        # periodic WAL checkpoint policy covered by test_publication_checkpoint.
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with reuse_reporting_reader(database, monkeypatch):
            yield database


@pytest.fixture(autouse=True)
def tv_http_routes(request, monkeypatch):
    """Build the real TV, playlist and bridge routes used by these checks."""
    from yt_playlist.providers import genres
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home, home_tv, playlists

    monkeypatch.setattr(genres, 'configure', lambda *args, **kwargs: None)
    routes = (app_update, audio_preview, home_tv, playlists) if 'tv_launch_web' in request.fixturenames else (
        app_update, audio_preview, home, playlists)
    if 'tv_launch_web' in request.fixturenames:
        from fastapi import APIRouter
        from yt_playlist.web.routes import audio_vibes

        monkeypatch.setattr(audio_vibes, 'build', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx) for module in routes])
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)


@pytest.fixture
def completed_video_catalog(store, monkeypatch):
    """Supply completed video ingestion below the real TV launch/playlist writer.

    The launch still creates its remote playlist, records confirmation, publishes
    SQL membership and recipe, and handles playback through the actual routes.
    Track metadata ingestion is covered by the track controller's own tests.
    """
    from yt_playlist.util.matching import identity_key

    def install(items):
        rows = [{**item, 'key': identity_key(item['title'], item['artist']),
                 'duration_s': item['duration']} for item in items]
        seed_publication_subjects(store, rows)
        with store.conn:
            # Prepared visual editions are unowned until the generated TV
            # playlist publishes them; they must retain generated quarantine.
            store.conn.executemany('DELETE FROM library_memberships WHERE entry_id='
                '(SELECT id FROM catalog_entries WHERE video_id=?)', [(row['video_id'],) for row in rows])
        entries = {row['video_id']: store.conn.execute(
            'SELECT id FROM catalog_entries WHERE video_id=?', (row['video_id'],)).fetchone()[0]
            for row in rows}

        def ingested(video, title='', artist='', album=None, duration_s=None, **kwargs):
            assert video in entries, video
            return entries[video]

        monkeypatch.setattr(store.tracks, 'upsert_track', ingested)

    return install


def row(index=0, title='Prepared 0 (Official Music Video)', **values):
    return {'videoId': f'visual{index:05}', 'title': title, 'artists': [{'name': 'Artist'}],
            'duration_seconds': 200, **values}


def prepare_tv_journey(store, *, mixes=4):
    from tests.test_growth_journey import early_journey

    early_journey(store)
    return prepare_tv_formats(store, mixes=mixes)


@pytest.fixture(scope='module')
def early_journey_template(_pristine_current_schema):
    from tests.test_growth_journey import early_journey

    with prepared_template(_pristine_current_schema, early_journey) as template:
        yield template


@pytest.fixture
def tv_journey_inputs(store, early_journey_template):
    early_journey_template.restore(store)


@pytest.fixture
def prepared_tv_predecessors(monkeypatch):
    # These TV HTTP cases consume prerequisite readiness. The real preparation
    # path remains in test_home_keeps_tv_entry_with_prepared_queue_after_the_early_journey
    # and test_growth_journey.test_early_pool_then_small_discover_interlude_precede_four_playable_mixes.
    from yt_playlist.rec import growth_readiness

    def prepared():
        for name in ('foundation', 'albums', 'discover'):
            monkeypatch.setattr(growth_readiness, name, lambda *args, **kwargs: True)
    return prepared


def prepare_tv_formats(store, *, mixes=4):
    """Publish this case's mixes/radio after its independent early journey."""
    tracks = publish_personal_mix(store, count=mixes, tracks_per_card=1)
    store.settings.set_setting('radio_seed_offer', json.dumps({
        'menu_id': 'prepared-radio', 'tracks': tracks[:4]}))
    complete_entries(store, [row[0] for row in store.conn.execute('SELECT id FROM tracks')], 1000)
    complete_discovered(store, [row[0] for row in store.conn.execute(
        'SELECT identity_key FROM discovered_tracks')], 1000)
    return tracks


def test_home_keeps_tv_entry_with_prepared_queue_after_the_early_journey(store, tv_journey_inputs):
    from tests.test_tv_stream import video
    iid = store.accounts.upsert_account('TV account', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('home_format', 'mixes')
    prepare_tv_formats(store)
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'independent-tv', 'at': 1, 'attempted': [],
        'items': [video(i) for i in range(tv.QUEUE_SIZE)]}, 1)
    state = account_growth.view(store, 1000)
    assert state['ready_formats']['mixes'] and state['ready_formats']['radio']
    assert state['ready_areas']['tv'] and len(tv.queue(store, 1000)) == tv.QUEUE_SIZE
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        html = client.get('/').text
        assert 'data-dash="tv"' in html and '>TV<span' in html
        assert 'class="tv-home tv-session"' in html
        assert 'Adjust TV parameters' in html
        assert client.get('/home/tv').json()['ready'] is True


@pytest.mark.parametrize('failed', [False, True])
def test_empty_and_failed_tv_status_remains_readable_when_the_journey_reaches_tv(
        store, failed, tv_journey_inputs):
    iid = store.accounts.upsert_account('TV account', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('home_format', 'albums')
    prepare_tv_formats(store)
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'independent-tv', 'at': 1, 'attempted': [],
        'items': [], 'state': 'no_sources'}, 1)
    store.rec_proposals.put_proposals('tv_preparation', {'failed': failed}, 1)
    state = account_growth.view(store, 1000)
    assert state['ready_formats']['radio'] and not state['ready_areas']['tv']
    assert next(node for node in state['nodes'] if node['id'] == 'tv')['state'] == 'preparing'
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        html = client.get('/').text
        assert 'data-dash="tv"' in html and 'class="tv-home tv-session"' in html
        status = client.get('/home/tv').json()
        assert status['ready'] is False
        assert ('reconnecting' if failed else 'liked songs') in status['message']


@pytest.mark.parametrize('missing', ['fourth_mix', 'twelfth_video', 'predecessor'])
def test_tv_preparation_cannot_bypass_mix_queue_or_predecessor_requirements(
        store, missing, tv_journey_inputs):
    from tests.test_onboarding_routes import _client
    from tests.test_tv_stream import video

    _, client = _client(store)
    prepare_tv_formats(store, mixes=3 if missing == 'fourth_mix' else 4)
    if missing == 'predecessor':
        store.rec_proposals.put_proposals('album_prepared', {'built_at': 1000, 'items': []}, 1000)
    count = tv.QUEUE_SIZE - (missing == 'twelfth_video')
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'not-yet-unlocked', 'at': 1000, 'attempted': [],
        'items': [video(i) for i in range(count)]}, 1000)
    state = account_growth.view(store, 1000)
    assert bool(tv.queue(store, 1000))  # Available output alone does not unlock access.
    assert not state['ready_areas']['tv']
    assert client.post('/home/tv/play').status_code == 409


class Videos(FakeClient):
    def __init__(self, results=None, details=None):
        super().__init__()
        self.results = results or [row()]
        self.details = details or {}
        self.searches, self.lookups = [], []

    def search(self, query, filter='songs'):
        self.searches.append((query, filter))
        self._catalog.update({t['videoId']: t for t in self.results})
        return self.results

    def get_song(self, video):
        self.lookups.append(video)
        return self.details.get(video, {})


@pytest.mark.parametrize('title,kind', [
    ('Song (Visualizer)', 'Visualizer'), ('Song visualiser', 'Visualizer'),
    ('Song (Live session)', 'Live session'), ('Song Official Music Video', 'Music video'),
    ('Song (Official Audio)', None), ('Song Lyric Video', None), ('Song', None)])
def test_visual_evidence(title, kind):
    assert tv.video_kind({'title': title}) == kind


def test_official_video_metadata_is_evidence_but_cannot_override_audio_title():
    assert tv.video_kind({'title': 'Song', 'videoType': 'MUSIC_VIDEO_TYPE_OMV'}) == 'Music video'
    assert tv.video_kind({'title': 'Song Official Audio', 'videoType': 'MUSIC_VIDEO_TYPE_OMV'}) is None


def test_resolver_rejects_wrong_recording_artist_stills_unknown_overlong_and_blocked(store):
    seeds = publish_personal_mix(store)
    rows = [row(1, artists=[{'name': 'Unrelated'}]), row(2, title='Unrelated Official Video'),
            row(3, title='Prepared 0 Official Audio'), row(4, duration_seconds=None),
            row(5, duration_seconds=901), row(6), row(7)]
    client = Videos(rows, {'visual00006': {'playabilityStatus': {'status': 'UNPLAYABLE'}}})
    # At most two detailed lookups per seed: narrow to the invalid edition and a valid fallback.
    assert not tv.resolve(client, seeds[:1], 15)
    client.results = rows[-2:]
    picks = tv.resolve(client, seeds[:1], 15)
    assert [t['video_id'] for t in picks] == ['visual00007']
    assert picks[0]['source_key'] == seeds[0]['key']
    assert all(kind == 'videos' for _, kind in client.searches)


def test_resolver_caps_remote_work_and_deduplicates_videos(store):
    seeds = publish_personal_mix(store)
    client = Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(4)])
    picks = tv.resolve(client, seeds * 20, 15)
    assert len(picks) == tv.MAX_SEEDS
    assert len({t['video_id'] for t in picks}) == tv.MAX_SEEDS
    assert len(client.searches) == tv.MAX_SEEDS * len(tv.SEARCHES)
    assert len(client.lookups) == tv.MAX_SEEDS


def test_preferred_searches_rank_footage_ahead_of_stills_and_visualizers(store):
    seeds = publish_personal_mix(store)
    class Editions(Videos):
        def search(self, query, filter='songs'):
            self.searches.append((query, filter))
            if query.endswith('session'):
                return [row(3, title='Prepared 0 (Studio Session)')]
            return [row(0, title='Prepared 0 (Visualizer)'),
                    row(1, title='Prepared 0 (Official Music Video)', videoType='MUSIC_VIDEO_TYPE_ATV'),
                    row(2, title='Prepared 0 (Static Image)')]
    client = Editions()
    picked = tv.resolve(client, seeds[:1], 15)
    assert [item['video_id'] for item in picked] == ['visual00003']
    assert [query for query, _ in client.searches] == [f'Artist Prepared 0 {edition}' for edition in tv.SEARCHES]


@pytest.mark.parametrize('details', [
    {'musicVideoType': 'MUSIC_VIDEO_TYPE_ATV'}, {'author': 'Artist - Topic'},
    {'shortDescription': 'Provided to YouTube by a record company'},
    {'shortDescription': 'An audio only upload with a static image'},
])
def test_detailed_audio_evidence_overrules_misleading_visual_title(store, details):
    seeds = publish_personal_mix(store)
    client = Videos([row()], {'visual00000': {'videoDetails': details}})
    assert not tv.resolve(client, seeds[:1], 15)


def test_pool_uses_likes_unowned_artist_tracks_and_discovery_without_exposure(store, monkeypatch):
    seeds = publish_personal_mix(store)
    for seed in seeds:
        seed_like(store, seed['key'], 1000)
    fresh = [{'key': f'fresh-{i}', 'title': f'New {i}', 'artist': artist, 'video_id': f'new-{i}'}
             for i, artist in enumerate(['Artist', 'Discovery A', 'Artist', 'Discovery B'])]
    monkeypatch.setattr(tv.fresh_pool, 'pick', lambda *args, **kwargs: fresh)
    def forbidden(*args, **kwargs):
        raise AssertionError('TV must not render cards or log mix exposure')
    monkeypatch.setattr(tv.home_prepared, 'cards', forbidden)
    picked = tv.seed_tracks(store, 1000)
    assert [t['group'] for t in picked[:6]] == list(tv.GROUPS) * 2
    # The shared fixture publishes four separate mix ranks. TV interleaves those
    # ranks instead of concatenating every song from the first mix first.
    assert [t['key'] for t in picked if t['group'] == 'liked'] == [
        seeds[index]['key'] for index in (0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15)]
    assert all(t['artist'] == 'Artist' for t in picked if t['group'] == 'your_artists')
    assert not set(t['key'] for t in picked if t['group'] != 'liked') & store.rec_query.library_keys()


def prepare_pool(store, client, now=1, *, seeds=None):
    if seeds is None:
        seeds = publish_personal_mix(store, now=now)
    for seed in seeds:
        seed_like(store, seed['key'], now)
    while tv.prepare(store, client, now):
        pass
    return tv.cached(store)


def test_preparation_publishes_in_bounded_slices_and_recovers_after_failure(store, monkeypatch):
    seeds = [{'key': f'key-{i}', 'title': f'Prepared {i}', 'artist': 'Artist', 'video_id': str(i), 'group': tv.GROUPS[i % 3]}
             for i in range(15)]
    monkeypatch.setattr(tv, 'seed_tracks', lambda *_: seeds)
    client = Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(15)])
    assert tv.prepare(store, client, 1000)
    first = tv.cached(store)
    assert len(first['attempted']) == tv.MAX_SEEDS and len(client.searches) == tv.MAX_SEEDS * len(tv.SEARCHES)
    def failed(*args, **kwargs): raise RuntimeError('offline')
    search = client.search
    client.search = failed
    with pytest.raises(RuntimeError, match='offline'): tv.prepare(store, client, 1001)
    assert tv.cached(store) == first
    assert store.rec_proposals.get_proposals('tv_preparation')['failed']
    client.search = search
    while tv.prepare(store, client, 1002):
        pass
    assert len(tv.cached(store)['attempted']) == 15
    assert not store.rec_proposals.get_proposals('tv_preparation')['failed']
    count = len(client.searches)
    assert not tv.prepare(store, client, 1004)
    assert len(client.searches) == count


def test_empty_home_refresh_keeps_available_prepared_videos_but_respects_feedback(store, monkeypatch):
    client = Videos()
    previous = prepare_pool(store, client)
    monkeypatch.setattr(tv, 'seed_tracks', lambda *_: [])
    assert not tv.prepare(store, client, 2)
    assert tv.cached(store)['items'] == previous['items']
    assert tv.queue(store, 2)
    for item in previous['items']:
        seed_dislike(store, item['source_key'], until=None, now=3)
    assert not tv.prepare(store, client, 3)
    assert not tv.queue(store, 3)


def test_search_cannot_publish_after_source_removal(store, monkeypatch):
    client = Videos()
    previous = prepare_pool(store, client)
    monkeypatch.setattr(tv, 'MAX_AGE', 1)
    assert tv.seed_tracks(store, 2)
    search = client.search
    def remove_source(*args, **kwargs):
        store.rec_proposals.put_proposals('home_tuned', {}, 2)
        return search(*args, **kwargs)
    client.search = remove_source
    assert tv.prepare(store, client, 2)
    assert tv.cached(store) == previous


def test_expired_pool_remains_playable_during_unsuccessful_refresh(store):
    client = Videos()
    previous = prepare_pool(store, client)
    assert tv.queue(store, 1)
    client.results = []
    now = tv.MAX_AGE + 2
    tv.prepare(store, client, now)
    assert tv.cached(store)['items'] == previous['items']
    assert tv.queue(store, now)
    for item in previous['items']:
        seed_dislike(store, item['source_key'], until=None, now=now + 1)
    assert not tv.queue(store, now + 1)


@pytest.mark.parametrize('refresh', ['empty-seeds', 'empty-results', 'failed-search'])
def test_lost_pool_recovers_saved_tv_output_offline_without_restoring_excluded_sources(store, monkeypatch, refresh):
    from yt_playlist.core import tv_output_retention

    client = Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(16)])
    previous = prepare_pool(store, client, now=1000)
    assert len(previous['items']) >= 4
    store.playlist_recipes.set_recipe('saved-tv', {
        'lane': 'tv', 'media': 'video', 'videos': previous['items']}, 1000)
    # Duplicate saved sessions must not manufacture additional queue capacity.
    store.playlist_recipes.set_recipe('older-tv', {
        'lane': 'tv', 'media': 'video', 'videos': previous['items']}, 999)
    with store.conn.atomic():
        tv_output_retention.clear_disposable(store.conn)
    assert tv.cached(store) is None
    if refresh == 'empty-seeds':
        monkeypatch.setattr(tv, 'seed_tracks', lambda *_: [])
    else:
        assert tv.seed_tracks(store, 1001)
    client.results = []
    if refresh == 'failed-search':
        def offline(*args, **kwargs):
            raise ConnectionError('offline search')
        monkeypatch.setattr(client, 'search', offline)
        with pytest.raises(ConnectionError, match='offline search'):
            tv.prepare(store, client, 1001)
        assert store.rec_proposals.get_proposals('tv_preparation')['failed']
    else:
        tv.prepare(store, client, 1001)
    assert tv.cached(store) is not None
    assert tv.cached(store)['items'] == previous['items']
    if refresh == 'failed-search':
        assert tv.cached(store)['attempted'] == []
    # This fixture has one artist, so the ordinary artist cap still applies.
    assert len(tv.queue(store, 1001)) == 3
    for item in previous['items']:
        seed_dislike(store, item['source_key'], until=None, now=1002)
    with store.conn.atomic():
        tv_output_retention.clear_disposable(store.conn)
    if refresh == 'failed-search':
        with pytest.raises(ConnectionError, match='offline search'):
            tv.prepare(store, client, 1002)
    else:
        tv.prepare(store, client, 1002)
    assert not (tv.cached(store) or {}).get('items')
    assert not tv.queue(store, 1002)


@pytest.mark.parametrize('change', ['feedback', 'home'])
def test_failed_search_cannot_publish_saved_tv_after_inputs_change(store, monkeypatch, change):
    from yt_playlist.core import tv_output_retention

    client = Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(4)])
    previous = prepare_pool(store, client, now=1000)
    store.playlist_recipes.set_recipe('saved-tv', {
        'lane': 'tv', 'media': 'video', 'videos': previous['items']}, 1000)
    with store.conn.atomic():
        tv_output_retention.clear_disposable(store.conn)
    assert tv.seed_tracks(store, 1001)
    def offline(*args, **kwargs):
        if change == 'feedback':
            seed_dislike(store, previous['items'][0]['source_key'], until=None, now=1001)
        else:
            store.rec_proposals.put_proposals('home_tuned', {}, 1001)
        raise ConnectionError('changed while offline')
    monkeypatch.setattr(client, 'search', offline)
    with pytest.raises(ConnectionError, match='changed while offline'):
        tv.prepare(store, client, 1001)
    assert tv.cached(store) is None
    assert not tv.queue(store, 1001)
    assert store.rec_proposals.get_proposals('tv_preparation')['failed']


@pytest.mark.parametrize('error', [ConnectionError, tv.WorkBudgetExceeded, tv.WorkClosing])
def test_saved_tv_failure_recovery_preserves_eligibility_and_cancellation(store, error):
    from tests.test_tv_stream import video

    seeds = publish_personal_mix(store, now=1000)
    for seed in seeds:
        seed_like(store, seed['key'], 1000)
    items = [{**video(i), 'source_key': seeds[i]['key'], 'source_artist': seeds[i]['artist']}
             for i in range(6)]
    items[1]['source_key'] = 'removed-source'
    items[2]['artist'] = 'Muted singer'
    items[3]['duration'] = 901
    store.rec_model.record_feedback('for_you', 'artist:Muted singer', 'mute', now=1000)
    seed_dislike(store, items[4]['source_key'], until=None, now=1000)
    store.tv.publish_visual(items[5]['video_id'], {
        'version': tv.tv_visual.VERSION, 'verdict': 'static', 'at': 1000}, 1000)
    recipe = {'lane': 'tv', 'media': 'video', 'videos': items}
    store.playlist_recipes.set_recipe('saved-tv', recipe, 1000)
    assert tv.seed_tracks(store, 1001)
    class Offline:
        def search(self, *args, **kwargs):
            raise error('controlled failure')
    with pytest.raises(error, match='controlled failure'):
        tv.prepare(store, Offline(), 1001)
    if error is not ConnectionError:
        assert tv.cached(store) is None
        assert store.tv_preparation.state() is None
    else:
        assert {t['video_id'] for t in tv.cached(store)['items']} == {
            items[i]['video_id'] for i in (0, 3)}
        assert [t['video_id'] for t in tv.queue(store, 1001)] == [items[0]['video_id']]
        store.settings.set_setting(tv.SETTING, 'unlimited')
        assert {t['video_id'] for t in tv.queue(store, 1001)} == {items[i]['video_id'] for i in (0, 3)}
        assert store.tv_preparation.state()['failed']
    assert store.playlist_recipes.get_recipe('saved-tv') == recipe


def test_refill_continues_across_menu_changes_and_keeps_prepared_videos(store, monkeypatch):
    seeds = [{'key': f'key-{i}', 'title': f'Prepared {i}', 'artist': 'Artist',
              'group': 'liked'} for i in range(7)]
    candidates = [seeds[:6]]
    monkeypatch.setattr(tv, 'seed_tracks', lambda *_: candidates[0])
    client = Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(7)])
    assert tv.prepare(store, client, 1000)
    first = tv.cached(store)
    assert len(first['items']) == tv.MAX_SEEDS
    # Reverse the ranking, add a candidate and remove a previously searched one.
    # None of those changes should restart every search or blank the serving pool.
    candidates[0] = list(reversed(seeds[1:]))
    client.results = [row(99, title='Unrelated Official Video')]
    assert tv.prepare(store, client, 1001)
    second = tv.cached(store)
    assert len(second['attempted']) == 5
    assert {t['source_key'] for t in second['items']} == {'key-1', 'key-2'}
    assert not tv.prepare(store, client, 1002)
    assert len(tv.cached(store)['attempted']) == 6
    assert all('Prepared 1 ' not in query and 'Prepared 2 ' not in query
               for query, _ in client.searches[tv.MAX_SEEDS * len(tv.SEARCHES):])


@pytest.fixture
def tv_launch_web(store, monkeypatch, prepared_tv_predecessors, completed_video_catalog):
    from tests.test_tv_stream import video
    from yt_playlist.rec import onboarding, onboarding_outputs

    iid = store.accounts.upsert_account('main', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '1')
    prepared_tv_predecessors()
    # TV consumes completed Mix tapes readiness. The real prerequisite journey
    # is tested above; this workflow keeps TV eligibility and writes real.
    monkeypatch.setattr(onboarding, 'prepared_mixes_ready', lambda *args, **kwargs: True)
    monkeypatch.setattr(onboarding_outputs, 'prepared_mixes_ready', lambda *args, **kwargs: True)
    seeds = [{'key': f'prepared {index}|artist', 'title': f'Prepared {index}',
              'artist': 'Artist', 'video_id': f'prepared-{index}', 'genre': 'house'}
             for index in range(4)]
    seed_publication_subjects(store, seeds)
    store.settings.set_setting('radio_seed_offer', json.dumps({
        'menu_id': 'prepared-radio', 'tracks': seeds}))
    complete_entries(store, list(store.tracks.track_ids_for_videos(
        [seed['video_id'] for seed in seeds]).values()), 1000)
    # HTTP serving consumes completed resolution. Resolver and bounded worker
    # publication remain real in test_preparation_publishes_in_bounded_slices_and_recovers_after_failure.
    client = Videos([])
    items = [{**video(i), 'artist': 'Artist', 'source_artist': 'Artist',
              'source_key': seed['key']} for i, seed in enumerate(seeds)]
    pool = {'schema': tv.SCHEMA, 'key': 'completed-tv-http', 'at': 1,
            'attempted': [seed['key'] for seed in seeds], 'items': items,
            'offset': 0, 'state': 'exhausted', 'retry_at': 1 + tv.SEARCH_RETRY_S}
    # Four same-artist videos and three other prepared artists exercise the
    # three-per-artist cap while still filling a real TV queue.
    extras = [{**video(10 + i), 'artist': f'Other artist {i // 3}',
               'source_artist': f'Other artist {i // 3}'} for i in range(9)]
    store.rec_proposals.put_proposals(tv.SURFACE, {**pool, 'items': [*pool['items'], *extras]}, 1)
    client._catalog.update({item['video_id']: {
        'videoId': item['video_id'], 'title': item['title'], 'artists': [{'name': item['artist']}],
        'duration_seconds': item['duration']} for item in [*items, *extras]})
    completed_video_catalog([*pool['items'], *extras])
    assert account_growth.view(store, 1)['ready_areas']['tv']
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1)
    with TestClient(app, base_url='http://127.0.0.1') as web:
        yield web, client


def test_precalculated_serving_parameters_generation_and_sidebar_launch(store, tv_launch_web, caplog):
    from collections import Counter

    web, client = tv_launch_web
    assert web.get('/home/tv').json() == {'ready': True, 'max_minutes': 15}
    assert not client.created
    assert not web.post('/home/tv', data={'max_minutes': '2'}).json()['ready']
    assert web.post('/home/tv', data={'max_minutes': '15'}).json()['ready']
    assert web.post('/home/tv', data={'max_minutes': '0'}).status_code == 400
    assert web.post('/home/tv', data={'max_minutes': 'nan'}).status_code == 400
    assert not client.created
    response = web.post('/home/tv/play')
    assert response.status_code == 200, response.text
    result = response.json()
    playlist = store.playlists.get_playlist(result['playlist_id'])
    items = result['items']
    assert len(items) == tv.QUEUE_SIZE
    assert Counter(item['source_artist'] for item in items) == {
        'Artist': 3, 'Other artist 0': 3, 'Other artist 1': 3, 'Other artist 2': 3}
    assert store.playlists.is_quarantined_playlist(playlist.playlist_scope_id)
    assert store.playlists.get_playlist_groups()[playlist.playlist_scope_id] == 'Generated'
    assert store.playlist_recipes.get_recipe(playlist.playlist_scope_id)['media'] == 'video'
    assert web.get(f'/home/tv/playlist/{playlist.id}').json()['items'] == items
    assert web.get('/home/tv').json()['ready']  # saving does not exhaust the pool
    assert web.get('/home/tv/playlist/999999').status_code == 404
    searches = len(client.searches)
    assert '"is_video": true' in web.get('/playlists').text
    assert 'Play on TV' in web.get(f'/playlist/{playlist.id}').text
    assert web.post('/play', json={'url': f'https://music.youtube.com/playlist?list={playlist.provider_playlist_id}'}).json() == {
        'ok': True, 'tv_url': f'/?tv_playlist={playlist.id}#tv'}
    assert len(client.searches) == searches
    event_url = f'/home/tv/playlist/{playlist.id}/event'
    video = items[0]['video_id']
    assert web.post(event_url, data={'kind': 'player_error', 'video_id': video,
                                    'state': '5', 'position': '3.15'}).json() == {'ok': True}
    assert f'TV player error: playlist={playlist.id} video={video} code=5 position=3.150' in caplog.text
    assert store.history.play_events_since(0) == []
    assert all(at is None for tracks in store.charts.get_playlist_track_recency(include_generated=True).values()
               for at in tracks)
    assert web.post(event_url, data={'kind': 'player_error', 'video_id': video,
                                    'state': 'bogus'}).status_code == 400
    assert web.post(event_url, data={'kind': 'player_error', 'video_id': 'not-in-playlist',
                                    'state': '5'}).status_code == 400
    assert web.post(event_url, data={'kind': 'play', 'video_id': video}).json() == {'ok': True}
    assert web.post(event_url, data={'kind': 'play', 'video_id': video}).json() == {'ok': True}
    plays = store.history.play_events_since(0)
    assert len(plays) == 1 and plays[0]['playlist_ytm_id'] == playlist.provider_playlist_id
    assert 1 in store.charts.get_playlist_track_recency(include_generated=True)[playlist.id]
    assert all(at is None for at in store.charts.get_playlist_track_recency()[playlist.id])
    assert web.post(event_url, data={'kind': 'ended', 'video_id': video, 'position': '200'}).status_code == 200
    assert web.post(event_url, data={'kind': 'play', 'video_id': 'not-in-playlist'}).status_code == 400
    assert web.post(event_url, data={'kind': 'play', 'video_id': video, 'position': 'nan'}).status_code == 400
    assert len(client.searches) == searches
    for artist in {item['source_artist'] for item in items}:
        store.rec_model.record_feedback('for_you', 'artist:' + artist, 'mute', now=1)
    assert not web.get('/home/tv').json()['ready']
    pending = web.post('/home/tv/play')
    # Earned access survives a temporarily empty eligible pool.
    assert pending.status_code == 202
    assert pending.json()['preparing']
    assert len(client.created) == 1
    assert 'current TV choices' in web.get('/home/tv').json()['message']


def test_missing_pool_requests_background_preparation_and_keeps_playback_locked(store, monkeypatch):
    app = create_app(store, lambda: {}, now_fn=lambda: 1)
    requests = []
    monkeypatch.setattr(app.state.ctx.rec_worker, 'trigger_tv_refresh', lambda: requests.append('tv'))
    with TestClient(app, base_url='http://127.0.0.1') as web:
        assert not web.get('/home/tv').json()['ready']
        response = web.post('/home/tv/play')
        assert response.status_code == 409
        assert requests == ['tv']  # The readable status requests preparation; locked playback does not.
        assert tv.draw_number(store) == 0


def test_new_sessions_use_unseen_pool_videos_before_recent_sessions_and_keep_lane_balance(store):
    items = [{'video_id': f'visual{i:05}', 'title': f'Video {i}', 'artist': f'Artist {i}',
              'source_artist': f'Artist {i}', 'source_key': f'source-{i}', 'duration': 200,
              'kind': 'Music video', 'group': tv.GROUPS[i % 3]} for i in range(36)]
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'pool', 'at': 1, 'attempted': [], 'items': items}, 1)
    sessions = []
    for _ in range(3):
        picked = tv.queue(store, 1)
        assert len(picked) == tv.QUEUE_SIZE
        assert [t['group'] for t in picked] == list(tv.GROUPS) * 4
        sessions.append({t['video_id'] for t in picked})
        tv.record_draw(store, picked)
    assert len(set.union(*sessions)) == 36
    assert not sessions[0] & sessions[1] and not sessions[1] & sessions[2]
    # Once the whole pool has been used, older sessions get another turn.
    assert {t['video_id'] for t in tv.queue(store, 1)} == sessions[0]


def test_small_pools_still_change_the_first_video_on_each_new_session(store):
    prepare_pool(store, Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(4)]))
    for _ in range(5):
        previous = tv.queue(store, 1)
        tv.record_draw(store, previous)
        current = tv.queue(store, 1)
        assert current[0]['video_id'] != previous[0]['video_id']
        assert set(t['video_id'] for t in current) - set(t['video_id'] for t in previous)


def test_refill_never_queues_the_current_video_when_every_alternative_failed(store):
    prepare_pool(store, Videos([row(i, title=f'Prepared {i} Visualizer') for i in range(4)]))
    items = tv.cached(store)['items']
    current = items[-1]['video_id']
    assert tv.queue(store, 1, current=current, exclude=[t['video_id'] for t in items[:-1]]) == []


def test_handoff_waits_for_extension_confirmation_and_never_assumes_success(store, monkeypatch):
    app = create_app(store, lambda: {}, now_fn=lambda: 1)
    frames = []
    async def command(frame, **kwargs):
        frames.append(frame)
        return 200, json.dumps({'ok': True, 'paused': True})
    monkeypatch.setattr(app.state.ctx.bridge, 'request_control', command)
    with TestClient(app, base_url='http://127.0.0.1') as web:
        assert web.post('/home/tv/takeover').json() == {'ok': True}
        assert frames == [{'type': 'pause-for-tv'}]
        async def unconfirmed(*args, **kwargs): return 200, '{"ok": true}'
        monkeypatch.setattr(app.state.ctx.bridge, 'request_control', unconfirmed)
        assert web.post('/home/tv/takeover').status_code == 409


@pytest.fixture
def tv_length_routes(monkeypatch, tv_http_routes, native_sql_calls):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home_tv, playlists

    # The length check launches TV; it does not render unrelated Home controls.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx) for module in
                                                       (app_update, audio_preview, home_tv, playlists)])


def test_tv_length_cap_can_admit_long_video_without_changing_music_limit(
        store, prepared_tv_predecessors, completed_video_catalog, tv_length_routes):
    from yt_playlist.library.playlist_generated import create_generated_playlist
    from tests.test_tv_stream import video
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    prepare_tv_formats(store)
    prepared_tv_predecessors()
    # Resolver matching has separate owners; length admission consumes a full
    # completed queue containing only long visual editions.
    videos = [video(i, duration=2100) for i in range(tv.QUEUE_SIZE)]
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'long-editions', 'at': 1,
        'attempted': [], 'items': videos}, 1)
    client = FakeClient(catalog={item['video_id']: {
        'videoId': item['video_id'], 'title': item['title'], 'artists': [{'name': item['artist']}],
        'duration_seconds': item['duration']} for item in videos})
    assert not tv.queue(store, 1)
    store.settings.set_setting(tv.SETTING, '40')
    items = tv.queue(store, 1)
    assert len(items) == tv.QUEUE_SIZE and all(item['duration'] == 2100 for item in items)
    completed_video_catalog(items)
    with pytest.raises(ValueError, match='maximum track length'):
        create_generated_playlist(store, 'Music', items, client, 1, iid)
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1)
    with TestClient(app, base_url='http://127.0.0.1') as web:
        result = web.post('/home/tv/play')
        assert result.status_code == 200, result.text
        assert result.json()['items'][0]['duration'] == 2100


def test_tv_tab_waits_for_real_mixes_and_follows_long_play(tv_home_store, prepared_tv_predecessors):
    from html.parser import HTMLParser
    from tests.conftest import seed_catalog_track

    class Tabs(HTMLParser):
        def __init__(self, markup):
            super().__init__()
            self.labels = []
            self.in_tab = False
            self.feed(markup)

        def handle_starttag(self, tag, attrs):
            if tag == 'button' and dict(attrs).get('role') == 'tab':
                self.in_tab = True
                self.labels.append('')

        def handle_data(self, data):
            if self.in_tab:
                self.labels[-1] += data

        def handle_endtag(self, tag):
            if tag == 'button':
                self.in_tab = False

    store = tv_home_store
    iid = store.accounts.upsert_account('main', 'cred', None, True)
    # An imported empty account has no preparation to await. Model a real
    # library whose first offers are still being prepared.
    seed_catalog_track(store, 'waiting|artist', 'Waiting', 'Artist', video_id='waiting')
    store.settings.set_setting('last_sync_at', '1')
    with TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1),
                    base_url='http://127.0.0.1') as web:
        assert 'TV' not in [label.strip() for label in Tabs(web.get('/').text).labels]
        from tests.test_tv_stream import video
        prepared_tv_predecessors()
        prepare_tv_formats(store)
        store.rec_proposals.put_proposals(tv.SURFACE, {
            'schema': tv.SCHEMA, 'key': 'ready-tv-tab', 'at': 1, 'attempted': [],
            'items': [video(i) for i in range(tv.QUEUE_SIZE)]}, 1)
        assert account_growth.view(store, 1)['ready_areas']['tv']
        markup = web.get('/').text
        labels = [label.strip() for label in Tabs(markup).labels]
        assert labels.index('Long play') + 1 == labels.index('TV')
        assert labels.index('TV') + 1 == labels.index('Discover')


def test_cache_expiry_parameters_and_bad_payloads_are_safe(store):
    # Invalid current writes fail at the typed owner without changing state.
    # A structurally valid older resolver version can persist but cannot serve.
    for payload, rejected in [([], True), ({'schema': tv.SCHEMA, 'key': 'k', 'at': 'bad'}, True),
            ({'schema': 2, 'key': 'old-resolver', 'at': 1000, 'items': [], 'attempted': []}, False),
            ({'schema': tv.SCHEMA, 'key': 'k', 'at': 1000, 'items': None}, True),
            ({'schema': tv.SCHEMA, 'at': float('inf'), 'items': []}, True)]:
        if rejected:
            before = list(store.conn.iterdump())
            with pytest.raises(ValueError):
                store.rec_proposals.put_proposals(tv.SURFACE, payload, 1000)
            assert list(store.conn.iterdump()) == before
        else:
            store.rec_proposals.put_proposals(tv.SURFACE, payload, 1000)
        assert tv.cached(store) is None
