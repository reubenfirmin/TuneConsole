import numpy as np
import pytest

from yt_playlist.rec import embedding_vectors as embed, scoring
from tests import model_support
from tests.repository_mock_support import no_database, repository_store


def test_playlist_taste_weights_by_play_and_scores(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    techno = [store.tracks.upsert_track(f"t{i}", f"T{i}", "TechnoBand", None, None) for i in range(8)]
    folk = [store.tracks.upsert_track(f"f{i}", f"F{i}", "FolkBand", None, None) for i in range(8)]
    pt = store.playlists.upsert_playlist(iid, "PT", "Techno Nights", 8, "h", 0.0)
    pf = store.playlists.upsert_playlist(iid, "PF", "Dad Vacation", 8, "h2", 0.0)
    store.playlists.set_playlist_tracks(pt, techno)
    store.playlists.set_playlist_tracks(pf, folk)
    for _ in range(5):                                    # you live in the techno playlist
        store.history.record_history_plays(iid, 1.0, ["t0|technoband"], source_timezone="UTC")
    store.history.record_history_plays(iid, 1.0, ["f0|folkband"], source_timezone="UTC")
    model_support.build_track_model(store, dim=4)

    pt_model = scoring.playlist_taste(store)
    assert pt_model                                       # non-empty
    keys, V, idx = embed.load_vectors(store)
    scores, _ = scoring.score_with_traces(store, pt_model, keys, V, idx, now=1.)
    assert scores["t1|technoband"] > scores["f1|folkband"]
    contributions = pt_model.context_contributions(V[idx["t1|technoband"]])
    assert pt_model.titles[contributions.argmax()] == "Techno Nights"


def test_playlist_taste_empty_without_vectors(no_database):
    store = repository_store(rec_model={'get_rec_vectors': []})
    pt = scoring.playlist_taste(store)
    store.rec_model.get_rec_vectors.assert_called_once_with()
    assert not pt
    assert pt.context_contributions(np.ones(2)).size == 0
    assert pt.allocated_scores(np.eye(2)) == pytest.approx([0., 0.])
    assert pt.allocated_scores(np.empty((0, 2))).size == 0
    single = scoring.PlaylistTaste(["Only"], np.array([[1., 0.]]), np.array([.2]))
    candidates = np.array([[1., 0.], [0., 1.], [-1., 0.], [2., 0.]])
    assert single.allocated_scores(candidates) == pytest.approx([1., 0., -1., 1.])
    assert single.allocated_scores(np.empty((0, 2))).size == 0
