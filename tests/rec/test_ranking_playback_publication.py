"""Disposable rankings can finish while the retained listening evidence advances."""
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import sqlite3
import threading
import time

import pytest

from tests.derived_rating_support import seed_dislike
from tests.prepared_fixture_support import prepared_template
from tests.rec.test_candidate_refresh import refresh
from tests.rec.test_model_refresh import seed
from yt_playlist.core import db_handles, fao, listening
from yt_playlist.rec import bundle_storage, discover, home_prepared, mode_surfaces, surfaces


def seed_discoveries(store):
    # Fresh ranks actual out-of-library discoveries, not library members.
    for index in range(12):
        store.discovery.upsert_discovered_track(
            f'new song {index}|new artist {index}', f'new{index}', f'New song {index}',
            f'New artist {index}', f'New album {index}', None,
            ('Techno', 'Jazz', 'Rock')[index // 4], str(1990 + index), None, 1800,
            duration_s=200)


def playback_library(store):
    # Keep the real mode-discovery threshold (80), all three genres and eight
    # overlapping baskets. Keep the modes produced by the real learner; the
    # independent concurrent-playback control retains 96 tracks and k=3.
    seed(store, track_count=80, mode_count=1)
    seed_discoveries(store)
    refresh(store)
    assert len(store.rec_query.library_keys()) == 80
    assert store.modes.list_modes(active_only=True)
    assert store.recommendations.page('home', 'all', 'temporal')
    assert store.recommendations.page('home', 'all', 'wheelhouse')
    assert store.recommendations.page('fresh', 'all', 'fresh')


@pytest.fixture(scope='module')
def playback_library_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, playback_library) as template:
        yield template


@pytest.fixture
def library(file_backed_store, playback_library_template):
    playback_library_template.restore(file_backed_store)
    return file_backed_store


@contextmanager
def ranking_phases(store, monkeypatch, record_property):
    """Inclusive, overlapping phase costs; wrappers always call the real owners."""
    costs = {}
    owner_thread = threading.get_ident()

    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            # The independent playback writer must not inflate ranker-thread CPU.
            if threading.get_ident() != owner_thread:
                return operation(*args, **kwargs)
            wall, cpu = time.monotonic(), time.thread_time()
            try:
                return operation(*args, **kwargs)
            finally:
                values = costs.setdefault(name, [0, 0.0, 0.0])
                values[0] += 1
                values[1] += time.monotonic() - wall
                values[2] += time.thread_time() - cpu
        return call

    with monkeypatch.context() as patch:
        operations = [(bundle_storage, 'prepare', 'plan'),
                      (db_handles.Cursor, '_run', 'sql_cursor'),
                      (fao, 'write_bytes', 'artifact_write'),
                      (fao, 'stage_bytes', 'artifact_stage'),
                      (fao.PreparedFile, 'publish', 'artifact_publish')]
        operations.extend((type(store.recommendations), name, name)
                          for name in ('claim', 'stage', 'publish'))
        for owner, attribute, name in operations:
            patch.setattr(owner, attribute, timed(name, getattr(owner, attribute)))
        try:
            yield timed
        finally:
            for name, (count, wall, cpu) in sorted(costs.items()):
                record_property(name + '_calls', count)
                record_property(name + '_wall_seconds', wall)
                record_property(name + '_thread_cpu_seconds', cpu)


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
def test_real_bundle_ranking_publishes_one_coherent_generation_during_playback(
        library, monkeypatch, change, record_property):
    prepare_change(library, change)
    other = library.accounts.upsert_account('Other listener', 'other fixture', None, False)
    other_key = library.tracks.identity_key_for_video('v11')
    library.history.import_play_events(other, [(other_key, 'v11', 500)], source='takeout')
    other_evidence = tuple(row for row in evidence(library) if row[0] == other)
    old_heads = library.recommendations.heads()
    calculate = mode_surfaces._calculate_bundles
    captured = []

    def calculate_while_playing(reader, now, policies):
        before = evidence(reader)
        result = calculate(reader, now, policies)
        playback_change(library, change)
        # Every ranker retains the same pinned evidence, including after writes.
        assert evidence(reader) == before
        captured.append(result[0])
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate_while_playing)
    with ranking_phases(library, monkeypatch, record_property) as timed:
        # Wrap the real calculation inside the callback so its timing excludes
        # the deliberately injected listening write.
        calculate = timed('ranking', calculate)
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
def test_playback_tolerance_keeps_authoritative_inputs_strict(library, monkeypatch, change):
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
    assert mode_surfaces.prepare_bundles(library, 2000) is None
    assert library.recommendations.heads() == old_heads
    assert not library.conn.in_transaction


@pytest.mark.parametrize('table', ['play_event_calendar', 'play_event_sources'])
def test_default_snapshot_publication_still_rejects_listening_projection_changes(library, table):
    with library.read_snapshot() as snapshot:
        snapshot.store.conn.execute('SELECT * FROM ' + table).fetchall()
        playback_change(library, 'calendar-update' if table.endswith('calendar') else 'source-update')
        assert not snapshot.publish(lambda: pytest.fail('Published changed authoritative listening data'))


def test_playback_keeps_arriving_while_real_rankers_finish(file_backed_store, monkeypatch, record_property):
    from yt_playlist.rec import model_refresh

    library = file_backed_store
    seed(library, distinct_albums=True)
    seed_discoveries(library)
    assert not library.recommendations.heads()
    assert model_refresh.rebuild(library, 2000) is not None
    assert len(library.rec_query.library_keys()) == 96
    modes = library.modes.list_modes(active_only=True)
    assert modes
    record_property('full_library_mode_count', len(modes))
    account = library.accounts.get_accounts()[0].id
    key = library.tracks.identity_key_for_video('v10')
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
        for index in range(64):
            if stopped.is_set():
                break
            library.history.record_play_event(account, key, 'v10', 150000 + index * 86400)
            writes.append(index)
            first_play.set()
            if stopped.wait(.02):
                break

    monkeypatch.setattr(surfaces, 'for_you', while_playing)
    started, cpu = time.monotonic(), time.thread_time()
    with ranking_phases(library, monkeypatch, record_property) as timed, ThreadPoolExecutor(1) as playback:
        calculate = mode_surfaces._calculate_bundles
        monkeypatch.setattr(mode_surfaces, '_calculate_bundles', timed('ranking', calculate))
        future = playback.submit(play)
        try:
            result = mode_surfaces.prepare_bundles(library, 2000)
        finally:
            stopped.set()
        future.result(3)
    record_property('wall_seconds', time.monotonic() - started)
    record_property('thread_cpu_seconds', time.thread_time() - cpu)
    record_property('live_events', len(writes))
    assert result is not None and result['all']['wheelhouse']
    assert result['all']['temporal'] and result['all']['fresh']
    assert library.recommendations.head('home')['id'] == library.recommendations.head('fresh')['id']
    assert [row['key'] for row in library.recommendations.page('home', 'all', 'temporal')] == [
        row['key'] for row in result['all']['temporal']]
    assert not library.conn.in_transaction
    assert not library.conn.execute('PRAGMA foreign_key_check').fetchall()
    assert ranked and len(writes) > 1
    # A later explicit verdict still takes effect against the published pool.
    with library.read_view() as reader:
        before = mode_surfaces.assemble_cards(reader, 2001, 0)
    assert before
    excluded = before[0]['tracks'][0]['key']
    seed_dislike(library, excluded, until=None, now=2001)
    with library.read_view() as reader:
        after = mode_surfaces.assemble_cards(reader, 2001, 0)
    assert after
    assert excluded not in {item['key'] for card in after for item in card['tracks']}
