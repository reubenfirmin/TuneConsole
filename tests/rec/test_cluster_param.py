from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params


def test_cluster_content_weight_default(tmp_path):
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    assert rec_params.get_param(s, "cluster_content_weight") == 0.30


def test_cluster_content_weight_clamped(tmp_path):
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    rec_params.set_param(s, "cluster_content_weight", 5.0)
    assert rec_params.get_param(s, "cluster_content_weight") == 1.0
