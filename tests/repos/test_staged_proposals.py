"""Last-good caches survive interrupted staging, stale inputs and failed cutovers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3

import pytest

from yt_playlist.core import json_cache, policy_config
from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded
from yt_playlist.core.db_space import LowDiskSpace
from yt_playlist.core.migrations import v063_proposal_cache
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store
from yt_playlist.repos import policy_storage, proposal_cache
from yt_playlist.repos.rec_proposals import encode_proposals


@pytest.fixture
def database(tmp_path):
    store = Store(tmp_path / 'cache.db')
    store.init_schema()
    yield store
    store.conn.close()


def publish(store, prepared, now=2):
    with store.read_snapshot() as snapshot:
        return snapshot.publish(lambda: store.rec_proposals.publish_prepared_bundles(*prepared, now))


def stage(store, bundles, fresh=None):
    return store.rec_proposals.stage_prepared_bundles(
        encode_proposals('mode_bundles', bundles), encode_proposals('fresh_pool', fresh or []))


def large_json():
    # UTF-8 characters cross chunk boundaries. Keep the bytes uncompressed so
    # this test exercises several physical commits rather than a tiny envelope.
    return json.dumps({'all': {'fresh': [{'key': f'song-{n}', 'title': 'café 🎵' * 1000}
                                       for n in range(20)]}, '_meta': {'full_pools': True}}, ensure_ascii=False)


@pytest.mark.parametrize('kind,name', [
    ('INDEX', 'ix_proposal_cache_expiry'), ('TRIGGER', 'proposal_cache_chunk_update'),
    ('TRIGGER', 'proposal_cache_legacy_update')])
def test_schema_validation_requires_cache_constraints(database, kind, name):
    database.conn.execute(f'DROP {kind} {name}')
    with pytest.raises(MigrationError, match=name):
        v063_proposal_cache.validate(database.conn)


def test_staging_releases_writer_between_bounded_commits_and_keeps_legacy_visible(database, monkeypatch):
    database.rec_proposals.put_proposals('mode_bundles', {'old': True}, 1)
    raw = large_json()
    original, sizes = proposal_cache._write, []
    with ThreadPoolExecutor(1) as foreground:
        @contextmanager
        def observed(repo):
            with original(repo) as conn:
                before = {(row[0], row[1]): row[2] for row in conn.execute(
                    'SELECT cache_id,chunk_index,length(payload) FROM rec_proposal_cache_chunks')}
                yield conn
                after = {(row[0], row[1]): row[2] for row in conn.execute(
                    'SELECT cache_id,chunk_index,length(payload) FROM rec_proposal_cache_chunks')}
                sizes.append(sum(size for key, size in after.items() if key not in before))
            assert not repo._lock.held_by_current_thread
            assert not repo.conn.in_transaction
            assert repo.get_proposals('mode_bundles') == {'old': True}
            foreground.submit(database.settings.set_setting, 'foreground', 'saved').result(5)

        monkeypatch.setattr(proposal_cache, '_write', observed)
        prepared = database.rec_proposals.stage_prepared_bundles(raw, '[]')
    monkeypatch.setattr(proposal_cache, '_write', original)
    assert len(raw.encode()) > 3 * proposal_cache.CHUNK_BYTES
    assert max(sizes) == proposal_cache.CHUNK_BYTES == v063_proposal_cache.CHUNK_BYTES
    assert publish(database, prepared)
    assert database.rec_proposals._proposal_json('mode_bundles') == raw
    assert database.rec_proposals.get_proposals('mode_bundles') == json.loads(raw)
    # Cutover must not free/rewrite the large legacy row while holding the lock.
    assert database.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?',
                                 ('mode_bundles',)).fetchone()[0] == '{"old":true}'


def test_interrupted_prefix_resumes_after_store_reopen_without_reinserting_chunks(tmp_path, monkeypatch):
    path = tmp_path / 'resume.db'
    database = Store(path)
    database.init_schema()
    database.rec_proposals.put_proposals('mode_bundles', {'old': True}, 1)
    raw = large_json()
    original = proposal_cache._write

    @contextmanager
    def interrupted(repo):
        with original(repo) as conn:
            yield conn
            if conn.execute('SELECT count(*) FROM rec_proposal_cache_chunks').fetchone()[0] == 3:
                raise WorkClosing('injected shutdown')

    monkeypatch.setattr(proposal_cache, '_write', interrupted)
    with pytest.raises(WorkClosing, match='injected shutdown'):
        proposal_cache.stage(database.rec_proposals, 'mode_bundles', raw)
    prefix = database.conn.execute('SELECT cache_id,chunk_index,payload FROM rec_proposal_cache_chunks '
                                   'ORDER BY chunk_index').fetchall()
    assert len(prefix) == 2
    cache_id = prefix[0][0]
    assert database.rec_proposals.get_proposals('mode_bundles') == {'old': True}
    database.conn.close()
    database = Store(path)
    database.init_schema()
    monkeypatch.setattr(proposal_cache, '_write', original)
    try:
        database.conn.execute("CREATE TEMP TRIGGER no_repeated_prefix BEFORE INSERT ON rec_proposal_cache_chunks "
                              "WHEN NEW.chunk_index<2 AND NEW.cache_id='" + cache_id + "' BEGIN "
                              "SELECT RAISE(ABORT,'Committed prefix was repeated'); END")
        prepared = database.rec_proposals.stage_prepared_bundles(raw, '[]')
        assert prepared[0].cache_id == cache_id
        assert publish(database, prepared)
        assert database.rec_proposals._proposal_json('mode_bundles') == raw
        assert [tuple(row) for row in database.conn.execute(
            'SELECT cache_id,chunk_index,payload FROM rec_proposal_cache_chunks '
            'WHERE cache_id=? AND chunk_index<2 ORDER BY chunk_index', (cache_id,))] == [tuple(row) for row in prefix]
    finally:
        database.conn.close()


@pytest.mark.parametrize('outcome', ['success', 'stale', 'failure'])
def test_heads_policies_and_settings_publish_atomically(database, outcome):
    old = {'all': {'fresh': [{'key': 'old'}]}}
    assert publish(database, stage(database, old, ['old']))
    database.settings.set_setting('input', 'old')
    row, base = policy_config.pack({'ranker': 'new'}, 10)
    bundles = {'all': {'fresh': [{'key': 'new', 'pool_policy': row[0]}]}}
    with database.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('input') == 'old'
        # Consume the active build too. Staging another immutable build must not
        # invalidate this input, and switching its head concurrently must do so.
        assert snapshot.store.rec_proposals.get_proposals('mode_bundles') == old
        snapshot.release_reader()
        prepared = stage(database, bundles, ['new'])
        if outcome == 'stale':
            database.settings.set_setting('input', 'changed')

        def finish():
            database.rec_ledger.publish_prepared_policies([row], [base])
            database.rec_proposals.publish_prepared_bundles(*prepared, 10)
            database.settings.publish_values({'published': 'new'})
            if outcome == 'failure':
                raise ValueError('injected cutover failure')

        if outcome == 'failure':
            with pytest.raises(ValueError, match='injected cutover'):
                snapshot.publish(finish)
        else:
            assert snapshot.publish(finish) is (outcome == 'success')
    succeeded = outcome == 'success'
    assert database.rec_proposals.get_proposals('mode_bundles') == (bundles if succeeded else old)
    assert database.rec_proposals.get_proposals('fresh_pool') == (['new'] if succeeded else ['old'])
    assert database.settings.get_setting('published') == ('new' if succeeded else None)
    assert database.rec_ledger.policy_configuration(row[0]) == ({'ranker': 'new'} if succeeded else None)
    assert not database.conn.in_transaction


def test_changed_head_rejects_old_snapshot_and_reusing_active_bytes_is_not_a_change(database):
    old = {'all': {'fresh': [{'key': 'old'}]}}
    assert publish(database, stage(database, old))
    with database.read_snapshot() as snapshot:
        assert snapshot.store.rec_proposals.get_proposals('mode_bundles') == old
        snapshot.release_reader()
        assert stage(database, old)[0].cache_id
        assert snapshot.publish(lambda: database.settings.publish_values({'same': 'accepted'}))
    with database.read_snapshot() as snapshot:
        assert snapshot.store.rec_proposals.get_proposals('mode_bundles') == old
        snapshot.release_reader()
        assert publish(database, stage(database, {'new': True}))
        assert not snapshot.publish(lambda: database.settings.publish_values({'stale': 'forbidden'}))
    assert database.settings.get_setting('same') == 'accepted'
    assert database.settings.get_setting('stale') is None


def test_cleanup_is_bounded_keeps_active_cache_and_preserves_pinned_reader(database):
    raw = large_json()
    old = database.rec_proposals.stage_prepared_bundles(raw, '[]')
    assert publish(database, old)
    with database.read_snapshot() as pinned:
        assert pinned.store.rec_proposals._proposal_json('mode_bundles') == raw
        assert publish(database, stage(database, {'new': True}))
        while True:
            before = database.conn.execute('SELECT count(*) FROM rec_proposal_cache_chunks').fetchone()[0]
            changed = proposal_cache.cleanup(database.rec_proposals, before=float('inf'))
            after = database.conn.execute('SELECT count(*) FROM rec_proposal_cache_chunks').fetchone()[0]
            assert before - after in (0, 1)
            if not changed:
                break
        assert pinned.store.rec_proposals._proposal_json('mode_bundles') == raw
    assert database.rec_proposals.get_proposals('mode_bundles') == {'new': True}
    assert database.rec_proposals.get_proposals('fresh_pool') == []
    assert database.conn.execute('SELECT 1 FROM rec_proposal_cache_builds WHERE cache_id=?',
                                 (old[0].cache_id,)).fetchone() is None


def test_low_disk_space_during_staging_keeps_last_good_cache(database, monkeypatch):
    assert publish(database, stage(database, {'old': True}))
    original = proposal_cache.require_database_space
    calls = 0

    def full(conn, growth):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise LowDiskSpace('injected full disk')
        return original(conn, growth)

    monkeypatch.setattr(proposal_cache, 'require_database_space', full)
    with pytest.raises(LowDiskSpace, match='injected full disk'):
        database.rec_proposals.stage_prepared_bundles(large_json(), '[]')
    assert database.rec_proposals.get_proposals('mode_bundles') == {'old': True}
    assert not database.conn.in_transaction


def test_incomplete_build_cannot_publish_skip_a_chunk_or_become_ready(database, monkeypatch):
    original = proposal_cache._write

    @contextmanager
    def interrupted(repo):
        with original(repo) as conn:
            yield conn
            if conn.execute('SELECT count(*) FROM rec_proposal_cache_chunks').fetchone()[0] == 2:
                raise WorkClosing('injected shutdown')

    monkeypatch.setattr(proposal_cache, '_write', interrupted)
    with pytest.raises(WorkClosing):
        proposal_cache.stage(database.rec_proposals, 'mode_bundles', large_json())
    cache_id, cursor = database.conn.execute('SELECT cache_id,written_chunks FROM rec_proposal_cache_builds').fetchone()
    assert cursor == 1
    with database.conn:
        database.conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(WorkBudgetExceeded, match='expired before publication'):
            proposal_cache.finish(database.conn, proposal_cache.PreparedCache('mode_bundles', cache_id), 2)
    with pytest.raises(sqlite3.IntegrityError, match='transition'), database.conn:
        database.conn.execute('UPDATE rec_proposal_cache_builds SET state=1')
    with pytest.raises(sqlite3.IntegrityError, match='prefix'), database.conn:
        database.conn.execute('INSERT INTO rec_proposal_cache_chunks VALUES (?,?,?)',
                              (cache_id, cursor + 1, b'x' * proposal_cache.CHUNK_BYTES))
    assert database.rec_proposals.get_proposals('mode_bundles') is None


def test_concurrent_identical_stagers_share_one_complete_build(database):
    raw = large_json()
    with ThreadPoolExecutor(2) as workers:
        attempts = [workers.submit(proposal_cache.stage, database.rec_proposals, 'mode_bundles', raw)
                    for _ in range(2)]
        first, second = [attempt.result(15) for attempt in attempts]
    assert first == second
    assert database.conn.execute('SELECT count(*) FROM rec_proposal_cache_builds').fetchone()[0] == 1
    assert database.conn.execute('SELECT written_chunks,chunk_count,state FROM rec_proposal_cache_builds').fetchone()[:] == (
        (len(raw.encode()) + proposal_cache.CHUNK_BYTES - 1) // proposal_cache.CHUNK_BYTES,
        (len(raw.encode()) + proposal_cache.CHUNK_BYTES - 1) // proposal_cache.CHUNK_BYTES, 1)
    assert database.rec_proposals.get_proposals('mode_bundles') is None


def test_interrupted_cleanup_rebuilds_from_zero_and_never_exposes_partial_bytes(database):
    raw = large_json()
    old = proposal_cache.stage(database.rec_proposals, 'mode_bundles', raw)
    assert proposal_cache.cleanup(database.rec_proposals, before=float('inf'))
    assert database.conn.execute('SELECT state FROM rec_proposal_cache_builds WHERE cache_id=?',
                                 (old.cache_id,)).fetchone()[0] == 2
    prepared = database.rec_proposals.stage_prepared_bundles(raw, '[]')
    assert prepared[0] == old
    assert publish(database, prepared)
    assert database.rec_proposals._proposal_json('mode_bundles') == raw


@pytest.mark.parametrize('operation', ['put', 'update', 'delete'])
def test_legacy_replacement_and_raw_invalidation_remove_active_head(database, operation):
    assert publish(database, stage(database, {'new': True}))
    if operation == 'put':
        database.rec_proposals.put_proposals('mode_bundles', {'replacement': True}, 3)
    else:
        with database.conn:
            if operation == 'update':
                database.conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='mode_bundles'")
            else:
                database.conn.execute('DELETE FROM rec_proposals')
    assert database.rec_proposals.get_proposals('mode_bundles') == (
        {'replacement': True} if operation == 'put' else {} if operation == 'update' else None)
    assert database.rec_proposals.has_proposals('mode_bundles') is (operation == 'put')
    assert database.conn.execute("SELECT 1 FROM rec_proposal_cache_heads WHERE surface='mode_bundles'").fetchone() is None


def test_database_rejects_mutating_or_cleaning_a_published_cache(database):
    prepared = stage(database, {'new': True})
    assert publish(database, prepared)
    for sql in ('UPDATE rec_proposal_cache_chunks SET payload=payload',
                'DELETE FROM rec_proposal_cache_chunks',
                'UPDATE rec_proposal_cache_builds SET state=2',
                'UPDATE rec_proposal_cache_builds SET state=0'):
        with pytest.raises(sqlite3.IntegrityError), database.conn:
            database.conn.execute(sql)
    assert database.rec_proposals.get_proposals('mode_bundles') == {'new': True}


@pytest.mark.parametrize('value,expected', [(None, False), ({}, False), ([], True), ({'all': {}}, True)])
def test_presence_matches_legacy_contract_without_reading_chunks(database, monkeypatch, value, expected):
    assert publish(database, stage(database, value))

    def no_body(*args, **kwargs):
        pytest.fail('Presence lookup loaded cache chunks')

    monkeypatch.setattr(proposal_cache, 'read', no_body)
    assert database.rec_proposals.has_proposals('mode_bundles') is expected


def test_policy_retention_uses_active_cache_and_ignores_hidden_or_retired_payloads(database):
    for version in ('old', 'active', 'unpublished'):
        database.rec_ledger.record_serving_policy(version, {'ranker': version}, 1)
    database.rec_proposals.put_proposals('mode_bundles', {'pool_policy': 'old'}, 1)
    assert publish(database, stage(database, {'pool_policy': 'active'}))
    stage(database, {'pool_policy': 'unpublished'})
    assert policy_storage.protected_references(database.conn) & {'old', 'active', 'unpublished'} == {'active'}
    database.rec_ledger._prune_policy_batch(10000)
    assert database.rec_ledger.policy_configuration('active') == {'ranker': 'active'}
    assert database.rec_ledger.policy_configuration('old') is None
    assert database.rec_ledger.policy_configuration('unpublished') is None


def test_staged_group_projection_does_not_inflate_mode_copies(database, monkeypatch):
    items = [{'key': str(n), 'musical': {'evidence': 'repeat ' * 1000}} for n in range(100)]
    bundles = {'all': {'fresh': items}, '_meta': {'full_pools': True}, '1': {'fresh': items}}
    prepared = stage(database, bundles, items)
    assert publish(database, prepared)
    envelope = json.loads(database.rec_proposals._proposal_json('mode_bundles'))
    original, unpacked = json_cache.unpack, []

    def unpack(encoded):
        unpacked.append(encoded)
        assert encoded != envelope['json'], 'Group projection inflated the mode copies'
        return original(encoded)

    monkeypatch.setattr(json_cache, 'unpack', unpack)
    assert json.loads(database.rec_proposals.group_pools_json()) == {
        'all': bundles['all'], '_meta': bundles['_meta']}
    assert unpacked == [envelope['groups'], envelope['profiles']]
