"""Listening is the fallback for libraries without playlist, album or choice contexts."""
import numpy as np
import pytest

from yt_playlist.rec import scoring
from yt_playlist.rec.scoring import _behavior_centroid


def test_behavior_centroid_from_played_tracks(monkeypatch, store):
    M = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]], dtype=np.float32)
    idx = {"a|x": 0, "b|y": 1, "c|z": 2}
    monkeypatch.setattr(store.rec_query, "play_counts", lambda: {"a|x": 5, "b|y": 1}, raising=False)
    c = _behavior_centroid(store, M, idx)
    assert c is not None and abs(np.linalg.norm(c) - 1.0) < 1e-6
    assert c[0] > 0 and c[1] > 0                       # blends the two played tracks (c|z unplayed, excluded)


def test_behavior_centroid_none_when_nothing_played(monkeypatch, store):
    monkeypatch.setattr(store.rec_query, "play_counts", lambda: {}, raising=False)
    assert _behavior_centroid(store, np.eye(2, dtype=np.float32), {"a|x": 0, "b|y": 1}) is None


@pytest.mark.parametrize('source', ['playlist', 'album', 'choice'])
def test_populated_contexts_do_not_read_behavior_evidence(monkeypatch, store, source):
    V = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    monkeypatch.setattr(scoring.vectors, "load_vectors", lambda s: (["a|x", "b|y"], V, {"a|x": 0, "b|y": 1}))
    if source == 'playlist':
        iid = store.identities.upsert_identity('main', 'cred', None, True)
        tid = store.tracks.upsert_track('v', 'A', 'X', None, 100)
        pid = store.playlists.upsert_playlist(iid, 'PL', 'Curated', 1, 'hash', 1)
        store.playlists.set_playlist_tracks(pid, [tid])
    elif source == 'album':
        store.collection.add_saved_album({'browse': 'record', 'artist': 'X', 'title': 'Saved'})
        store.tracks.upsert_track('v', 'A', 'X', 'Saved', 100, album_browse_id='record')
    else:
        store.rec_proposals.put_proposals('choice_interests', {'contexts': [{
            'format': 'mixes', 'option_id': 'mix', 'keys': ['a|x'], 'at': 100,
            'days': 1, 'episodes': 1, 'episode_support': 1}]}, 100)
    monkeypatch.setattr(store.rec_query, 'play_counts', lambda: pytest.fail('Populated taste loaded listening fallback'))
    pt = scoring.playlist_taste(store, now=110)
    assert pt and pt.titles != ['Your listening']
    assert pt.centroids == pytest.approx(np.array([[1., 0.]]))
    assert abs(pt.weights.sum() - 1.0) < 1e-9


def test_empty_contexts_keep_the_listening_fallback(monkeypatch, store):
    V = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    monkeypatch.setattr(scoring.vectors, "load_vectors", lambda s: (["a|x", "b|y"], V, {"a|x": 0, "b|y": 1}))
    monkeypatch.setattr(store.rec_query, "play_counts", lambda: {"b|y": 3}, raising=False)
    pt = scoring.playlist_taste(store)
    assert pt.titles == ["Your listening"]
    assert pt.centroids == pytest.approx(np.array([[0., 1.]]))
    assert pt.weights == pytest.approx([1.])
