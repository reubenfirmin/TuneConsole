import numpy as np

from tests.repository_mock_support import no_database, repository_store
from yt_playlist.rec import cluster_ranking, rec_params


def test_cluster_expand_reads_beta_param(no_database, monkeypatch):
    # rank_candidates resolves beta from the tunable when the caller passes none. Assert the wiring by
    # capturing the beta that reaches _branch_scores for a stored override.
    s = repository_store(
        settings={'get_setting': None},
        rec_model={'get_rec_vectors': [
            ('x', np.array([1., 0.], dtype=np.float32).tobytes()),
            ('neighbor', np.array([.9, .1], dtype=np.float32).tobytes()),
        ], 'content_generation': {'library': [], 'discovered': []}},
    )
    s.settings.get_setting.side_effect = {
        rec_params.SETTING_PREFIX + 'cluster_beta': '1.25'}.get
    seen = {}
    real = cluster_ranking._branch_scores

    def spy(pos_keys, neg_keys, beta, *a, **k):
        seen["beta"] = beta
        return real(pos_keys, neg_keys, beta, *a, **k)

    monkeypatch.setattr(cluster_ranking, "_branch_scores", spy)
    assert [key for key, _ in cluster_ranking.rank_candidates(s, pos_keys=["x"])] == ['neighbor']
    assert seen["beta"] == 1.25
