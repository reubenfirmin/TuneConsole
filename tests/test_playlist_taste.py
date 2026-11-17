import numpy as np
import pytest

from yt_playlist.rec import embedding_vectors as embed, scoring
from tests.repository_mock_support import no_database, repository_store


def test_playlist_taste_weights_by_play_and_scores(no_database):
    V = np.array([[1., 0.], [1., .1], [0., 1.], [.1, 1.]])
    pt_model = scoring._centroids_from_playlist_rows(V,
        [('Techno Nights', 1, [0, 1]), ('Dad Vacation', 2, [2, 3])],
        {1: (1., 5), 2: (1., 1)})
    assert pt_model                                       # non-empty
    assert pt_model.weights == pytest.approx([5 / 6, 1 / 6])
    assert np.linalg.norm(pt_model.centroids, axis=1) == pytest.approx([1., 1.])
    scores = pt_model.allocated_scores(V)
    assert scores[1] > scores[3]
    contributions = pt_model.context_contributions(V[1])
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
