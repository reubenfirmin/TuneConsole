"""Legitimate worker failure survives every grouping progress owner."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from tests.core.test_collection_control_preparation import finish
from tests.group_support import recordings
from tests.test_group_preparation import seed
from yt_playlist.core import entity_names, group_progress
from yt_playlist.core.activity import Activity
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import checkpoint_publication as publication, model_checkpoints as preparation
from yt_playlist.core.store import Store
from yt_playlist.rec import group_discovery, group_preparation, group_serving
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.rec.worker_queue import Job


@contextmanager
def installed(path, version):
    store = Store(path)
    try:
        if version != 107:
            runner.migrate(store.conn, registry=registry.MIGRATIONS[:version])
        store.init_schema(defer_metadata=True)
        yield store
    finally:
        store.conn.close()


def history(conn):
    return [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')]


@pytest.fixture
def current_group_store(tmp_path):
    with installed(tmp_path / 'current.db', 107) as store:
        yield store


@pytest.mark.parametrize('version', [93, 107])
def test_real_worker_records_failure_and_recovers_on_retry(tmp_path, monkeypatch, version):
    with installed(tmp_path / 'worker.db', version) as store:
        seed(store, recordings([('Pop', 12, .2)]))
        before = history(store.conn)
        now = [1234]
        worker = RecWorker(SimpleNamespace(store=store, work=Activity(),
            now_fn=lambda: now[0], logger=logging.getLogger(__name__)), enabled=False)
        discover = group_discovery.discover
        def fail(*args, **kwargs):
            raise RuntimeError('controlled discovery failure')
        monkeypatch.setattr(group_discovery, 'discover', fail)
        worker._prepare_groups()
        assert group_preparation.status(store) == {
            'phase': 'failed', 'at': 1234, 'reason': 'preparation-failed'}
        state = worker.jobs.retry[Job.GROUPS]
        assert state.failures == 1 and state.dirty and state.retry_at > state.attempted_at
        assert store.taste_groups.head() is None
        # The scheduler and grouping worker have independent bounded retry clocks.
        state.retry_at = 0
        now[0] += 61
        monkeypatch.setattr(group_discovery, 'discover', discover)
        # Isolate preview completion; the generation worker and error handler stay real.
        monkeypatch.setattr(group_serving, 'prepare', lambda *_: False)
        worker._prepare_groups()
        assert store.taste_groups.head() is not None
        assert group_preparation.status(store) == {'phase': 'ready', 'at': 1295, 'reason': None}
        assert state.failures == 0 and not state.dirty
        assert history(store.conn) == before


@pytest.mark.parametrize('admitted', [False, True])
def test_explicit_conversion_retains_failed_phase_without_rewriting_frozen_schema(tmp_path, admitted):
    with installed(tmp_path / 'conversion.db', 93) as store:
        if admitted:
            regular.migrate(store.conn)
        before = history(store.conn)
        group_preparation.report(store, 'failed', -123.5, reason='original failure')
        entity_names.clear_predecessor_reads(store.conn)
        runner.migrate(store.conn, registry=registry.MIGRATIONS[:94])
        assert history(store.conn)[:93] == before
        frozen = store.conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='group_preparation_state'").fetchone()[0]
        assert "'failed'" not in frozen
        store.init_schema(defer_metadata=True)
        assert group_preparation.status(store) == {'phase': 'failed', 'at': -123.5, 'reason': 'original failure'}
        reopened = Store(store._db_path)
        try:
            reopened.init_schema(defer_metadata=True)
            assert group_preparation.status(reopened) == group_preparation.status(store)
        finally:
            reopened.conn.close()


def test_native_adoption_keeps_old_sql_writers_and_rollback_compatible(current_group_store):
    store = current_group_store
    with store.conn:
        store.conn.execute("INSERT INTO group_preparation_state(singleton,phase,reported_at,reason) VALUES (1,'ready',10,'old')")
    # Simulate the pre-change installed owner, using its untouched frozen DDL.
    with store.conn:
        for name, _ in group_progress.definitions(store.conn):
            if name != group_progress.TABLE:
                store.conn.execute('DROP TRIGGER ' + name)
        store.conn.execute('DROP TABLE ' + group_progress.TABLE)
    before = history(store.conn)
    regular.migrate(store.conn)
    assert group_preparation.status(store) == {'phase': 'ready', 'at': 10, 'reason': 'old'}
    group_preparation.report(store, 'failed', 20, reason='new failure')
    with sqlite3.connect(store._db_path) as old_writer:
        old_writer.execute("UPDATE group_preparation_state SET phase='deferred',reported_at=30,reason='old writer'")
    assert group_preparation.status(store) == {'phase': 'deferred', 'at': 30, 'reason': 'old writer'}
    store.conn.execute('BEGIN IMMEDIATE')
    store.conn.execute("UPDATE group_preparation_state SET phase='ready',reported_at=40,reason=NULL")
    assert group_preparation.status(store)['phase'] == 'ready'
    store.conn.rollback()
    assert group_preparation.status(store)['phase'] == 'deferred'
    with pytest.raises(ValueError, match='Unsupported grouping'):
        group_preparation.report(store, 'unknown', 50)
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        store.conn.execute(f"UPDATE {group_progress.TABLE} SET phase='unknown'")
    assert group_preparation.status(store)['phase'] == 'deferred'
    assert history(store.conn) == before


def test_old_prepared_and_published_progress_can_upgrade_without_rewriting_generations(tmp_path):
    with installed(tmp_path / 'publication.db', 93) as store:
        key = group_preparation.PROGRESS
        original = json.dumps({'phase': 'deferred', 'at': 10, 'reason': 'original'}, indent=2)
        store.settings.set_setting(key, original)
        job = preparation.JOBS['group-checkpoints']
        old_job = replace(job, additive_destinations=())
        old_job.install(store.conn)
        revision = finish(old_job, store.conn).prepared_revision
        digest = hashlib.sha256(json.dumps(original, ensure_ascii=False).encode()).hexdigest()
        # Controlled fixture for the already-installed publication format. Its
        # retained DDL and immutable generation are checked by the real reader.
        with store.conn:
            for name, sql in publication.definitions():
                if name not in publication.ADDITIVE:
                    store.conn.execute(sql)
            store.conn.execute(f'INSERT INTO {publication.GENERATIONS} VALUES (?,?,?)', ('group-checkpoints', revision, 11))
            store.conn.execute(f'INSERT INTO {publication.ORIGINALS} VALUES (?,?,?,?,?)',
                               ('group-checkpoints', revision, key, digest, original))
            store.conn.execute(f'INSERT INTO {publication.LEGACY_PROGRESS} VALUES (?,?,?,?,?,?,?)',
                               ('group-checkpoints', revision, key, 'recorded', 'deferred', 10, 'original'))
            store.conn.execute(f'INSERT INTO {publication.HEAD} VALUES (?,?,1)', ('group-checkpoints', revision))
        regular.validate(store.conn)
        before = history(store.conn)
        assert group_preparation.status(store) == json.loads(original)
        old_rows = [tuple(row) for row in store.conn.execute(f'SELECT * FROM {publication.LEGACY_PROGRESS}')]
        group_preparation.report(store, 'failed', 20, reason='provider failure')
        assert publication.selected(store.conn, 'group-checkpoints') is None
        job.install(store.conn)
        next_revision = finish(job, store.conn).prepared_revision
        publication.publish(store.conn, 'group-checkpoints', revision=next_revision)
        assert group_preparation.status(store) == {'phase': 'failed', 'at': 20, 'reason': 'provider failure'}
        assert json.loads(publication.read(store.conn, 'group-checkpoints', revision, key, None)) == json.loads(original)
        assert [tuple(row) for row in store.conn.execute(f'SELECT * FROM {publication.LEGACY_PROGRESS}')] == old_rows
        with pytest.raises(sqlite3.IntegrityError, match='future cleanup'), store.conn:
            store.conn.execute(f'DELETE FROM {publication.PROGRESS_V2}')
        assert history(store.conn) == before
        reopened = Store(store._db_path)
        try:
            reopened.init_schema(defer_metadata=True)
            assert group_preparation.status(reopened)['phase'] == 'failed'
            group_preparation.report(reopened, 'ready', 30)
            assert publication.selected(reopened.conn, 'group-checkpoints') is None
            revision = finish(job, reopened.conn).prepared_revision
            publication.publish(reopened.conn, 'group-checkpoints', revision=revision)
            assert group_preparation.status(reopened) == {'phase': 'ready', 'at': 30, 'reason': None}
            with reopened.conn:
                reopened.conn.execute(f'DROP TABLE {publication.PROGRESS_V2}')
            with pytest.raises(MigrationError, match='missing its typed checkpoint'):
                regular.validate(reopened.conn)
        finally:
            reopened.conn.close()


def test_unknown_phase_keeps_original_on_preparation_and_conversion_failure(tmp_path):
    with installed(tmp_path / 'invalid.db', 93) as store:
        raw = '{"phase":"unknown","at":1234,"reason":"retain me"}'
        store.settings.set_setting(group_preparation.PROGRESS, raw)
        job = preparation.JOBS['group-checkpoints']
        job.install(store.conn)
        with pytest.raises(ValueError, match='original retained'):
            finish(job, store.conn)
        assert job.progress(store.conn).status == 'failed'
        assert store.settings.get_setting(group_preparation.PROGRESS) == raw
        entity_names.clear_predecessor_reads(store.conn)
        before = list(store.conn.iterdump())
        with pytest.raises(MigrationError, match='original retained'):
            runner.migrate(store.conn, registry=registry.MIGRATIONS[:94])
        assert list(store.conn.iterdump()) == before
