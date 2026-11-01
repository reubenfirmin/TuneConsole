"""Radio selection must not borrow SQLite's serving connection from a writer."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import genre_identity
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec.radio_genres import GenrePolicy
from yt_playlist.rec.radio_policy import RadioPolicy
from yt_playlist.web.radio_persistence import RadioPersistence
from yt_playlist.web.radio_state import Phase, Session


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
@pytest.mark.parametrize('consumer', ['policy', 'directions', 'fallback', 'mismatch'])
def test_radio_selection_owns_genre_reads_while_sync_owns_writer(installed, monkeypatch, consumer):
    store, _ = installed
    store.genres.rename_genre('synth pop', 'Renamed Synth')
    store.genres.set_genre_family('synth pop', 'jazz')
    original = genre_identity.resolve
    reads = []

    def owned_read(conn, label):
        # Assert before SQLite: a real GIL/connection-mutex deadlock would also
        # freeze pytest's own timeout handling.
        assert conn is not store.conn or store._lock.held_by_current_thread, 'Radio genre read borrowed the serving connection'
        reads.append(label)
        return original(conn, label)

    monkeypatch.setattr(genre_identity, 'resolve', owned_read)
    track = {'key': 'one', 'video_id': 'v1', 'genre': 'Renamed Synth'}
    other = {'key': 'two', 'video_id': 'v2', 'genre': 'Synth Pop'}
    locked, release = threading.Event(), threading.Event()

    def sync():
        with store._lock:
            locked.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(sync)
        assert locked.wait(2)
        try:
            if consumer == 'policy':
                policy = GenrePolicy(1, [track], store=store)
                assert policy.allows(other)
                assert policy.bonus(other) == 1
                assert policy.recent[0][:2] == ('synth pop', 'jazz')
            elif consumer == 'directions':
                candidates = SimpleNamespace(store=store, tuning={'theme': 1})
                directions = RadioDirections(candidates, track)
                assert directions._genres([track]).allows(other)
                assert directions._label([track, other], track)[0] == 'jazz'
            elif consumer == 'fallback':
                candidates = object.__new__(RadioCandidates)
                candidates.store = store
                candidates.membership, candidates.vectors, candidates.centroids = {}, {}, {}
                candidates.pools = {'one': [], 'two': []}
                candidates.modes = {'one': {'families': [('jazz', 2)]}, 'two': {'families': [('rock', 2)]}}
                candidates.affinities = {}
                candidates._read_vectors = lambda keys: None
                assert candidates.cluster_for(track) == 'one'
                assert candidates.similarity(track, other) == 1
            else:
                view = SimpleNamespace(candidates=SimpleNamespace(store=store, tuning={'theme': 1}))
                assert not RadioPolicy.genre_mismatch(view, SimpleNamespace(current=track), other)
        finally:
            release.set()
        writer.result(2)
    assert reads


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
def test_radio_checkpoint_schema_probes_own_the_connection(installed, monkeypatch):
    from yt_playlist.core import radio_storage

    store, _ = installed
    account = store.accounts.upsert_account('Radio', 'radio.json', None, True)
    original = radio_storage.available
    probes = []

    def available(conn):
        assert conn is not store.conn or store._lock.held_by_current_thread
        probes.append(conn)
        return original(conn)

    monkeypatch.setattr(radio_storage, 'available', available)
    persistence = RadioPersistence(store.settings)
    session = Session(None, SimpleNamespace(fingerprint='probe', tuning={}), account, 'Radio',
                      phase=Phase.STOPPED, playlist='PROBE', local_id=1)
    persistence.save(session)
    assert persistence.load()['episode_id'] == session.episode_id
    assert len(probes) >= 3


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
def test_home_album_readiness_owns_genre_reads(installed, monkeypatch):
    from yt_playlist.rec import album_choices

    store, _ = installed
    original = genre_identity.resolve

    def resolve(conn, label):
        assert conn is not store.conn or store._lock.held_by_current_thread
        return original(conn, label)

    monkeypatch.setattr(genre_identity, 'resolve', resolve)
    album = {'browse_id': 'owned-read', 'genre': 'Synth Pop'}
    assert album_choices.eligible(store, [album], now=1)[0]['browse_id'] == album['browse_id']
