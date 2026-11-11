"""Admitted discovery finishes; only durable publications are acknowledged."""
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
    if expires == 'never':
        publish = store.rec_proposals.publish_prepared_home
        stopping = [False]
        # Exercise both failures at the real pre-commit boundary, without
        # adding another database fixture or another parametrized test case.
        for failure in ('write', 'cancel'):
            def fail_before_commit(*args, **kwargs):
                publish(*args, **kwargs)
                if failure == 'write':
                    raise OSError('Controlled discovery write failure')
                stopping[0] = True

            with monkeypatch.context() as patch:
                patch.setattr(store.rec_proposals, 'publish_prepared_home', fail_before_commit)
                with pytest.raises(OSError if failure == 'write' else activity.WorkClosing), \
                        activity.cancellable_work(lambda: stopping[0]):
                    home_prepared.prepare_discovery(store, time.time())
            assert not store.conn.in_transaction
            assert checkpoints == []
            assert home_prepared.load_discovery(store) is None
            stopping[0] = False
    result = home_prepared.prepare_discovery(store, time.time())
    saved = home_prepared.load_discovery(store)
    assert len(checkpoints) == 1
    assert saved == {'discovery_albums': [], 'discovery_artists': [], 'discovery_tracks': []}
    assert result == saved, 'Committed discovery was reported as deferred after checkpoint'
