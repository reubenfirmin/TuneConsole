"""Disposable rankings can finish while the retained listening evidence advances."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import numpy as np
import pytest

from tests import model_support
from tests.derived_rating_support import seed_dislike
from tests.genre_catalog_support import bound_catalog
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core import listening
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import (candidate_refresh, discover, home_prepared,
                             embedding_vectors, measurement, mode_surfaces, surfaces, taste_modes)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/publication cases never construct the web application."""


@pytest.fixture
def ranker_library(tmp_path, playback_schema, record_property, monkeypatch):
    """Completed small model inputs; ranking and concurrent publication stay real."""
    started = time.monotonic()
    bound_catalog(monkeypatch, entries=(('rock', 'Rock'), ('jazz', 'Jazz'), ('techno', 'Techno')))
    # Maintenance and the full policy/input fingerprint have independent
    # owners. Keep genuine policy identities, file staging and publication.
    monkeypatch.setattr(measurement, 'maintain_storage', lambda *args: True)
    monkeypatch.setattr(measurement, 'serving_policy', completed_policy)
    monkeypatch.setattr(candidate_refresh, 'prepared_settings',
                        lambda reader, now: {'rec_candidate_preferences': 'captured-ranker-inputs'})
    store = Store(tmp_path / 'rankers.db')
    with closing(store.conn):
        # This check owns visibility and concurrency, not power-loss recovery.
        store.conn.execute('PRAGMA synchronous=OFF')
        # WAL checkpoint maintenance has independent owners; committed WAL
        # visibility to the pinned reader and concurrent writer remains real.
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        initialize_consumer(store, playback_schema)
        record_property('ranker_schema_setup_seconds', time.monotonic() - started)
        _seed_ranker_inputs(store)
        record_property('ranker_input_setup_seconds', time.monotonic() - started)
        yield store


def _seed_ranker_inputs(store):
    tracks = [{'key': f'song {index:03}|artist {index}', 'title': f'Song {index:03}',
               'artist': f'Artist {index}', 'video_id': f'v{index}',
               'album': f'Album {index}', 'year': str(1990 + index), 'duration_s': 200,
               'genre': ('Techno', 'Jazz', 'Rock')[index % 3]} for index in range(9)]
    fresh = {'key': 'new song|new artist', 'title': 'New song', 'artist': 'New artist',
             'video_id': 'new', 'genre': 'Jazz', 'year': '2001', 'duration_s': 200}
    seed_publication_subjects(store, [*tracks, fresh], discoveries=[fresh['key']], now=1800)
    with atomic_write(store):
        account = store.accounts.upsert_account('Listener', 'fixture', None, True)
        entries = store.tracks.track_ids_for_videos([track['video_id'] for track in tracks])
        for index, members in enumerate((tracks[:6], tracks[3:])):
            playlist = store.playlists.upsert_playlist(account, f'p{index}', f'Playlist {index}', 6, '', 1)
            store.playlists.set_playlist_tracks(playlist, [entries[track['video_id']] for track in members])
        store.settings.set_setting('rec_dim', '4')
    store.history.import_play_events(account, [(tracks[0]['key'], 'v0', 1000)])
    # Numerical fitting at the 79/80 admission boundary has its own owners in
    # test_taste_modes_discover. This case starts from an admitted mode and owns
    # actual scoring, bucketing, disk publication and simultaneous listening.
    assert model_support.build_track_model(store) == 9
    assert model_support.build_content_model(store) == 9
    snapshot = store.rec_model.content_generation()
    keys, matrix, _index = embedding_vectors.load_content_vectors(store, snapshot=snapshot)
    centroid = matrix.mean(axis=0)
    centroid /= np.linalg.norm(centroid)
    store.modes.replace_modes([{'mode_id': 1, 'label': 'Prepared region',
        'families': [['Jazz', 3], ['Rock', 3], ['Techno', 3]], 'centroid': centroid,
        'size': len(keys), 'rep_keys': keys,
        'space': embedding_vectors.content_space_id(store, snapshot=snapshot)}], [], 2000)
    assert taste_modes.live_modes(store)


def playback_library(store):
    # These cases coordinate captured rankings with listening writes. Three
    # owned recordings and one discovery retain every mutation below. The
    # independent concurrent-ranker case uses real scoring and file publication.
    account = store.accounts.upsert_account('Listener', 'fixture', None, True)
    tracks = [{'key': f'song {index:03}|artist {index}', 'title': f'Song {index:03}',
               'artist': f'Artist {index}', 'video_id': f'v{index}', 'genre': genre}
              for index, genre in ((0, 'Techno'), (10, 'Jazz'), (11, 'Rock'))]
    fresh = {'key': 'new song 0|new artist 0', 'video_id': 'new0', 'title': 'New song 0',
             'artist': 'New artist 0', 'genre': 'Techno', 'year': '1990', 'duration_s': 200}
    seed_publication_subjects(store, [*tracks, fresh], discoveries=[fresh['key']], now=1800)
    store.history.import_play_events(account, [('song 000|artist 0', 'v0', 1000)], source='takeout')
    other = store.accounts.upsert_account('Other listener', 'other fixture', None, False)
    store.history.import_play_events(other, [('song 011|artist 11', 'v11', 500)], source='takeout')
    store.rec_model.replace_rec_content_vectors([
        ('song 000|artist 0', np.array([1., 0.], dtype=np.float32).tobytes())])
    bundle, fresh = completed_rankings(store)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(measurement, 'serving_policy', completed_policy)
        publish_bundles(store, bundle, now=2000, fresh=fresh)
    assert store.recommendations.page('home', 'all', 'temporal')
    assert store.recommendations.page('home', 'all', 'wheelhouse')
    assert store.recommendations.page('fresh', 'all', 'fresh')


def completed_rankings(reader, now=None, policies=None):
    """Small lower-layer result; real catalog, policy and publication owners."""
    keys = ['song 000|artist 0', 'song 010|artist 10']
    tracks = reader.rec_query.tracks_by_keys(keys)
    fresh = [{'key': 'new song 0|new artist 0', 'video_id': 'new0',
              'title': 'New song 0', 'artist': 'New artist 0', 'genre': 'Techno'}]
    lanes = {'wheelhouse': [{'key': keys[0], **tracks[keys[0]]}],
             'temporal': [{'key': keys[1], **tracks[keys[1]], 'year': '1990'}],
             'fresh': fresh}
    if policies is not None:
        lanes = {lane: policies.stamp(items, lane) for lane, items in lanes.items()}
    return {'all': lanes, '_meta': {'full_pools': True}}, lanes['fresh']


def completed_policy(reader, now, ranker='lane'):
    # Fingerprinting every model and listening signal belongs to measurement's
    # own tests. Keep real policy identities, files and publication here.
    return 'fixture', {'ranker': ranker, 'source': 'playback-publication-fixture'}


@pytest.fixture(scope='module')
def playback_schema():
    # These cases own consumer coordination, not schema admission. Install the
    # shipped definition once with its FK checks and genuine generated revision
    # guards. Every consumer gets a private SQL/file namespace and real guards.
    with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz'), ('techno', 'Techno'))) as schema:
        yield schema


@pytest.fixture
def library(tmp_path, playback_schema, monkeypatch):
    # Policy retirement has its own real maintenance owners in
    # test_policy_storage; it is below this coordination check.
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', completed_rankings)
    monkeypatch.setattr(measurement, 'serving_policy', completed_policy)
    monkeypatch.setattr(measurement, 'maintain_storage', lambda *args, **kwargs: True)
    # Captured preference fingerprints have independent coverage in
    # test_candidate_refresh. These checks own publication and its schema fence.
    monkeypatch.setattr(candidate_refresh, 'prepared_settings',
                        lambda reader, now: {'rec_candidate_preferences': 'captured-fixture-inputs'})
    # Four native subjects are cheaper to seed than capturing and relocating
    # every unrelated file owner in a prepared-template copy.
    store = Store(tmp_path / 'playback.db')
    with closing(store.conn):
        # These visibility/fencing cases do not exercise power-loss recovery
        # or background WAL checkpoint policy.
        store.conn.execute('PRAGMA synchronous=OFF')
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        initialize_consumer(store, playback_schema)
        playback_library(store)
        yield store


def evidence(store):
    return tuple(tuple(row) for row in store.conn.execute(
        'SELECT account_id,identity_key,day FROM listening_days '
        'ORDER BY account_id,identity_key,day'))


def playback_change(store, change):
    account = store.accounts.get_accounts()[0].id
    event = store.conn.execute('SELECT id FROM play_events ORDER BY id LIMIT 1').fetchone()[0]
    key = store.tracks.identity_key_for_video('v10')
    if change == 'live':
        store.history.record_play_event(account, key, 'v10', 150000)
    elif change == 'overlapping-source':
        existing = store.tracks.identity_key_for_video('v0')
        assert store.history.import_play_events(account, [(existing, 'v0', 1000)], source='json') == 0
    elif change == 'remove-source':
        store.history.remove_listening_source('takeout', account)
    elif change == 'confirm-calendar':
        with store.conn:
            listening.calendar(store.conn, account, 'America/New_York')
    elif change == 'reconcile-day':
        store.history.record_play_event(account, key, 'v10', 150000)
        row = store.conn.execute(
            "SELECT day FROM listening_observations WHERE source='foreign-date'").fetchone()
        assert row[0] == '1970-01-02'
    else:
        with store.conn:
            if change == 'calendar-update':
                store.conn.execute("UPDATE play_event_calendar SET day='1970-01-02' WHERE event_id=?", (event,))
            elif change == 'calendar-delete':
                store.conn.execute('DELETE FROM play_event_calendar WHERE event_id=?', (event,))
            elif change == 'source-update':
                store.conn.execute("UPDATE play_event_sources SET source='renamed' WHERE event_id=?", (event,))
            else:
                raise AssertionError(change)


CHANGES = ['live', 'overlapping-source', 'remove-source', 'confirm-calendar',
           'reconcile-day', 'calendar-update', 'calendar-delete', 'source-update']


def prepare_change(store, change):
    if change == 'reconcile-day':
        account = store.accounts.get_accounts()[0].id
        with store.conn:
            listening.calendar(store.conn, account, 'UTC')
            listening.observe_day(store.conn, account, store.tracks.identity_key_for_video('v10'),
                '1970-01-02', 'foreign-date', 'America/New_York', 900)
        assert store.conn.execute(
            "SELECT day FROM listening_observations WHERE source='foreign-date'").fetchone()[0] is None


@pytest.mark.parametrize('change', CHANGES)
def test_bundle_publication_keeps_one_coherent_generation_during_playback(
        library, monkeypatch, change, record_property):
    prepare_change(library, change)
    # The unchanged account is an input, not a transition under test. Its real
    # retained listening evidence is present before the captured transition.
    other = next(account.id for account in library.accounts.get_accounts()
                 if account.label == 'Other listener')
    other_evidence = tuple(row for row in evidence(library) if row[0] == other)
    old_heads = library.recommendations.heads()
    calculate = mode_surfaces._calculate_bundles
    captured = []

    def calculate_while_playing(reader, now, policies):
        before = evidence(reader)
        result = calculate(reader, now, policies)
        playback_change(library, change)
        # Calculation retains the same pinned evidence, including after writes.
        assert evidence(reader) == before
        captured.append(result[0])
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate_while_playing)
    # Detailed ranker/SQL profiling belongs to the independent real-ranker
    # control, rather than wrapping every cursor in these publication cases.
    started, cpu = time.monotonic(), time.thread_time()
    result = mode_surfaces.prepare_bundles(library, 2000)
    record_property('wall_seconds', time.monotonic() - started)
    record_property('thread_cpu_seconds', time.thread_time() - cpu)
    assert result is captured[0]
    assert result['all']['wheelhouse'] and result['all']['fresh']
    assert result['all']['temporal']
    assert library.recommendations.heads() != old_heads
    heads = [library.recommendations.head(surface) for surface in ('home', 'fresh')]
    assert heads[0]['id'] == heads[1]['id']
    assert [row['key'] for row in library.recommendations.page('home', 'all', 'temporal')] == [
        row['key'] for row in result['all']['temporal']]
    assert not library.conn.in_transaction
    assert not library.conn.execute('PRAGMA foreign_key_check').fetchall()
    assert tuple(row for row in evidence(library) if row[0] == other) == other_evidence


@pytest.mark.parametrize('change', ['live', 'overlapping-source', 'remove-source', 'confirm-calendar'])
def test_discovery_publishes_usable_tiles_during_listening_updates(library, monkeypatch, change):
    library.discovery.upsert_discovered_album('new-album', 'New artist', 'New album',
                                             '2026', None, 1000, genre='Jazz')
    library.discovery.upsert_discovered_artist('New artist', .9, 'similar', 'Jazz', None,
                                              1000, genre='Jazz')
    pick = discover.pick_discovered_albums

    def pick_while_playing(reader, *args, **kwargs):
        result = pick(reader, *args, **kwargs)
        # Read the real learning projection, rather than an invented dependency.
        before = evidence(reader)
        playback_change(library, change)
        assert evidence(reader) == before
        return result

    monkeypatch.setattr(discover, 'pick_discovered_albums', pick_while_playing)
    result = home_prepared.prepare_discovery(library, 2000)
    assert result is not None
    assert result['discovery_albums'][0]['browse_id'] == 'new-album'
    assert result['discovery_artists'][0]['artist'] == 'New artist'
    assert home_prepared.load_discovery(library) == result


@pytest.mark.parametrize('change', ['feedback', 'preferences', 'model', 'schema', 'external-writer'])
def test_captured_rankings_finish_despite_input_changes_but_preserve_the_schema_boundary(library, monkeypatch, change):
    calculate = mode_surfaces._calculate_bundles
    old_heads = library.recommendations.heads()

    def superseded(reader, now, policies):
        result = calculate(reader, now, policies)
        playback_change(library, 'live')
        if change == 'feedback':
            seed_dislike(library, 'song 000|artist 0', until=None, now=now)
        elif change == 'preferences':
            library.rec_model.set_weight('genre:techno', .5, now=now)
        elif change == 'model':
            assert library.conn.execute(
                "SELECT count(*) FROM recording_embedding_files WHERE space='content_library'").fetchone()[0]
            with library.conn:
                library.conn.execute("DELETE FROM recording_embedding_files WHERE space='content_library'")
        elif change == 'schema':
            with library.conn:
                library.conn.execute('CREATE TABLE unrelated_new_schema (value TEXT)')
        else:
            with closing(sqlite3.connect(library._db_path)) as other, other:
                other.execute("UPDATE play_event_calendar SET day='1970-01-03'")
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', superseded)
    result = mode_surfaces.prepare_bundles(library, 2000)
    if change == 'schema':
        assert result is None
        assert library.recommendations.heads() == old_heads
    else:
        assert result is not None
        assert library.recommendations.heads() != old_heads
    assert not library.conn.in_transaction


@pytest.mark.parametrize('table', ['play_event_calendar', 'play_event_sources'])
def test_default_snapshot_publication_still_rejects_listening_projection_changes(library, table):
    with library.read_snapshot() as snapshot:
        snapshot.store.conn.execute('SELECT * FROM ' + table).fetchall()
        playback_change(library, 'calendar-update' if table.endswith('calendar') else 'source-update')
        assert not snapshot.publish(lambda: pytest.fail('Published changed authoritative listening data'))


def test_real_rankers_finish_from_pinned_inputs_while_playback_arrives(ranker_library, monkeypatch, record_property):
    from yt_playlist.rec import vibe_pools

    library = ranker_library
    assert not library.recommendations.heads()
    modes = library.modes.list_modes(active_only=True)
    assert modes
    record_property('prepared_mode_count', len(modes))
    account = library.accounts.get_accounts()[0].id
    key = library.tracks.identity_key_for_video('v1')
    calculating, first_play, stopped = threading.Event(), threading.Event(), threading.Event()
    ranked, writes = [], []
    rank = surfaces.for_you

    def while_playing(reader, *args, **kwargs):
        calculating.set()
        assert first_play.wait(3)
        result = rank(reader, *args, **kwargs)
        ranked.append(len(writes))
        return result

    def play():
        assert calculating.wait(3)
        # Two arrivals prove continuing writes. Replaying dozens of equivalent
        # daily events adds lock contention without another concurrency state.
        for index in range(2):
            if stopped.is_set():
                break
            library.history.record_play_event(account, key, 'v1', 150000 + index * 86400)
            writes.append(index)
            first_play.set()
            if index == 0 and stopped.wait(.02):
                break

    monkeypatch.setattr(surfaces, 'for_you', while_playing)
    # This independent control runs actual Home/Fresh rankers while playback
    # advances. Vibe family production is covered in test_vibe_pool_reads.
    monkeypatch.setattr(vibe_pools, 'prepare', lambda *args: None)
    started, cpu = time.monotonic(), time.thread_time()
    with ThreadPoolExecutor(1) as playback:
        future = playback.submit(play)
        try:
            with library.read_snapshot() as snapshot:
                before = evidence(snapshot.store)
                policies = measurement.PoolPolicies(snapshot.store, 2000)
                result, fresh = mode_surfaces._calculate_bundles(snapshot.store, 2000, policies)
                assert evidence(snapshot.store) == before
        finally:
            stopped.set()
        future.result(3)
    record_property('wall_seconds', time.monotonic() - started)
    record_property('thread_cpu_seconds', time.thread_time() - cpu)
    record_property('live_events', len(writes))
    assert result is not None and result['all']['wheelhouse']
    assert result['all']['temporal'] and result['all']['fresh']
    assert fresh == result['all']['fresh']
    assert result['1']['wheelhouse'] and result['1']['temporal']
    assert not library.recommendations.heads()
    assert not library.conn.in_transaction
    assert not library.conn.execute('PRAGMA foreign_key_check').fetchall()
    assert ranked and len(writes) == 2
    assert [row[0] for row in library.conn.execute(
        'SELECT played_at FROM play_events WHERE played_at>=150000 ORDER BY played_at')] == [150000, 236400]
    # Typed publication while listening changes is owned above by
    # test_bundle_publication_keeps_one_coherent_generation_during_playback.


def test_published_rankings_apply_later_explicit_suppression(ranker_library, monkeypatch):
    library = ranker_library
    account = library.accounts.get_accounts()[0].id
    played = library.tracks.identity_key_for_video('v1')

    def captured_rankings(reader, now, policies):
        listening_before = evidence(reader)
        keys = [f'song {index:03}|artist {index}' for index in range(9)]
        tracks = reader.rec_query.tracks_by_keys(keys)
        fresh = reader.discovery.discovered_tracks_by_keys(['new song|new artist'])
        lanes = {lane: policies.stamp([{'key': key, **tracks[key]} for key in keys], lane)
                 for lane in ('wheelhouse', 'temporal')}
        discovery = fresh['new song|new artist']
        lanes['fresh'] = policies.stamp([{'key': 'new song|new artist', **{
            name: discovery.get(name) for name in ('video_id', 'title', 'artist', 'album', 'genre', 'year')}}], 'fresh')
        for at in (150000, 236400):
            library.history.record_play_event(account, played, 'v1', at)
        assert evidence(reader) == listening_before
        return {'all': lanes, '1': lanes, '_meta': {'full_pools': True}}, lanes['fresh']

    # Real scoring is owned immediately above; this case owns the real typed
    # publication and render-time verdict against its captured lower result.
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', captured_rankings)
    result = mode_surfaces.prepare_bundles(library, 2000)
    assert result
    assert library.recommendations.head('home')['id'] == library.recommendations.head('fresh')['id']
    assert [row['key'] for row in library.recommendations.page('home', 'all', 'temporal')] == [
        row['key'] for row in result['all']['temporal']]
    assert [row[0] for row in library.conn.execute(
        'SELECT played_at FROM play_events WHERE played_at>=150000 ORDER BY played_at')] == [150000, 236400]
    # A later explicit verdict still takes effect against the published pool.
    with library.read_view() as reader:
        before = mode_surfaces.assemble_cards(reader, 2001, 0)
    assert before
    excluded = before[0]['tracks'][0]['key']
    seed_dislike(library, excluded, until=None, now=2001)
    with library.read_view() as reader:
        assert excluded in mode_surfaces.home_exclusions(reader, {excluded}, 2001)
        after = mode_surfaces.assemble_cards(reader, 2001, 0)
    assert after
    assert excluded not in {item['key'] for card in after for item in card['tracks']}
    assert library.recommendations.page('home', 'all', 'temporal')
    assert not library.conn.in_transaction
    assert not library.conn.execute('PRAGMA foreign_key_check').fetchall()
