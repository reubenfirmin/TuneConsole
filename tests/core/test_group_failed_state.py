"""Legitimate worker failure survives every grouping progress owner."""
from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from tests.group_support import recordings
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input, native_sql_calls, without_query_timing
from tests.test_group_preparation import seed
from yt_playlist.core import entity_names, group_progress, schema_inspection
from yt_playlist.core.activity import Activity
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.store import Store
from yt_playlist.rec import group_discovery, group_preparation, group_serving
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.rec.worker_queue import Job


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Grouping progress checks do not construct an HTTP application."""


@pytest.fixture(autouse=True)
def group_vocabulary(monkeypatch):
    bound_catalog(monkeypatch, entries=(('pop', 'Pop'), ('rock', 'Rock'), ('jazz', 'Jazz')))


@contextmanager
def installed(path, version, *, admit=True):
    store = Store(path)
    try:
        store.conn.execute('PRAGMA synchronous=OFF')
        install_numbered_input(store.conn, version)
        with schema_inspection.reuse_tables(store.conn):
            if admit:
                store.init_schema(defer_metadata=True)
            else:
                store.conn.execute('PRAGMA journal_mode=WAL')
                store._schema_compatibility = version < 103
                store._rebuild_source_version = None
                store._schema_recovery_pending = False
                store._bind_repositories()
                store._snapshot_revisions = Revisions(store.conn)
            yield store
    finally:
        store.conn.close()


def history(conn):
    return [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')]


@pytest.fixture
def current_group_store(tmp_path):
    with installed(tmp_path / 'current.db', 107) as store:
        yield store


@pytest.mark.parametrize('version', [107])
def test_real_worker_records_failure_and_recovers_on_retry(
        tmp_path, monkeypatch, version):
    # Startup admission is below this worker/error-handler boundary. Keep its
    # real WAL, revision guards, twelve inputs and generation publication.
    with installed(tmp_path / 'worker.db', version, admit=False) as store:
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
