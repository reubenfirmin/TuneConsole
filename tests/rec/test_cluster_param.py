from yt_playlist.core.store import Store
from yt_playlist.rec import embed, rec_params


def test_cluster_content_weight_default(tmp_path):
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    assert rec_params.get_param(s, "cluster_content_weight") == 0.30


def test_cluster_content_weight_clamped(tmp_path):
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    rec_params.set_param(s, "cluster_content_weight", 5.0)
    assert rec_params.get_param(s, "cluster_content_weight") == 1.0


def test_cluster_beta_default_matches_constant(tmp_path):
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    assert rec_params.get_param(s, "cluster_beta") == embed.CLUSTER_BETA


def test_cluster_beta_clamped(tmp_path):
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    rec_params.set_param(s, "cluster_beta", 9.0)            # max is 2.0
    assert rec_params.get_param(s, "cluster_beta") == 2.0


def test_cluster_seed_spread_default_matches_constant(tmp_path):
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    assert rec_params.get_param(s, "cluster_seed_spread") == embed.SEED_FANOUT


def test_cluster_seed_spread_is_advanced(tmp_path):
    assert rec_params.PARAMS_BY_NAME["cluster_seed_spread"].advanced is True
    assert rec_params.PARAMS_BY_NAME["cluster_beta"].group == "discovery"


def test_cluster_expand_reads_beta_param(tmp_path, monkeypatch):
    # cluster_expand resolves beta from the tunable when the caller passes none. Assert the wiring by
    # capturing the beta that reaches _branch_scores for a stored override.
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    rec_params.set_param(s, "cluster_beta", 1.25)
    seen = {}
    real = embed._branch_scores

    def spy(pos_keys, neg_keys, beta, *a, **k):
        seen["beta"] = beta
        return real(pos_keys, neg_keys, beta, *a, **k)

    monkeypatch.setattr(embed, "_branch_scores", spy)
    monkeypatch.setattr(embed, "load_vectors", lambda store: (["x"], __import__("numpy").zeros((1, 4), dtype="float32"), {"x": 0}))
    embed.cluster_expand(s, pos_keys=["x"])
    assert seen["beta"] == 1.25
