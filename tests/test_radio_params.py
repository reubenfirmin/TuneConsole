from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_radio_param_defaults():
    s = _store()
    assert rec_params.get_param(s, "radio_artist_cap") == 3
    assert rec_params.get_param(s, "radio_candidate_pool") == 60
    assert rec_params.get_param(s, "radio_skip_artist_penalty") == 0.5
    assert rec_params.get_param(s, "radio_skip_mode_penalty") == 0.25
    assert rec_params.get_param(s, "radio_skip_halflife_h") == 2.0
    assert rec_params.get_param(s, "radio_volume_floor") == 0.1


def test_radio_params_clamp_and_persist():
    s = _store()
    rec_params.set_param(s, "radio_artist_cap", 99)          # over max 10 -> clamps
    assert rec_params.get_param(s, "radio_artist_cap") == 10
    rec_params.set_param(s, "radio_skip_artist_penalty", 0.8)
    assert rec_params.get_param(s, "radio_skip_artist_penalty") == 0.8


def test_radio_seed_depth_default_and_clamp():
    s = _store()
    assert rec_params.get_param(s, "radio_seed_depth") == 6   # raised from 3: a deeper seed keeps the queue panel visually ours
    rec_params.set_param(s, "radio_seed_depth", 99)   # over max 10 -> clamps
    assert rec_params.get_param(s, "radio_seed_depth") == 10


def test_radio_deck_size_default_and_clamp():
    s = _store()
    assert rec_params.get_param(s, "radio_deck_size") == 3
    rec_params.set_param(s, "radio_deck_size", 99)    # over max 6 -> clamps
    assert rec_params.get_param(s, "radio_deck_size") == 6
    rec_params.set_param(s, "radio_deck_size", 1)     # under min 2 -> clamps
    assert rec_params.get_param(s, "radio_deck_size") == 2
