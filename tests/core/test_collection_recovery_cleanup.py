"""Daily collection yields between bounded batches without losing file custody."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import json
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tests.core.test_action_work_migration import backup
from tests.prepared_fixture_support import prepared_template, roots
from tests.store_fixture_support import initialize_consumer
from yt_playlist.core import collection_recovery as recovery
from yt_playlist.core import activity
from yt_playlist.core.activity import Activity, WorkBudgetExceeded
from yt_playlist.core.store import Store


def finish(store, limit=2000):
    for _ in range(limit):
        store.retention.maintain_collection_recovery()
        if not store.retention.collection_recovery_pending():
            return
    pytest.fail('Bounded collection did not complete')


def seed_recipe_owners(store, owners):
    # Exercise ordinary file publication and each owner's committed SQL writes.
    for i in range(owners):
        store.playlist_recipes.set_recipe(f'owned-{i}', {'model': 'fixture', 'value': i}, 1)
    return owners


@pytest.fixture(scope='session')
def recipe_owner_templates(_pristine_current_schema, tmp_path_factory):
    directory = tmp_path_factory.mktemp('recipe-owner-templates')
    templates = {}
    with ExitStack() as resources:
        def template(owners):
            if owners not in templates:
                home = directory / str(owners)
                home.mkdir()
                templates[owners] = resources.enter_context(prepared_template(
                    _pristine_current_schema, lambda source: seed_recipe_owners(source, owners),
                    database=home / 'library.db'))
            return templates[owners]
        yield template


@pytest.mark.parametrize('owners', [16, 1024])
def test_batches_bound_candidates_and_allow_read_write_between_turns(
        file_backed_store, monkeypatch, owners, record_property, recipe_owner_templates):
    store = file_backed_store
    assert recipe_owner_templates(owners).restore(store) == owners
    monkeypatch.setattr(recovery.OrphanCollector, 'LIMIT', 8)
    held = {Path(row[3]) for row in recovery.references(store.conn)}
    orphaned = {Path(recovery.publish(store.conn, 'playlist-recipe', f'orphan-{i}', {'value': i})[0])
                for i in range(45)}
    queries, elapsed = [], []
    store.conn.set_trace_callback(queries.append)
    try:
        for tick in range(2000):
            started = time.monotonic()
            removed = store.retention.maintain_collection_recovery()
            elapsed.append(time.monotonic() - started)
            assert 0 <= removed <= 8
            # A different connection can read and reserve the writer between turns.
            other = Store(store._db_path)
            try:
                other.conn.execute('PRAGMA busy_timeout=0')
                assert other.conn.execute('SELECT count(*) FROM rec_recipes').fetchone()[0] == owners
                with other.conn:
                    other.conn.execute("INSERT INTO settings VALUES ('gc-probe','usable') "
                                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            finally:
                other.conn.close()
            if tick == 3:
                recovery.close(store)  # A stopped worker loses only its disposable cursor.
                continue
            if not store.retention.collection_recovery_pending():
                break
        else:
            pytest.fail('Collection stopped making progress')
    finally:
        store.conn.set_trace_callback(None)
        recovery.close(store)
    assert all(path.exists() for path in held)
    assert not any(path.exists() for path in orphaned)
    ownership = [sql for sql in queries if sql.startswith('WITH page AS MATERIALIZED')]
    assert ownership and all(' IN (' in sql and ' LIMIT 2048)' in sql for sql in ownership)
    record_property('owner_count', owners)
    record_property('maximum_turn_seconds', max(elapsed))
    record_property('turn_count', len(elapsed))


def test_snapshot_and_retained_action_backup_survive_collection(file_backed_store, tmp_path):
    store = file_backed_store
    account = store.accounts.upsert_account('Owner', 'owner.json', None, True)
    store.playlists.upsert_playlist(account, 'SOURCE', 'Source', 2, '', 1)
    original_backup = backup(tmp_path, account, 'SOURCE')
    undo = {'deletions': [{'identity': account, 'ytm': 'SOURCE', 'phase': 'pending',
                          'backup': original_backup}]}
    store.actions.record_action('delete_playlist', '{}', '{}', 'delete_pending', json.dumps(undo), 2)
    retained, = [Path(row[0]) for row in store.conn.execute('SELECT retained_filename FROM action_backups')]
    exact = retained.read_bytes()
    store.playlist_recipes.set_recipe('P', {'model': 'old'}, 1)
    old = Path(store.conn.execute("SELECT recovery_filename FROM rec_recipes").fetchone()[0])
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        assert reader.playlist_recipes.get_recipe('P') == {'model': 'old'}
        store.playlist_recipes.set_recipe('P', {'model': 'new'}, 2)
        store.retention.maintain_collection_recovery()
        assert old.exists() and store.retention.collection_recovery_pending()
        assert reader.playlist_recipes.get_recipe('P') == {'model': 'old'}
    finish(store)
    assert not old.exists()
    assert retained.read_bytes() == exact
    assert store.playlist_recipes.get_recipe('P') == {'model': 'new'}


@pytest.mark.parametrize('rollback', [False, True])
def test_inflight_publication_keeps_file_until_its_writer_finishes(
        file_backed_store, monkeypatch, rollback):
    store = file_backed_store
    writer = Store(store._db_path)
    writer._bind_repositories()
    entered, release = threading.Event(), threading.Event()
    paths = []
    publish = recovery.publish_at

    def held_publish(*args, **kwargs):
        result = publish(*args, **kwargs)
        paths.append(Path(result[0]))
        entered.set()
        assert release.wait(15)
        if rollback:
            raise RuntimeError('Interrupted publication')
        return result

    monkeypatch.setattr(recovery, 'publish_at', held_publish)
    try:
        with ThreadPoolExecutor(1) as executor:
            job = executor.submit(writer.playlist_recipes.set_recipe, 'P', {'model': 'inflight'}, 1)
            try:
                assert entered.wait(5)
                assert store.retention.maintain_collection_recovery() == 0
                assert paths[0].exists()
                assert not store.conn.in_transaction
            finally:
                release.set()
            if rollback:
                with pytest.raises(RuntimeError, match='Interrupted publication'):
                    job.result(timeout=10)
            else:
                job.result(timeout=10)
        finish(store)
        assert paths[0].exists() is not rollback
    finally:
        release.set()
        writer.conn.close()
        recovery.close(store)


def test_interrupted_batch_retains_cursor_and_releases_all_protection(file_backed_store, monkeypatch):
    store = file_backed_store
    orphan = Path(recovery.publish(store.conn, 'playlist-recipe', 'orphan', {'exact': [1, 1, 2]})[0])
    with monkeypatch.context() as interrupted:
        interrupted.setattr(recovery, 'check_cancellable_work',
                            lambda: (_ for _ in ()).throw(WorkBudgetExceeded('Controlled deadline')))
        assert store.retention.maintain_collection_recovery() == 0
    assert orphan.exists() and store.retention.collection_recovery_pending() and not store.conn.in_transaction
    store.settings.set_setting('after interrupted collection', 'usable')
    finish(store)
    assert not orphan.exists()


def test_sql_deadline_interrupts_owner_scan_and_preserves_its_batch(
        file_backed_store, monkeypatch, recipe_owner_templates):
    store = file_backed_store
    assert recipe_owner_templates(1024).restore(store) == 1024
    orphan = Path(recovery.publish(store.conn, 'playlist-recipe', 'orphan', {'value': 'unowned'})[0])
    clock, interrupted_queries = [0], []

    def trace(sql):
        if sql.startswith('WITH page AS MATERIALIZED') and ' FROM rec_recipes ' in sql:
            interrupted_queries.append(sql)
            clock[0] = 1  # Expire only after SQLite has begun the actual owner query.

    with monkeypatch.context() as deadline:
        deadline.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        store.conn.set_trace_callback(trace)
        try:
            for _ in range(100):
                store.retention.maintain_collection_recovery()
                if interrupted_queries:
                    break
        finally:
            store.conn.set_trace_callback(None)
    assert interrupted_queries and orphan.exists()
    assert store.retention.collection_recovery_pending() and store._collection_orphans.batch
    assert not store.conn.in_transaction
    store.settings.set_setting('after SQL deadline', 'usable')
    finish(store)
    assert not orphan.exists()
    assert store.conn.execute('SELECT count(*) FROM rec_recipes').fetchone()[0] == 1024


def test_recipe_owner_clones_keep_complete_private_file_custody(
        recipe_owner_templates, file_backed_store, _pristine_current_schema, tmp_path):
    template = recipe_owner_templates(1024)
    first = file_backed_store
    sibling = tmp_path / 'sibling'
    sibling.mkdir()
    second = Store(sibling / 'library.db')
    try:
        initialize_consumer(second, _pristine_current_schema)
        assert template.restore(first) == template.restore(second) == 1024
        targets = (template.source, first, second)
        references = [list(recovery.references(target.conn)) for target in targets]
        for target, rows in zip(targets, references):
            assert len(rows) == 1024
            assert target.conn.execute('PRAGMA foreign_key_check').fetchall() == []
            for table, identity, kind, filename, digest, size in rows:
                assert (table, kind) == ('rec_recipes', 'playlist-recipe')
                assert recovery.read(filename, digest, size, kind=kind, owner=identity) == {
                    'model': 'fixture', 'value': identity - 1}
        assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            template.source.settings.set_setting('private owner fixture', 'forbidden')

        destinations = [roots(target.conn) for target in (first, second)]
        copied = 0
        for name, directory in template.files.items():
            assert len({directory, *(owned[name] for owned in destinations)}) == 3
            for source in directory.rglob('*'):
                if not source.is_file():
                    continue
                paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
                assert all(path.read_bytes() == source.read_bytes() for path in paths)
                assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
                copied += 1
        assert copied >= 1024
        assert all(Path(row[3]).is_relative_to(recovery.directory(target.conn))
                   for target, rows in zip(targets, references) for row in rows)

        original = [Path(rows[0][3]).read_bytes() for rows in references]
        assert original[0] == original[1] == original[2]
        Path(references[1][0][3]).write_bytes(b'Private corrupted consumer recipe')
        assert Path(references[0][0][3]).read_bytes() == original[0]
        assert Path(references[2][0][3]).read_bytes() == original[2]
        first.settings.set_setting('private owner fixture', 'first consumer')
        first.playlist_recipes.set_recipe('owned-1', {'model': 'private replacement'}, 2)
        for target in (template.source, second):
            assert target.settings.get_setting('private owner fixture') is None
            assert target.playlist_recipes.get_recipe('owned-1') == {'model': 'fixture', 'value': 1}
    finally:
        second.conn.close()


def test_worker_continues_local_batches_without_clients(file_backed_store, monkeypatch):
    from yt_playlist.library import playlist_generated
    from yt_playlist.rec import discover, rec_worker

    store = file_backed_store
    monkeypatch.setattr(recovery.OrphanCollector, 'LIMIT', 1)
    orphaned = [Path(recovery.publish(store.conn, 'playlist-recipe', f'orphan-{i}', {'value': i})[0])
                for i in range(3)]
    clock, waits, batches, discoveries = [0], [], [], []
    maintain = recovery.maintain

    def collect(target):
        batches.append(clock[0])
        return maintain(target)

    class Stop:
        def wait(self, seconds):
            waits.append(seconds)
            clock[0] += seconds
            store.settings.set_setting('between local batches', str(clock[0]))
            assert len(batches) < 20, 'Local collection did not finish'
            return len(batches) > 1 and not recovery.pending(store)

        def is_set(self):
            return False

    def remote(*args, **kwargs):
        pytest.fail('Local maintenance must work without a remote client')

    monkeypatch.setattr(recovery, 'maintain', collect)
    monkeypatch.setattr(rec_worker, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(playlist_generated, 'gc_generated_playlists', remote)
    monkeypatch.setattr(discover, 'gc_discovery', lambda ctx, now: discoveries.append(now) or {})
    worker = rec_worker.RecWorker.__new__(rec_worker.RecWorker)
    worker.ctx = SimpleNamespace(store=store, client_provider=lambda: {},
        now_fn=lambda: clock[0], logger=logging.getLogger(__name__))
    worker.work, worker._stop = Activity(), Stop()
    worker.gc_initial_s, worker.gc_tick_s = 0, 86400
    worker._gc_loop()
    assert len(batches) > 1 and batches[0] == 0 and max(batches) < 86400
    assert waits[0] == 0 and all(delay == 60 for delay in waits[1:-1])
    assert clock[0] == 86400  # Discovery keeps its original daily deadline.
    assert discoveries == [0]
    assert not any(path.exists() for path in orphaned)
