"""Startup Radio reads must never fetch through another thread's SQLite connection."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import radio_provenance
from yt_playlist.rec import transient
from yt_playlist.web.radio_launch import RadioLaunch


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
@pytest.mark.parametrize('consumer', ['adoption', 'taste'])
def test_radio_origins_use_owned_connection_while_enrichment_holds_writer(installed, monkeypatch, consumer):
    store, _ = installed
    with store._lock, store.conn:
        radio_provenance.remember(store.conn, 'known-radio')
    store.transient_radio_playlists.add('unsaved-radio')
    original = radio_provenance.ids
    connections = []

    def owned_read(conn):
        # Fail before entering SQLite: recreating the native SQLite/GIL deadlock
        # inside pytest could freeze the entire test process, including timeouts.
        assert conn is not store.conn, 'Radio fetched on the busy serving connection'
        connections.append(conn)
        return original(conn)

    monkeypatch.setattr(radio_provenance, 'ids', owned_read)
    locked, release = threading.Event(), threading.Event()

    def enrichment():
        with store._lock:
            locked.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(enrichment)
        assert locked.wait(2)
        try:
            if consumer == 'adoption':
                launch = RadioLaunch(store, None, None, None, threading.RLock(), lambda: 1)
                assert launch.adopt({'playlist_ytm_id': 'ordinary'}, {}, None) is None
            else:
                assert transient.radio_list_ids(store) == ['known-radio', 'unsaved-radio']
        finally:
            release.set()
        writer.result(2)
    assert len(connections) == 1


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
def test_nested_origin_read_keeps_own_transaction_and_updates(installed):
    store, _ = installed
    with store._lock:
        store.conn.execute('BEGIN IMMEDIATE')
        try:
            radio_provenance.remember(store.conn, 'uncommitted-radio')
            assert store.playlists.radio_playlist_ids() == ['uncommitted-radio']
        finally:
            store.conn.rollback()
    assert store.playlists.radio_playlist_ids() == []
    with store.read_view() as reader:
        assert reader.playlists.radio_playlist_ids() == []
