import pytest

from tests.repository_mock_support import no_database, transient_store
from yt_playlist.rec import scoring, transient, rec_params


def test_muted_family_excludes(transient_store):
    store = transient_store
    store.rec_model.get_weights.return_value = {'genre:techno': 0.}
    assert scoring.discovery_facet_weight(store, "techno", now=1.0) is None


def test_favored_family_above_one(transient_store):
    store = transient_store
    store.rec_model.get_weights.return_value = {'genre:house': 2.}
    assert scoring.discovery_facet_weight(store, "house", now=1.0) > 1.0


def test_untagged_is_neutral(transient_store):
    store = transient_store
    assert scoring.discovery_facet_weight(store, None, now=1.0) == 1.0
    assert scoring.discovery_facet_weight(store, "", now=1.0) == 1.0
    store.rec_model.get_weights.assert_not_called()


def test_neutral_family_is_one(transient_store):
    store = transient_store
    assert scoring.discovery_facet_weight(store, "ambient", now=1.0) == 1.0


def test_transient_lean_is_damped(transient_store, monkeypatch):
    """A positive transient lean nudges the weight up, but by LESS than the full in-library facet
    multiplier. Discovery stays deliberately stable."""
    store = transient_store
    monkeypatch.setattr(transient, "facet_leans", lambda s, now: {"genre:house": 1.0})
    w = scoring.discovery_facet_weight(store, "house", now=1.0)
    assert w == pytest.approx(1.175)
    assert w < 1.35  # Full in-library strength at the default gain.
    store.settings.get_setting.side_effect = {
        rec_params.SETTING_PREFIX + name: str(override)
        for name, override in (("facet_gain", 1.), ("facet_mult_min", .9), ("facet_mult_max", 4.))
    }.get
    # Outward discovery keeps the default shape even when in-library controls change.
    assert scoring.discovery_facet_weight(store, "house", 1., leans={"genre:house": 1.}) == pytest.approx(w)
    assert scoring.discovery_facet_weight(store, "house", 1., leans={"genre:house": -100.}) == pytest.approx(.35)
    assert scoring.discovery_facet_weight(store, "house", 1., leans={"genre:house": 100.}) == pytest.approx(2.)
    store.rec_model.get_lean.return_value = 2.
    assert scoring.discovery_facet_weight(store, "house", 1., leans={"genre:house": -100.}) == pytest.approx(.7)
    store.rec_model.get_lean.return_value = .5
    assert scoring.discovery_facet_weight(store, "house", 1., leans={"genre:house": 100.}) == pytest.approx(1.25)
