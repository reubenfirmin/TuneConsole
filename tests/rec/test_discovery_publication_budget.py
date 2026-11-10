"""A checkpoint deadline must not discard a discovery result already committed."""
from types import SimpleNamespace
import time

import pytest

from yt_playlist.core import activity, db_snapshot
from yt_playlist.rec import home_prepared


@pytest.mark.parametrize('expires', ['never', 'before_publication', 'after_checkpoint'])
def test_discovery_acknowledges_only_durable_publication(file_backed_store, monkeypatch, expires):
    store = file_backed_store
    checkpoints = []
    real_checkpoint = db_snapshot.checkpoint_wal
    real_albums = home_prepared.discover.pick_discovered_albums
    real_clock = time.monotonic

    def expire():
        # Advance only deadline observation; preserve the production budget,
        # data preparation, SQL, file syncing and private PASSIVE checkpoint.
        monkeypatch.setattr(activity, 'time', SimpleNamespace(
            monotonic=lambda: real_clock() + home_prepared.SUMMARY_BUDGET_S + 1))

    def checkpoint(path):
        assert not store.conn.in_transaction and not store._lock.held_by_current_thread
        checkpoints.append(path)
        result = real_checkpoint(path)
        if expires == 'after_checkpoint':
            expire()
        return result

    def albums(*args, **kwargs):
        result = real_albums(*args, **kwargs)
        if expires == 'before_publication':
            expire()
        return result

    monkeypatch.setattr(db_snapshot, 'checkpoint_wal', checkpoint)
    monkeypatch.setattr(home_prepared.discover, 'pick_discovered_albums', albums)
    result = home_prepared.prepare_discovery(store, time.time())
    saved = home_prepared.load_discovery(store)
    if expires == 'before_publication':
        assert checkpoints == []
        assert result is None and saved is None
    else:
        assert len(checkpoints) == 1
        assert saved == {'discovery_albums': [], 'discovery_artists': [], 'discovery_tracks': []}
        assert result == saved, 'Committed discovery was reported as deferred after checkpoint'
