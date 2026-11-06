"""Parameter policy uses captured settings; one workflow owns real persistence."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yt_playlist.rec import embed, rec_params


def test_parameter_overrides_persist_and_resets_preserve_unrelated_settings(store):
    store.settings.set_setting("unrelated", "keep")
    rec_params.set_param(store, "comfort_recency_full_days", 60)
    assert rec_params.get_param(store, "comfort_recency_full_days") == 60
    rec_params.set_param(store, "erosion_view_cap", 2.0)
    v = rec_params.get_param(store, "erosion_view_cap")
    assert v == 2 and isinstance(v, int)
    rec_params.set_param(store, "ppr_ab_share", 2)
    rec_params.set_param(store, "ppr_iters", 30)
    with store.read_view() as reader:
        assert rec_params.get_param(reader, "comfort_recency_full_days") == 60
        assert rec_params.get_param(reader, "ppr_ab_share") == 1
        assert rec_params.get_param(reader, "ppr_iters") == 30
    rec_params.set_param(store, "comfort_min_plays", 5)
    rec_params.reset_param(store, "comfort_min_plays")
    assert rec_params.get_param(store, "comfort_min_plays") == 4
    assert rec_params.get_param(store, "comfort_recency_full_days") == 60
    rec_params.reset_all_params(store)
    assert rec_params.get_param(store, "comfort_recency_full_days") == 30
    assert rec_params.get_param(store, "comfort_min_plays") == 4
    assert rec_params.get_param(store, "ppr_ab_share") == .5
    assert rec_params.get_param(store, "ppr_iters") == 50
    assert store.settings.get_setting("unrelated") == "keep"


def test_unknown_param_raises_without_reading_settings():
    store = SimpleNamespace(settings=Mock())
    with pytest.raises(KeyError):
        rec_params.get_param(store, "no_such_knob")
    store.settings.get_setting.assert_not_called()


def test_parameter_policy_clamps_numbers_and_recovers_missing_or_invalid_settings():
    store = SimpleNamespace(settings=Mock())
    # Numeric policy is shared across knobs; these also pin the former
    # cluster/PPR/cleanup owners' distinct registered bounds.
    cases = [
        ("breadth_bias", "5", 1.), ("breadth_bias", "-2", -1.),
        ("cluster_content_weight", "5", 1.), ("cluster_beta", "9", 2.),
        ("ppr_ab_share", "2", 1.), ("discovery_gc_days", "9999", 365),
        ("erosion_view_cap", "2.0", 2),
        ("comfort_recency_full_days", None, 30),
        ("comfort_recency_full_days", "", 30),
        ("comfort_recency_full_days", "invalid", 30),
    ]
    for name, raw, expected in cases:
        store.settings.get_setting.return_value = raw
        result = rec_params.get_param(store, name)
        assert result == expected and type(result) is type(expected), (name, raw)


def test_registry_defaults_constants_and_groups():
    store = SimpleNamespace(settings=Mock(get_setting=Mock(return_value=None)))
    expected = {
        "comfort_recency_full_days": 30, "comfort_min_plays": 4,
        "cluster_content_weight": .30, "cluster_beta": embed.CLUSTER_BETA,
        "cluster_seed_spread": embed.SEED_FANOUT, "discovery_gc_days": 30,
        "ppr_alpha": .85, "ppr_iters": 50, "ppr_tol": 1e-6,
        "ppr_rank_depth": 200, "ppr_ab_share": .5,
        "play_transient_w": .30, "like_transient_w": .45, "dislike_transient_w": 1.5,
        "facet_gain": .35, "recent_play_limit": 50, "facet_mult_min": .35,
        "facet_mult_max": 2.5, "mood_alpha": .35, "dislike_suppress_days": 365,
        "theme_threshold": 1.2, "graduate_up": 1.05, "graduate_down": .95,
        "source_w_vibe": 1., "source_w_like": 1., "source_w_like_synced": .4,
        "source_w_slider": .5, "source_w_play": .08,
    }
    for name, default in expected.items():
        spec = rec_params.PARAMS_BY_NAME[name]
        assert spec.default == rec_params.get_param(store, name) == default, name
    assert "graduation_enabled" not in rec_params.PARAMS_BY_NAME
    assert "source_w_dislike" not in rec_params.PARAMS_BY_NAME
    assert rec_params.PARAMS_BY_NAME["theme_threshold"].group == "graduation"
    cleanup = rec_params.PARAMS_BY_NAME["discovery_gc_days"]
    assert cleanup.group == "discovery" and cleanup.integer is True
    names = {p.name for p in rec_params.PARAMS}
    assert {"comfort_recency_full_days", "comfort_min_plays", "breadth_bias",
            "candidate_pool_factor"} <= names
    assert {p.group for p in rec_params.PARAMS} == {'discovery', 'transient', 'graduation'}
