"""Legitimate worker failure survives every grouping progress owner."""
import logging
from types import SimpleNamespace

import pytest

from tests.group_support import recordings
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, without_query_timing
from tests.test_group_preparation import seed
from yt_playlist.core.activity import Activity
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


@pytest.fixture
def current_group_store(tmp_path):
    store = Store(tmp_path / 'worker.db')
    try:
        store.conn.execute('PRAGMA synchronous=OFF')
        with bounded_consumer_schema((('pop', 'Pop'), ('rock', 'Rock'), ('jazz', 'Jazz'))) as schema:
            initialize_consumer(store, schema)
        yield store
    finally:
        store.conn.close()


def history(conn):
    return [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')]


def test_real_worker_records_failure_and_recovers_on_retry(
        current_group_store, monkeypatch):
    store = current_group_store
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
    monkeypatch.setattr(group_serving, 'prepare', lambda *_: False)
    worker._prepare_groups()
    assert store.taste_groups.head() is not None
    assert group_preparation.status(store) == {'phase': 'ready', 'at': 1295, 'reason': None}
    assert state.failures == 0 and not state.dirty
    assert history(store.conn) == before
