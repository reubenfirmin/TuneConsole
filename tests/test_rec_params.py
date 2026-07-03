"""Unit tests for the recommender parameter registry and per-genre score weighting."""
import pytest

from yt_playlist.rec import rec_params
from yt_playlist.rec.recommend import genre_adjusted_scores


# --- registry / get_param ---

def test_unset_param_returns_registry_default(store):
    # nothing stored -> the spec default, not None
    assert rec_params.get_param(store, "comfort_recency_full_days") == 30
    assert rec_params.get_param(store, "comfort_min_plays") == 4


def test_set_and_get_param_round_trips(store):
    rec_params.set_param(store, "comfort_recency_full_days", 60)
    assert rec_params.get_param(store, "comfort_recency_full_days") == 60


def test_integer_param_is_rounded_to_int(store):
    rec_params.set_param(store, "erosion_view_cap", 2.0)
    v = rec_params.get_param(store, "erosion_view_cap")
    assert v == 2 and isinstance(v, int)


def test_param_is_clamped_to_spec_range(store):
    rec_params.set_param(store, "palette_absence_penalty", 5.0)   # max is 2.0
    assert rec_params.get_param(store, "palette_absence_penalty") == 2.0
    rec_params.set_param(store, "palette_absence_penalty", -2.0)  # min is 0.0
    assert rec_params.get_param(store, "palette_absence_penalty") == 0.0


def test_reset_param_restores_default(store):
    rec_params.set_param(store, "comfort_min_plays", 5)
    rec_params.reset_param(store, "comfort_min_plays")
    assert rec_params.get_param(store, "comfort_min_plays") == 4


def test_reset_all_params_restores_every_default(store):
    rec_params.set_param(store, "comfort_recency_full_days", 14)
    rec_params.set_param(store, "comfort_min_plays", 1)
    rec_params.reset_all_params(store)
    assert rec_params.get_param(store, "comfort_recency_full_days") == 30
    assert rec_params.get_param(store, "comfort_min_plays") == 4


def test_unknown_param_raises(store):
    with pytest.raises(KeyError):
        rec_params.get_param(store, "no_such_knob")


def test_transient_param_defaults_match_constants(store):
    # #85: mood_recency_alpha (rank EMA) and stale_decay_halflife_d (sync-staleness relax) were dropped
    # with rank decay / the staleness relax; the per-source wall-clock half-lives replace them.
    pairs = [
        ("play_transient_w", rec_params.PLAY_TRANSIENT_W),
        ("like_transient_w", rec_params.LIKE_TRANSIENT_W),
        ("dislike_transient_w", rec_params.DISLIKE_TRANSIENT_W),
        ("facet_gain", rec_params.FACET_GAIN),
        ("recent_play_limit", rec_params.RECENT_PLAY_LIMIT),
        ("facet_mult_min", rec_params.FACET_MULT_MIN),
        ("facet_mult_max", rec_params.FACET_MULT_MAX),
    ]
    for name, const in pairs:
        assert name in rec_params.PARAMS_BY_NAME, name
        assert rec_params.get_param(store, name) == const, name
    assert rec_params.PARAMS_BY_NAME["mood_alpha"].default == 0.35


def test_graduation_params_registered(store):
    assert rec_params.PARAMS_BY_NAME["graduation_enabled"].boolean is True
    assert rec_params.get_param(store, "graduation_enabled") is True
    assert rec_params.get_param(store, "theme_threshold") == rec_params.THEME_THRESHOLD
    assert rec_params.get_param(store, "source_w_play") == rec_params.SOURCE_W_PLAY
    assert rec_params.PARAMS_BY_NAME["theme_threshold"].group == "graduation"


def test_boolean_param_roundtrip(store, monkeypatch):
    spec = rec_params.ParamSpec("test_flag", "Flag", "graduation", "help", 0, 1, 1, True, boolean=True)
    monkeypatch.setitem(rec_params.PARAMS_BY_NAME, "test_flag", spec)
    assert rec_params.get_param(store, "test_flag") is True          # default
    rec_params.set_param(store, "test_flag", False)
    assert rec_params.get_param(store, "test_flag") is False
    assert store.get_setting("rec_param:test_flag") == "0"


def test_registry_has_expected_groups_and_advanced_flag():
    names = {p.name for p in rec_params.PARAMS}
    assert {"comfort_recency_full_days", "comfort_min_plays", "palette_absence_penalty",
            "candidate_pool_factor"} <= names
    advanced = {p.name for p in rec_params.PARAMS if p.advanced}
    assert "candidate_pool_factor" in advanced            # the nichest knobs are collapsed


# --- per-genre score weighting ---

def test_genre_weights_no_op_when_all_neutral():
    scores = {"a": 0.5, "b": 0.4}
    genre_of = {"a": "rock", "b": "jazz"}
    assert genre_adjusted_scores(scores, genre_of, {}) == scores
    assert genre_adjusted_scores(scores, genre_of, {"rock": 1.0}) == scores


def test_genre_suppression_sinks_that_family():
    # rock (a, c) suppressed to 0 -> both drop below jazz (b), even though a scored highest
    scores = {"a": 0.5, "b": 0.4, "c": 0.3}
    genre_of = {"a": "rock", "b": "jazz", "c": "rock"}
    adj = genre_adjusted_scores(scores, genre_of, {"rock": 0.0})
    assert adj["b"] > adj["a"]
    assert adj["b"] > adj["c"]
    assert adj["a"] == 0.0 and adj["c"] == 0.0


def test_genre_boost_lifts_that_family_above_a_higher_neutral():
    # jazz (b) scores just below rock (a); a 2x boost lifts it above. c is the pool's low anchor.
    scores = {"a": 0.40, "b": 0.39, "c": 0.10}
    genre_of = {"a": "rock", "b": "jazz", "c": "rock"}
    adj = genre_adjusted_scores(scores, genre_of, {"jazz": 2.0})
    assert adj["b"] > adj["a"]
