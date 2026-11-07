"""Sync publication cannot expose a new result with stale associated stamps."""
import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import sync_state
from yt_playlist.library import sync_outcomes


@pytest.mark.parametrize('installed', [79, 107], indirect=True)
def test_failed_sync_publication_rolls_back_result_and_freshness(installed, monkeypatch):
    store, _ = installed
    first = store.sync.begin_attempt(100)
    sync_outcomes.finish_attempt(store, {'status': 'complete', 'accounts': {}, 'resources': {}},
                                 100, expected_revision=first)
    revision = store.sync.begin_attempt(200)
    before = store.sync.state()
    before_stamp = store.settings.get_setting('last_sync_at')
    if sync_state.available(store.conn):
        original = sync_state.write_prepared
        def fail(conn, *args, **kwargs):
            original(conn, *args, **kwargs)
            raise RuntimeError('publication interrupted')
        monkeypatch.setattr(sync_state, 'write_prepared', fail)
    else:
        def fail(*args, **kwargs):
            raise RuntimeError('publication interrupted')
        monkeypatch.setattr(store.settings, 'publish_values', fail)
    with pytest.raises(RuntimeError, match='publication interrupted'):
        sync_outcomes.finish_attempt(store, {'status': 'complete', 'accounts': {}, 'resources': {}},
                                     200, expected_revision=revision)
    assert store.sync.state() == before
    assert store.settings.get_setting('last_sync_at') == before_stamp
    assert not store.conn.in_transaction
