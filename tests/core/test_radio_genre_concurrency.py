"""Radio selection must not borrow SQLite's serving connection from a writer."""
from concurrent.futures import ThreadPoolExecutor
import re
import threading
from types import SimpleNamespace

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import genre_identity
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec.radio_genres import GenrePolicy
from yt_playlist.rec.radio_policy import RadioPolicy
from yt_playlist.controllers.radio_inputs import inputs_for
from yt_playlist.web.radio_persistence import RadioPersistence
from yt_playlist.web.radio_state import Phase, Session


@pytest.mark.parametrize('installed', [107], indirect=True)
@pytest.mark.parametrize('consumer', ['policy', 'directions', 'fallback', 'mismatch'])
def test_radio_selection_owns_genre_reads_while_sync_owns_writer(installed, monkeypatch, consumer):
    store, _ = installed
    with store.conn:
        store.conn.execute("UPDATE genre_catalog SET name='Renamed Synth',family_key='jazz',"
                           "family_source='custom' WHERE key='synth pop'")
    original = genre_identity.axis_match_rows
    reads = []
    locked, release = threading.Event(), threading.Event()

    def owned_read(conn, labels):
        # Assert before SQLite: a real GIL/connection-mutex deadlock would also
        # freeze pytest's own timeout handling.
        assert conn is not store.conn or store._lock.held_by_current_thread, 'Radio genre read borrowed the serving connection'
        assert locked.is_set() and not release.is_set() and not writer.done()
        labels = list(labels)
        reads.append((conn, [label for label, _ in labels]))
        return original(conn, labels)

    # Observe SQL after lookup_read has acquired a lock or a private reader.
    monkeypatch.setattr(genre_identity, 'axis_match_rows', owned_read)
    track = {'key': 'one', 'video_id': 'v1', 'genre': 'Renamed Synth'}
    other = {'key': 'two', 'video_id': 'v2', 'genre': 'Synth Pop'}

    def sync():
        with store._lock, store.conn:
            store.conn.execute("INSERT INTO settings VALUES ('radio-sync', 'pending')")
            locked.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(sync)
        assert locked.wait(2)
        try:
            # Prove the guard rejects an unsafe borrow without entering SQLite.
            with pytest.raises(AssertionError, match='borrowed the serving connection'):
                genre_identity.axis_matches(store.conn, ['Renamed Synth'])
            assert not reads
            if consumer == 'policy':
                policy = GenrePolicy(1, [track], axes=inputs_for(store).taxonomy([track['genre'], other['genre']]))
                assert policy.allows(other)
                assert policy.bonus(other) == 1
                assert policy.recent[0][:2] == ('synth pop', 'jazz')
            elif consumer == 'directions':
                candidates = SimpleNamespace(axes=inputs_for(store).taxonomy([track['genre'], other['genre']]), tuning={'theme': 1})
                directions = RadioDirections(candidates, track)
                assert directions._genres([track]).allows(other)
                assert directions._label([track, other], track)[0] == 'jazz'
            elif consumer == 'fallback':
                candidates = object.__new__(RadioCandidates)
                candidates.store = store
                candidates.membership, candidates.vectors, candidates.centroids = {}, {}, {}
                candidates.pools = {'one': [], 'two': []}
                candidates.modes = {'one': {'families': [('jazz', 2)]}, 'two': {'families': [('rock', 2)]}}
                candidates.group_head, candidates.group_allocation = None, {}
                candidates.affinities = {}
                candidates.axes = inputs_for(store).taxonomy([track['genre'], other['genre']])
                candidates._read_vectors = lambda keys: None
                assert candidates.cluster_for(track) == 'one'
                assert candidates.similarity(track, other) == 1
            else:
                view = SimpleNamespace(candidates=SimpleNamespace(
                    axes=inputs_for(store).taxonomy([track['genre'], other['genre']]), tuning={'theme': 1}))
                assert not RadioPolicy.genre_mismatch(view, SimpleNamespace(current=track), other)
        finally:
            release.set()
        writer.result(2)
    assert reads and all(conn is not store.conn and labels for conn, labels in reads)
    assert store.settings.get_setting('radio-sync') == 'pending'


@pytest.mark.parametrize('installed', [107], indirect=True)
def test_radio_checkpoint_uses_admitted_schema(installed):
    store, _ = installed
    account = store.accounts.upsert_account('Radio', None, True)
    persistence = RadioPersistence(store.radio_checkpoints)
    session = Session(None, SimpleNamespace(fingerprint='probe', tuning={}), account, 'Radio',
                      phase=Phase.STOPPED, playlist='PROBE', local_id=1)
    queries = []
    store.conn.set_trace_callback(queries.append)
    try:
        persistence.save(session)
        assert persistence.load()['episode_id'] == session.episode_id
    finally:
        store.conn.set_trace_callback(None)
    assert any('radio_sessions' in query for query in queries)
    probes = [query for query in queries if re.search(
        r'\bsqlite_(schema|master)\b|\bpragma\s+(?:main\.)?(table_|index_|foreign_key_list)', query, re.I)]
    assert not probes, '\n'.join(probes)


@pytest.mark.parametrize('installed', [107], indirect=True)
def test_home_album_readiness_owns_genre_reads(installed, monkeypatch):
    from yt_playlist.rec import album_choices

    store, _ = installed
    original = genre_identity.axis_match_rows
    reads = []

    def owned_read(conn, labels):
        assert conn is not store.conn or store._lock.held_by_current_thread, 'Unowned album genre read'
        labels = list(labels)
        reads.append([label for label, _ in labels])
        return original(conn, labels)

    monkeypatch.setattr(genre_identity, 'axis_match_rows', owned_read)
    with pytest.raises(AssertionError, match='Unowned album genre read'):
        genre_identity.axis_matches(store.conn, ['Synth Pop'])
    assert not reads
    album = {'browse_id': 'owned-read', 'genre': 'Synth Pop'}
    assert album_choices.eligible(store, [album], now=1)[0]['browse_id'] == album['browse_id']
    assert reads == [['Synth Pop']]
