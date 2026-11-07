"""Discovery cleanup fences typed rankings without holding long reader locks.

Published ranks and explanations are immutable. Serving joins current membership,
so deletion hides expired discoveries on every surface without rewriting rankings.
The retained legacy codec is checked separately, without reviving its old writer.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core import bundle_plan, fao, json_cache
from yt_playlist.core.activity import Activity, WorkBudgetExceeded, WorkClosing
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import bundle_storage, measurement
from yt_playlist.repos import discovery_cache, proposal_cache
from yt_playlist.core import discovery_cache_policy, discovery_provenance
from yt_playlist.core.proposal_codec import encode_proposals
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles

DAY = 86400


@pytest.fixture(scope='module')
def population_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, populate) as template:
        yield template


@pytest.fixture
def database(tmp_path, population_template):
    # Every consumer owns private SQL/files; the unchanged admitted publication
    # is built once, without first cloning an empty Store for each consumer.
    with population_template.open(tmp_path / 'library.db') as store:
        store._fixture_cleanup_population = population_template.result
        yield store


def populate(store, count=4):
    population = getattr(store, '_fixture_cleanup_population', None)
    if population is not None:
        assert len(population[1]['all']['fresh']) == count
        return deepcopy(population)
    key = 'gone'
    items = [{'key': key if n == 0 else f'keep-{n}', 'title': f'Track {n}',
              'artist': 'Artist',
              'reason': 'album context and independent listening evidence ' * 10} for n in range(count)]
    # Cleanup consumes existing SQL subjects; artist-credit inference and
    # ingestion observers belong to their own tests. Import the required native
    # identities/memberships in batches before starting the cleanup race.
    owned = items[1:] + [{'key': 'owned', 'title': 'Owned', 'artist': 'Other artist'},
                         {'key': 'new-cache', 'title': 'New cache', 'artist': 'Other artist'}]
    first_id = store.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM recordings').fetchone()[0]
    subjects = [(rid, item) for rid, item in enumerate([items[0], *owned], first_id)]
    def video(item):
        return 'gone-video' if item['key'] == key else 'video-' + item['key']

    with atomic_write(store):
        store.conn.executemany('INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
            [(rid, item['key'], item['title'], item['artist']) for rid, item in subjects])
        store.conn.executemany('INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist) '
            'VALUES (?,?,?,?,?,?)', [(video(item), rid, item['title'], item['artist'],
                                    item['title'], item['artist']) for rid, item in subjects])
        store.conn.executemany('INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (?,?,?)',
            [(rid, rid, video(item)) for rid, item in subjects])
        store.conn.executemany("INSERT INTO library_memberships VALUES (?,'fixture',NULL)",
                               [(rid,) for rid, item in subjects if item['key'] != key])
        store.conn.executemany("INSERT INTO recording_aliases VALUES (?,?,'fixture')",
                               [(item['key'], rid) for rid, item in subjects])
        store.conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,0)',
                           (first_id, first_id))
        origin = discovery_provenance.origin(store.conn, 'old-album')
        discovery_provenance.retain(store.conn, first_id, origin, 'gone-video', 0)
    bundles = {'all': {'fresh': items, 'wheelhouse': [{'key': 'owned', 'title': 'Owned', 'artist': 'Other artist'}]},
               '1': {'fresh': items}, '_meta': {'full_pools': True, 'comfort_pool': 12},
               '_radio': {'members': {'1': [key, 'keep-1']}}}
    # Cleanup owns membership fencing, not policy/model calculation. Supply a
    # completed policy input but keep policy identities, native SQL publication,
    # explanation files and every serving slot real.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(measurement, 'serving_policy',
                      lambda *args, **kwargs: ('cleanup-policy', {'fixture': 'cleanup'}))
        # Cross real explanation-file boundaries without constructing thousands
        # of unrelated candidates. Production encoding/publication stays real.
        patch.setattr(bundle_plan, 'EXPLANATION_BYTES', 2048)
        publish_bundles(store, bundles, 1, fresh=items)
    return key, bundles


def stage_successor(store):
    """Leave a fully staged successor unpublished while cleanup uses committed heads."""
    # One real staged pool proves unpublished membership is ignored. Repeating
    # every committed surface here adds no cleanup state and rewrites the same
    # explanations through unrelated publication paths.
    item = deepcopy(store._fixture_bundle_payload['all']['fresh'][0])
    item['pool_policy'] = store.recommendations.pool('home', 'all', 'fresh')['policy_version']
    bundle = {'all': {'fresh': [item]}}
    head = store.recommendations.head('home')
    plan = bundle_storage.prepare(store.conn, bundle, None,
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
        assert snapshot.publish_ordered(lambda: store.recommendations.publish(build, 2))


def cleanup(store):
    return store.discovery.gc_discovery_pool(100 * DAY, 30, set())


def published_state(store):
    """Exact committed rank facts, file bytes and framing, excluding staged successors."""
    with store.read_view() as reader:
        return _published_state(reader)


def _published_state(store):
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
        page = store.recommendations.candidate_page(surface, mode, lane, after=after, limit=2)
        tracks.extend(page['tracks'])
        if page['exhausted']:
            return tracks
        assert page['after'] > after
        after = page['after']


SLOTS = (('home', 'all', 'fresh'), ('home', '1', 'fresh'),
         ('home', 'all', 'wheelhouse'), ('fresh', 'all', 'fresh'), ('radio', '1', 'membership'))


def visible(store):
    # All pages share one pinned SQL/file view. Opening a fresh reader for each
    # 512-row page repeats owner construction and permits inconsistent pages.
    with store.read_view() as reader:
        return {slot: served(reader, *slot) for slot in SLOTS}


def observed_state(store):
    """Capture committed files, ranks and served pages from one pinned view."""
    with store.read_view() as reader:
        return _published_state(reader), {slot: served(reader, *slot) for slot in SLOTS}


@pytest.mark.parametrize('staged', [False, True])
def test_cleanup_prepares_after_reader_close_and_allows_foreground_writes(
        database, monkeypatch, staged):
    key, bundles = populate(database)
    successor = stage_successor(database) if staged else None
    before, before_visible = observed_state(database)
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
    original_snapshot, original_prepare = database.read_snapshot, discovery_cache_policy.prepare
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
    monkeypatch.setattr(discovery_cache_policy, 'prepare', prepare)
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
    after, after_visible = observed_state(database)
    assert after_visible == {slot: [row for row in rows if row['key'] != key]
                             for slot, rows in before_visible.items()}
    # Current policy IDs, metadata, explanation bytes, other lanes and framing
    # survive exactly; even the expired recording's historical rank is retained.
    assert after == before
    assert database.recommendations.framing('home')['comfort_pool'] == 12
    assert database.recommendations.framing('home')['full_pools'] == 1
    outputs = before['home']['outputs']
    assert any(row['first_rank'] is not None and row['first_rank'] > 0 for row, _encoded in outputs)
    output_bytes = sum(len(fao.read(row['filename']).bytes) for row, _encoded in outputs)
    assert output_bytes > 0
    assert all(row['byte_size'] < 128 * 1024 for row, _encoded in outputs)
    if successor:
        assert database.conn.execute('SELECT status FROM recommendation_generations WHERE id=?',
                                      (successor.generation_id,)).fetchone()[0] == 'ready'
    assert not database.conn.in_transaction


def test_cleanup_does_not_read_or_rewrite_published_ranks_or_files(database, monkeypatch):
    """Native cleanup cost is independent of the immutable publication's size.

    The 8,000-candidate document transformation is owned by the codec cases
    below. Here an attempted bulk read or file access fails, even for this small
    native publication, rather than paying to construct unrelated large menus.
    """
    key, _bundles = populate(database)
    before, before_visible = observed_state(database)
    statements = []
    original_snapshot = database.read_snapshot

    @contextmanager
    def traced_snapshot(**kwargs):
        with original_snapshot(**kwargs) as snapshot:
            snapshot.store.conn.set_trace_callback(statements.append)
            yield snapshot

    output_directories = {Path(row['filename']).parent for state in before.values()
                          for row, _encoded in state['outputs']}

    def guard_file_access(operation):
        def guarded(filename, *args, **kwargs):
            assert Path(filename).parent not in output_directories, (
                'Discovery cleanup accessed immutable recommendation files')
            # Opening a database reader still needs its independent handle
            # lease. Guard recommendation files, not that lifetime protocol.
            return operation(filename, *args, **kwargs)
        return guarded

    with monkeypatch.context() as patch:
        patch.setattr(database, 'read_snapshot', traced_snapshot)
        patch.setattr(fao, 'read', guard_file_access(fao.read))
        patch.setattr(fao, 'write', guard_file_access(fao.write))
        database.conn.set_trace_callback(statements.append)
        try:
            assert cleanup(database) == {'albums': 0, 'artists': 0, 'tracks': 1}
        finally:
            database.conn.set_trace_callback(None)

    immutable = r'(?:recommendation_candidates|unresolved_recommendation_candidates|prepared_outputs)\b'
    assert not [sql for sql in statements if re.search(
        r'\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:main\.)?"?' + immutable, sql, re.IGNORECASE)]
    after, after_visible = observed_state(database)
    assert after == before
    assert after_visible == {slot: [row for row in rows if row['key'] != key]
                             for slot, rows in before_visible.items()}
    assert database.discovery.discovered_tracks_by_keys([key]) == {}


@pytest.mark.parametrize('change', ['cache', 'library', 'rediscovered'])
@pytest.mark.parametrize('staged', [False, True])
def test_stale_preparation_cannot_delete_membership_or_overwrite_newer_cache(
        database, monkeypatch, change, staged):
    key, bundles = populate(database)
    if staged:
        stage_successor(database)
    before = published_state(database)
    original = discovery_cache_policy.prepare
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

    monkeypatch.setattr(discovery_cache_policy, 'prepare', prepare)
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

    monkeypatch.setattr(discovery_cache_policy, 'prepare', original)
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
    original = discovery_cache_policy.prepare
    successors = []

    def prepare(rows, gone):
        prepared = original(rows, gone)
        successors.append(stage_successor(database))
        return prepared

    monkeypatch.setattr(discovery_cache_policy, 'prepare', prepare)
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

        monkeypatch.setattr(discovery_cache_policy, 'prepare', prepare)
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


def test_cleanup_retry_finishes_after_earlier_repository_commits(database, monkeypatch):
    key, _bundles = populate(database)
    database.settings.set_setting('enrich_attempts:' + key, 'old attempts')

    def fail(*args):
        assert not database.conn.in_transaction
        raise ValueError('injected membership deletion failure')

    with monkeypatch.context() as changes:
        changes.setattr(database.discovery.repository, 'delete_pool_entries', fail)
        with pytest.raises(ValueError, match='membership deletion failure'):
            cleanup(database)
    assert database.settings.get_setting('enrich_attempts:' + key) is None
    assert set(database.discovery.discovered_tracks_by_keys([key])) == {key}
    assert not database.conn.in_transaction
    assert cleanup(database) == {'albums': 0, 'artists': 0, 'tracks': 1}
    assert database.discovery.discovered_tracks_by_keys([key]) == {}


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
    prepared = {surface: raw for raw, surface in discovery_cache_policy.prepare(rows, {'gone'})}
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
