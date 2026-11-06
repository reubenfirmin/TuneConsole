"""Discovery cleanup fences typed rankings without holding long reader locks.

Published ranks and explanations are immutable. Serving joins current membership,
so deletion hides expired discoveries on every surface without rewriting rankings.
The retained legacy codec is checked separately, without reviving its old writer.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import logging
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core import fao, json_cache
from yt_playlist.core.activity import Activity, WorkBudgetExceeded, WorkClosing
from yt_playlist.rec import bundle_storage
from yt_playlist.repos import discovery_cache, proposal_cache
from yt_playlist.repos.rec_proposals import encode_proposals
from tests.conftest import seed_catalog_track
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles

DAY = 86400


@pytest.fixture(scope='module')
def population_templates(_pristine_current_schema):
    templates = {}
    with ExitStack() as resources:
        def template(count):
            if count not in templates:
                templates[count] = resources.enter_context(prepared_template(
                    _pristine_current_schema, lambda source: populate(source, count)))
            return templates[count]
        yield template


@pytest.fixture
def database(file_backed_store, population_templates):
    # Actual publication runs once for each unchanged size; every cleanup,
    # staged successor, race and failure still uses independent WAL/files.
    file_backed_store._fixture_cleanup_population = population_templates
    return file_backed_store


def populate(store, count=200):
    templates = getattr(store, '_fixture_cleanup_population', None)
    if templates is not None:
        return templates(count).restore(store)
    key = store.discovery.upsert_discovered_track(
        'gone', 'gone-video', 'Gone', 'Artist', None, None, 'House', '2000', 'old-album', 0)
    items = [{'key': key if n == 0 else f'keep-{n}', 'title': f'Track {n}',
              'artist': 'Artist',
              'reason': 'album context and independent listening evidence ' * 20} for n in range(count)]
    for item in items[1:] + [{'key': 'owned', 'title': 'Owned', 'artist': 'Other artist'},
                           {'key': 'new-cache', 'title': 'New cache', 'artist': 'Other artist'}]:
        seed_catalog_track(store, item['key'], item['title'], item['artist'], video_id='video-' + item['key'])
    bundles = {'all': {'fresh': items, 'wheelhouse': [{'key': 'owned', 'title': 'Owned', 'artist': 'Other artist'}]},
               '1': {'fresh': items}, '_meta': {'full_pools': True, 'comfort_pool': 12},
               '_radio': {'members': {'1': [key, 'keep-1']}}}
    publish_bundles(store, bundles, 1, fresh=items)
    return key, bundles


def stage_successor(store):
    """Leave a fully staged successor unpublished while cleanup uses committed heads."""
    bundle = deepcopy(store._fixture_bundle_payload)
    for mode, bucket in bundle.items():
        if mode.startswith('_'):
            continue
        for lane, items in bucket.items():
            version = store.recommendations.pool('home', mode, lane)['policy_version']
            for item in items:
                item['pool_policy'] = version
    head = store.recommendations.head('home')
    plan = bundle_storage.prepare(store.conn, bundle, bundle['all']['fresh'],
        external_key='cleanup-unpublished', policy_version=head['policy_version'],
        input_revision=store.prepared_content.serial(), created_at=2)
    build = store.recommendations.claim(plan)
    store.recommendations.stage(plan, build)
    assert store.recommendations.head('home') == head
    assert store.conn.execute('SELECT status FROM recommendation_generations WHERE id=?',
                              (build.generation_id,)).fetchone()[0] == 'ready'
    return build


def replace_fresh(store, items):
    """Replace only the fresh head; other published surfaces retain their owner."""
    head = store.recommendations.head('fresh')
    policy = store.recommendations.pool('fresh', 'all', 'fresh')['policy_version']
    with store.read_snapshot() as snapshot:
        plan = bundle_storage.prepare(snapshot.store.conn, {},
            [{**item, 'pool_policy': policy} for item in items], external_key='cleanup-new-fresh',
            policy_version=head['policy_version'], input_revision=snapshot.store.prepared_content.serial(), created_at=2)
        plan = replace(plan, slots=tuple(slot for slot in plan.slots if slot.surface == 'fresh'),
                       outputs=tuple(output for output in plan.outputs if output.surface == 'fresh'))
        snapshot.release_reader()
        build = store.recommendations.claim(plan)
        store.recommendations.stage(plan, build)
        assert snapshot.publish(lambda: store.recommendations.publish(build, 2))


def cleanup(store):
    return store.discovery.gc_discovery_pool(100 * DAY, 30, set())


def published_state(store):
    """Exact committed rank facts, file bytes and framing, excluding staged successors."""
    result = {}
    for surface in ('home', 'fresh', 'radio'):
        head = store.recommendations.head(surface)
        generation = head['id']
        slots = [tuple(row) for row in store.conn.execute(
            'SELECT m.mode_key,m.purpose,p.content_digest,p.policy_version,c.* '
            'FROM recommendation_surface_pools m JOIN recommendation_pools p ON p.id=m.pool_id '
            'LEFT JOIN recommendation_candidates c ON c.pool_id=p.id '
            'WHERE m.generation_id=? AND m.surface=? ORDER BY m.mode_key,m.purpose,c.rank',
            (generation, surface))]
        outputs = [(dict(row), Path(row['filename']).read_bytes()) for row in store.conn.execute(
            'SELECT * FROM prepared_outputs WHERE generation_id=? AND surface=? ORDER BY chunk_key',
            (generation, surface))]
        result[surface] = {'head': head, 'slots': slots, 'outputs': outputs,
                           'framing': store.recommendations.framing(surface)}
    return result


def served(store, surface, mode, lane):
    tracks, after = [], -1
    while True:
        page = store.recommendations.candidate_page(surface, mode, lane, after=after, limit=512)
        tracks.extend(page['tracks'])
        if page['exhausted']:
            return tracks
        assert page['after'] > after
        after = page['after']


SLOTS = (('home', 'all', 'fresh'), ('home', '1', 'fresh'),
         ('home', 'all', 'wheelhouse'), ('fresh', 'all', 'fresh'), ('radio', '1', 'membership'))


def visible(store):
    return {slot: served(store, *slot) for slot in SLOTS}


@pytest.mark.parametrize('staged,count', [
    (False, 8), (True, 8), (False, 8000),
])
def test_large_cleanup_prepares_after_reader_close_and_allows_foreground_writes(
        database, monkeypatch, staged, count):
    key, bundles = populate(database, count=count)
    successor = stage_successor(database) if staged else None
    if count == 8000:
        assert len(json.dumps(bundles).encode()) > 10 * 1024 * 1024
    before, before_visible = published_state(database), visible(database)
    expected_keys = [item['key'] for item in bundles['all']['fresh']]
    for slot in SLOTS:
        expected = (['owned'] if slot[2] == 'wheelhouse' else [key, 'keep-1']
                    if slot[0] == 'radio' else expected_keys)
        assert [row['key'] for row in before_visible[slot]] == expected
        policy = database.recommendations.pool(*slot)['policy_version']
        assert all(row['pool_policy'] == policy for row in before_visible[slot])
        if slot[2] == 'fresh':
            assert [row['reason'] for row in before_visible[slot]] == [
                item['reason'] for item in bundles['all']['fresh']]
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
        work = pool.submit(cleanup, database)
        try:
            assert entered.wait(5)

            def foreground():
                database.settings.set_setting('interactive', 'saved')
                return database.settings.get_setting('interactive')

            assert pool.submit(foreground).result(2) == 'saved'
        finally:
            release.set()
        result = work.result(15)
    assert result == {'albums': 0, 'artists': 0, 'tracks': 1}
    assert database.discovery.get_discovered_tracks() == []
    assert visible(database) == {slot: [row for row in rows if row['key'] != key]
                                 for slot, rows in before_visible.items()}
    # Current policy IDs, metadata, explanation bytes, other lanes and framing
    # survive exactly; even the expired recording's historical rank is retained.
    assert published_state(database) == before
    assert database.recommendations.framing('home')['comfort_pool'] == 12
    assert database.recommendations.framing('home')['full_pools'] == 1
    outputs = before['home']['outputs']
    output_bytes = sum(len(fao.read(row['filename']).bytes) for row, _encoded in outputs)
    assert output_bytes > 0
    if count == 8000:
        assert output_bytes > 5 * 1024 * 1024
    assert all(row['byte_size'] < 128 * 1024 for row, _encoded in outputs)
    if successor:
        assert database.conn.execute('SELECT status FROM recommendation_generations WHERE id=?',
                                      (successor.generation_id,)).fetchone()[0] == 'ready'
    assert not database.conn.in_transaction


@pytest.mark.parametrize('change', ['cache', 'library', 'rediscovered'])
@pytest.mark.parametrize('staged', [False, True])
def test_stale_preparation_cannot_delete_membership_or_overwrite_newer_cache(
        database, monkeypatch, change, staged):
    key, bundles = populate(database)
    if staged:
        stage_successor(database)
    before = published_state(database)
    original = discovery_cache.prepare
    mutations = []

    def prepare(rows, gone):
        prepared = original(rows, gone)
        mutations.append(change)
        if change == 'cache':
            replace_fresh(database, [bundles['all']['fresh'][0],
                          {'key': 'new-cache', 'title': 'New cache', 'artist': 'Other artist'}])
        elif change == 'library':
            database.tracks.upsert_track('owned-video', 'Owned', 'Other artist', None, 200)
        else:
            database.discovery.upsert_discovered_track(
                key, 'gone-video', 'Gone', 'Artist', None, None, 'House', '2000', 'radio:new-source', 99 * DAY)
        return prepared

    monkeypatch.setattr(discovery_cache, 'prepare', prepare)
    with pytest.raises(WorkBudgetExceeded, match='inputs changed'):
        cleanup(database)
    assert mutations == [change]
    assert set(database.discovery.discovered_tracks_by_keys([key])) == {key}
    assert 'old-album' in {row['source_token'] for row in database.discovery.discovery_sources_for_track(key)}
    if change == 'cache':
        assert [row['key'] for row in served(database, 'fresh', 'all', 'fresh')] == [key, 'new-cache']
        assert database.recommendations.head('fresh')['id'] != before['fresh']['head']['id']
        after = published_state(database)
        assert after['home'] == before['home'] and after['radio'] == before['radio']
    else:
        assert published_state(database) == before
    assert not database.conn.in_transaction

    monkeypatch.setattr(discovery_cache, 'prepare', original)
    result = cleanup(database)
    if change == 'rediscovered':
        assert result == {'albums': 0, 'artists': 0, 'tracks': 0}
        assert set(database.discovery.discovered_tracks_by_keys([key])) == {key}
        assert published_state(database) == before
    else:
        assert result == {'albums': 0, 'artists': 0, 'tracks': 1}
        assert database.discovery.get_discovered_tracks() == []
        if change == 'cache':
            assert [row['key'] for row in served(database, 'fresh', 'all', 'fresh')] == ['new-cache']


def test_concurrent_successor_staging_keeps_cleanup_inputs_valid(database, monkeypatch):
    key, _bundles = populate(database)
    before = published_state(database)
    original = discovery_cache.prepare
    successors = []

    def prepare(rows, gone):
        prepared = original(rows, gone)
        successors.append(stage_successor(database))
        return prepared

    monkeypatch.setattr(discovery_cache, 'prepare', prepare)
    result = cleanup(database)
    assert result == {'albums': 0, 'artists': 0, 'tracks': 1}
    assert database.discovery.discovered_tracks_by_keys([key]) == {}
    assert published_state(database) == before
    assert len(successors) == 1
    assert database.conn.execute('SELECT status FROM recommendation_generations WHERE id=?',
                                (successors[0].generation_id,)).fetchone()[0] == 'ready'
    assert key not in {row['key'] for row in served(database, 'fresh', 'all', 'fresh')}
    assert not database.conn.in_transaction


@pytest.mark.parametrize('failure', ['publish', 'cancel', 'invalid'])
def test_cleanup_failure_preserves_sources_membership_and_rankings(database, monkeypatch, failure):
    key, _bundles = populate(database)
    before = published_state(database)
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
        def read(*args, **kwargs):
            raise ValueError('injected invalid cache inventory')

        monkeypatch.setattr(proposal_cache, 'logical_rows', read)
        error = ValueError
    with pytest.raises(error, match='injected'):
        cleanup(database)
    assert published_state(database) == before
    assert database.discovery.discovery_sources_for_track(key) == sources
    assert set(database.discovery.discovered_tracks_by_keys([key])) == {key}
    assert not database.conn.in_transaction


@pytest.mark.parametrize('encoded', [False, True])
def test_legacy_cache_pruning_preserves_all_other_fields_and_compression(encoded):
    """Retain the old complete-document assertions at the explicit codec boundary."""
    items = [{'key': 'gone' if n == 0 else f'keep-{n}', 'title': f'Track {n}',
              'description': 'album context and independent listening evidence ' * 20,
              'pool_policy': 'retained-policy'} for n in range(8000)]
    bundles = {'all': {'fresh': items, 'wheelhouse': [{'key': 'owned'}]},
               '1': {'fresh': items}, '_meta': {'full_pools': True, 'comfort_pool': 12},
               '_radio': {'1': ['gone', 'keep-1']}}
    source = {'mode_bundles': bundles, 'fresh_pool': items}
    assert len(json.dumps(bundles).encode()) > 10 * 1024 * 1024
    rows = [{'surface': surface, 'payload': encode_proposals(surface, value) if encoded else json.dumps(value)}
            for surface, value in source.items()]
    prepared = {surface: raw for raw, surface in discovery_cache.prepare(rows, {'gone'})}
    expected = items[1:]
    cleaned = {**bundles, 'all': {**bundles['all'], 'fresh': expected}, '1': {'fresh': expected}}
    assert json_cache.decode(prepared['mode_bundles']) == cleaned
    assert json_cache.decode(prepared['fresh_pool']) == expected
    assert json_cache.decode(prepared['mode_bundles'], keys={'all', '_meta'}) == {
        'all': cleaned['all'], '_meta': bundles['_meta']}
    for surface, raw in prepared.items():
        envelope = json.loads(raw)
        assert envelope[json_cache.MARKER] == json_cache.FORMAT
        assert envelope['policies'] == [{'pool_policy': 'retained-policy'}]
        assert len(raw.encode()) < len(json.dumps(cleaned if surface == 'mode_bundles' else expected).encode())


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
