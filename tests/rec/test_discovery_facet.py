from yt_playlist.rec import recommend, rec_params


def test_muted_family_excludes(store):
    store.set_weight("genre:techno", 0.0, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)
    assert recommend.discovery_facet_weight(store, "techno", now=1.0) is None


def test_favored_family_above_one(store):
    store.set_weight("genre:house", 2.0, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)
    assert recommend.discovery_facet_weight(store, "house", now=1.0) > 1.0


def test_untagged_is_neutral(store):
    assert recommend.discovery_facet_weight(store, None, now=1.0) == 1.0
    assert recommend.discovery_facet_weight(store, "", now=1.0) == 1.0


def test_neutral_family_is_one(store):
    assert recommend.discovery_facet_weight(store, "ambient", now=1.0) == 1.0


def test_transient_lean_is_damped(store, monkeypatch):
    """A positive transient lean nudges the weight up, but by LESS than the full in-library facet
    multiplier — discovery stays deliberately stable."""
    monkeypatch.setattr(recommend.transient, "facet_leans", lambda s, now: {"genre:house": 1.0})
    w = recommend.discovery_facet_weight(store, "house", now=1.0)
    full = recommend.transient.facet_multiplier(
        1.0, rec_params.FACET_GAIN, rec_params.FACET_MULT_MIN, rec_params.FACET_MULT_MAX)
    assert 1.0 < w < full
