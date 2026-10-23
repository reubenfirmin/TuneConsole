"""Repair visible discovery artwork without changing the recommendation itself."""
from types import SimpleNamespace
import logging

import pytest

from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec import discover
from yt_playlist.rec.rec_worker import RecWorker


def test_missing_images_are_repaired_and_misses_back_off(store):
    for i in range(4):
        store.discovery.upsert_discovered_artist(f'Artist {i}', i, ['Anchor'], ['Context'], None, 1)
    rows = store.discovery.get_discovered_artists()
    ids = [a['artist_id'] for a in rows]
    calls = []

    def fetch(name, filter):
        assert not store.conn.in_transaction
        assert filter == 'artists'
        calls.append(name)
        return [] if name == 'Artist 1' else [{'thumbnails': [{'url': 'http://image/' + name}]}]

    ctx = SimpleNamespace(store=store, client_provider=lambda: {1: SimpleNamespace(search=fetch)})
    discover.repair_artist_images(ctx, ids[:3], 10)
    assert calls == ['Artist 0', 'Artist 1', 'Artist 2']
    repaired = {a['artist']: a for a in store.discovery.get_discovered_artists()}
    assert repaired['Artist 0']['thumbnail'] == 'http://image/Artist 0'
    assert repaired['Artist 3']['thumbnail'] is None
    assert repaired['Artist 0']['because'] == ['Anchor']
    assert repaired['Artist 0']['offered_count'] == 0
    discover.repair_artist_images(ctx, ids[:3], 20)
    assert len(calls) == 3
    discover.repair_artist_images(ctx, ids[:3], 10 + 86400)
    assert calls[-1] == 'Artist 1' and len(calls) == 4
    # A later failed discovery lookup must not erase an existing image.
    store.discovery.upsert_discovered_artist('Artist 0', 9, [], [], None, 30)
    assert store.discovery.get_discovered_artists()[0]['thumbnail'] == 'http://image/Artist 0'


@pytest.mark.parametrize('clients, bridge', [({}, None), ({1: object()}, SimpleNamespace(connected=False))])
def test_disconnected_startup_does_not_record_a_failed_image_lookup(store, clients, bridge):
    store.discovery.upsert_discovered_artist('Artist', 1, [], [], None, 1)
    artist_id = store.discovery.get_discovered_artists()[0]['artist_id']
    ctx = SimpleNamespace(store=store, client_provider=lambda: clients, bridge=bridge)
    discover.repair_artist_images(ctx, [artist_id], 10)
    assert store.discovery.artist_images_due([artist_id], 10)
    assert store.settings.get_setting(f'discovery:artist-image:{artist_id}') is None


def test_transport_failure_retries_quickly_and_does_not_drop_other_images(store):
    for name in ('Offline', 'Available'):
        store.discovery.upsert_discovered_artist(name, 1, [], [], None, 1)
    ids = [a['artist_id'] for a in store.discovery.get_discovered_artists()]

    def search(name, filter):
        if name == 'Offline':
            raise ConnectionError('Extension reconnecting')
        return [{'thumbnails': [{'url': 'http://image/artist'}]}]

    ctx = SimpleNamespace(store=store, client_provider=lambda: {1: SimpleNamespace(search=search)},
                          logger=logging.getLogger(__name__))
    discover.repair_artist_images(ctx, ids, 10)
    assert store.discovery.artist_images_due(ids, 69) == []
    assert [a['artist'] for a in store.discovery.artist_images_due(ids, 70)] == ['Offline']
    assert store.discovery.get_discovered_artists()[1]['thumbnail'] == 'http://image/artist'


def test_cancelled_image_lookup_does_not_record_a_miss(store):
    store.discovery.upsert_discovered_artist('Artist', 1, [], [], None, 1)
    artist_id = store.discovery.get_discovered_artists()[0]['artist_id']

    def search(*args, **kwargs):
        raise WorkClosing('Stopped')

    ctx = SimpleNamespace(store=store, client_provider=lambda: {1: SimpleNamespace(search=search)})
    with pytest.raises(WorkClosing):
        discover.repair_artist_images(ctx, [artist_id], 10)
    assert store.discovery.artist_images_due([artist_id], 10)


def test_worker_coalesces_images_and_limits_each_pass_to_three(store, monkeypatch):
    from yt_playlist.core.activity import Activity
    ctx = SimpleNamespace(store=store, work=Activity(), logger=logging.getLogger(__name__), now_fn=lambda: 10)
    worker = RecWorker(ctx, debounce_s=0)
    launches, batches = [], []
    monkeypatch.setattr(worker, '_start_loop', lambda: launches.append(True))
    monkeypatch.setattr(discover, 'repair_artist_images', lambda ctx, ids, now: batches.append(ids))
    for _ in range(5):
        worker.trigger_artist_images(range(10))
    assert launches == [True]
    try:
        worker._run_pending()
        assert [len(b) for b in batches] == [3, 3, 3, 1]
        assert [i for batch in batches for i in batch] == list(range(10))
        assert not worker.busy
    finally:
        worker.shutdown()
