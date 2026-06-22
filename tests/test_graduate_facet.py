from yt_playlist.rec import recommend, rec_params


def test_graduate_facet_nudges_weight_once_threshold_crossed(store):
    axis = "journey:energy_arc"
    # Each +1 accumulates in the theme ledger; crossing THEME_THRESHOLD nudges the permanent weight.
    n = int(rec_params.THEME_THRESHOLD) + 1
    for _ in range(n):
        recommend.graduate_facet(store, axis, 1.0, 1.0)
    assert store.get_weights().get(axis, 1.0) > 1.0          # liked -> weight rose


def test_graduate_facet_negative_lowers_weight(store):
    axis = "journey:odyssey"
    n = int(rec_params.THEME_THRESHOLD) + 1
    for _ in range(n):
        recommend.graduate_facet(store, axis, -1.0, 1.0)
    assert store.get_weights().get(axis, 1.0) < 1.0          # disliked -> weight fell (floored at GENRE_MIN=0.0)
