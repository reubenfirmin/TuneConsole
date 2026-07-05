from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_ppr_param_defaults():
    s = _store()
    assert rec_params.get_param(s, "ppr_alpha") == 0.85
    assert rec_params.get_param(s, "ppr_iters") == 50
    assert rec_params.get_param(s, "ppr_tol") == 1e-06
    assert rec_params.get_param(s, "ppr_rank_depth") == 200
    assert rec_params.get_param(s, "ppr_ab_share") == 0.5


def test_ppr_share_clamps_and_persists():
    s = _store()
    rec_params.set_param(s, "ppr_ab_share", 2.0)       # over max 1.0 -> clamps
    assert rec_params.get_param(s, "ppr_ab_share") == 1.0
    rec_params.set_param(s, "ppr_iters", 30)
    assert rec_params.get_param(s, "ppr_iters") == 30
