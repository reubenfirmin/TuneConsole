"""Discovery deletion and complete cache pruning publish together without long reader locks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core import json_cache
from yt_playlist.core.activity import Activity, WorkBudgetExceeded, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.repos import discovery_cache

DAY = 86400


@pytest.fixture
def database(tmp_path):
    store = Store(tmp_path / 'cleanup.db')
    store.init_schema()
    yield store
    store.conn.close()


def populate(store, count=200):
    key = store.discovery.upsert_discovered_track(
        'gone', 'gone-video', 'Gone', 'Artist', None, None, 'House', '2000', 'old-album', 0)
    items = [{'key': key if n == 0 else f'keep-{n}', 'title': f'Track {n}',
              'description': 'album context and independent listening evidence ' * 20,
              'pool_policy': 'retained-policy'} for n in range(count)]
    bundles = {'all': {'fresh': items, 'wheelhouse': [{'key': 'owned'}]},
               '1': {'fresh': items}, '_meta': {'full_pools': True, 'comfort_pool': 12},
               '_radio': {'1': [key, 'keep-1']}}
    store.rec_proposals.put_proposals('mode_bundles', bundles, 1)
    store.rec_proposals.put_proposals('fresh_pool', items, 1)
    return key, bundles


def publish_staged(store):
    prepared = store.rec_proposals.stage_prepared_bundles(
        store.rec_proposals._proposal_json('mode_bundles'),
        store.rec_proposals._proposal_json('fresh_pool'))
    with store.read_snapshot() as snapshot:
        assert snapshot.publish(lambda: store.rec_proposals.publish_prepared_bundles(*prepared, 1))


def cleanup(store, operation):
    if operation == 'gc':
        return store.discovery.gc_discovery_pool(100 * DAY, 30, set())
    return store.discovery.remove_discovery_source('gone', 'old-album')


def raw_caches(store):
    return {surface: store.rec_proposals._proposal_json(surface)
            for surface in ('mode_bundles', 'fresh_pool')}


@pytest.mark.parametrize('operation', ['gc', 'source'])
@pytest.mark.parametrize('staged', [False, True])
def test_large_cleanup_prepares_after_reader_close_and_allows_foreground_writes(
        database, monkeypatch, operation, staged):
    key, bundles = populate(database, count=8000)
    if staged:
        publish_staged(database)
    assert len(json.dumps(bundles).encode()) > 10 * 1024 * 1024
    entered, release = threading.Event(), threading.Event()
    original_snapshot, original_prepare = database.read_snapshot, discovery_cache.prepare
    connections = []

    @contextmanager
    def tracked_snapshot(**kwargs):
        with original_snapshot(**kwargs) as snapshot:
            assert snapshot.store.conn is not database.conn
            connections.append(snapshot.store.conn)
            yield snapshot

    def prepare(rows, gone):
        assert connections
        for conn in connections:
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                conn.execute('SELECT 1')
        assert not database._lock.held_by_current_thread
        assert not database.conn.in_transaction
        entered.set()
        assert release.wait(5), 'Foreground write was blocked during cache preparation'
        return original_prepare(rows, gone)

    monkeypatch.setattr(database, 'read_snapshot', tracked_snapshot)
    monkeypatch.setattr(discovery_cache, 'prepare', prepare)
    with ThreadPoolExecutor(2) as pool:
        work = pool.submit(cleanup, database, operation)
        try:
            assert entered.wait(5)

            def foreground():
                database.settings.set_setting('interactive', 'saved')
                return database.settings.get_setting('interactive')

            assert pool.submit(foreground).result(2) == 'saved'
        finally:
            release.set()
        result = work.result(15)
    assert result == ({'albums': 0, 'artists': 0, 'tracks': 1} if operation == 'gc' else True)
    assert database.discovery.get_discovered_tracks() == []
    cleaned = database.rec_proposals.get_proposals('mode_bundles')
    expected = [item for item in bundles['all']['fresh'] if item['key'] != key]
    assert cleaned == {**bundles, 'all': {**bundles['all'], 'fresh': expected}, '1': {'fresh': expected}}
    assert database.rec_proposals.get_proposals('fresh_pool') == expected
    assert json.loads(database.rec_proposals.group_pools_json()) == {
        'all': cleaned['all'], '_meta': bundles['_meta']}
    for surface, raw in raw_caches(database).items():
        envelope = json.loads(raw)
        assert envelope[json_cache.MARKER] == json_cache.FORMAT
        assert envelope['policies'] == [{'pool_policy': 'retained-policy'}]
        assert len(raw.encode()) < len(json.dumps(cleaned if surface == 'mode_bundles' else expected).encode())
    assert not database.conn.in_transaction


@pytest.mark.parametrize('operation', ['gc', 'source'])
@pytest.mark.parametrize('change', ['cache', 'library', 'rediscovered'])
@pytest.mark.parametrize('staged', [False, True])
def test_stale_preparation_cannot_delete_membership_or_overwrite_newer_cache(
        database, monkeypatch, operation, change, staged):
    key, _bundles = populate(database)
    if staged:
        publish_staged(database)
    before = raw_caches(database)
    original = discovery_cache.prepare
    mutations = []

    def prepare(rows, gone):
        prepared = original(rows, gone)
        mutations.append(change)
        if change == 'cache':
            database.rec_proposals.put_proposals('fresh_pool', [{'key': key}, {'key': 'new-cache'}], 2)
        elif change == 'library':
            database.tracks.upsert_track('owned-video', 'Owned', 'Other artist', None, 200)
        else:
            database.discovery.upsert_discovered_track(
                key, 'gone-video', 'Gone', 'Artist', None, None, 'House', '2000', 'radio:new-source', 99 * DAY)
        return prepared

    monkeypatch.setattr(discovery_cache, 'prepare', prepare)
    with pytest.raises(WorkBudgetExceeded, match='inputs changed'):
        cleanup(database, operation)
    assert mutations == [change]
    assert set(database.discovery.discovered_tracks_by_keys([key])) == {key}
    assert 'old-album' in {row['source_token'] for row in database.discovery.discovery_sources_for_track(key)}
    if change == 'cache':
        assert database.rec_proposals.get_proposals('fresh_pool') == [{'key': key}, {'key': 'new-cache'}]
        assert database.rec_proposals._proposal_json('mode_bundles') == before['mode_bundles']
    else:
        assert raw_caches(database) == before
    assert not database.conn.in_transaction

    monkeypatch.setattr(discovery_cache, 'prepare', original)
    result = cleanup(database, operation)
    if change == 'rediscovered':
        assert result == ({'albums': 0, 'artists': 0, 'tracks': 0} if operation == 'gc' else False)
        assert set(database.discovery.discovered_tracks_by_keys([key])) == {key}
        assert raw_caches(database) == before
    else:
        assert result == ({'albums': 0, 'artists': 0, 'tracks': 1} if operation == 'gc' else True)
        assert database.discovery.get_discovered_tracks() == []
        if change == 'cache':
            assert database.rec_proposals.get_proposals('fresh_pool') == [{'key': 'new-cache'}]


@pytest.mark.parametrize('operation', ['gc', 'source'])
@pytest.mark.parametrize('failure', ['publish', 'cancel', 'invalid'])
def test_cleanup_failure_preserves_sources_membership_and_both_caches(database, monkeypatch, operation, failure):
    key, _bundles = populate(database)
    before = raw_caches(database)
    sources = database.discovery.discovery_sources_for_track(key)
    if failure == 'publish':
        original = discovery_cache.publish

        def publish(conn, prepared):
            original(conn, prepared)
            raise ValueError('injected failure after cache writes')

        monkeypatch.setattr(discovery_cache, 'publish', publish)
        error = ValueError
    elif failure == 'cancel':
        def prepare(*args):
            raise WorkClosing('injected shutdown during pruning')

        monkeypatch.setattr(discovery_cache, 'prepare', prepare)
        error = WorkClosing
    else:
        def decode(*args):
            raise ValueError('injected invalid cache')

        monkeypatch.setattr(json_cache, 'decode', decode)
        error = ValueError
    with pytest.raises(error, match='injected'):
        cleanup(database, operation)
    assert raw_caches(database) == before
    assert database.discovery.discovery_sources_for_track(key) == sources
    assert set(database.discovery.discovered_tracks_by_keys([key])) == {key}
    assert not database.conn.in_transaction


def test_direct_cache_prune_rejects_a_serving_transaction_before_decoding(database, monkeypatch):
    populate(database)

    def unexpected(*args):
        pytest.fail('Cache decoding began while the serving lock was owned')

    monkeypatch.setattr(json_cache, 'decode', unexpected)
    with database._lock, database.conn:
        with pytest.raises(RuntimeError, match='outside the serving lock'):
            database.discovery._prune_cached_tracks({'gone'})


def test_stale_discovery_retry_does_not_repeat_the_daily_generated_playlist_sweep(database, monkeypatch, caplog):
    from yt_playlist.library import playlist_generated
    from yt_playlist.rec import discover, rec_worker

    clock, waits, collections, discoveries = [0], [], [], []

    class Stop:
        def wait(self, seconds):
            waits.append(seconds)
            clock[0] += seconds
            return len(discoveries) >= 3

        def is_set(self):
            return False

    def collect(store, clients, now, *, retry_only=False):
        collections.append((now, retry_only))
        return []

    def sweep(ctx, now):
        discoveries.append(now)
        if len(discoveries) == 1:
            raise WorkBudgetExceeded('Discovery pool cleanup inputs changed; retry cleanup')
        return {'tracks': 0}

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(rec_worker, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(playlist_generated, 'gc_generated_playlists', collect)
    monkeypatch.setattr(discover, 'gc_discovery', sweep)
    worker = rec_worker.RecWorker.__new__(rec_worker.RecWorker)
    worker.ctx = SimpleNamespace(store=database, client_provider=lambda: {'configured': object()},
                                 now_fn=lambda: clock[0], logger=logging.getLogger(__name__))
    worker.work, worker._stop = Activity(), Stop()
    worker.gc_initial_s, worker.gc_tick_s = 0, DAY
    worker._gc_loop()
    assert discoveries == [0, 60, DAY + 60]
    assert collections == [(0, False), (60, True), (DAY, False), (DAY + 60, True)]
    assert waits == [0, 60, DAY - 60, 60, DAY - 60]
    deferred = [record for record in caplog.records if 'discovery pool deferred' in record.message]
    assert len(deferred) == 1 and deferred[0].exc_info is None
