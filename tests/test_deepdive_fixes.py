"""Regression tests for the deep-dive review fixes."""
import pytest

from yt_playlist.util.matching import identity_key, normalize


# --- #1 normalize: non-Latin no longer collapses to "" / identity_key "|" ---
def test_normalize_keeps_latin_behavior():
    assert normalize("café") == "cafe"
    assert normalize("Time Zero (Paul Ritch Remix)") == "time zero"


def test_normalize_non_latin_stays_distinct():
    assert normalize("Песня") and normalize("歌曲")            # not empty
    assert identity_key("Песня", "Кино") != identity_key("歌曲", "歌手")
    assert identity_key("Песня", "Кино") != "|"


# --- Z set_weight is clamped like nudge_weight ---
def test_set_weight_clamps(store):
    import pytest
    store.rec_model.set_weight("lane:deep_cut", 0.0, now=1000.0)
    assert store.rec_model.get_weights(now=1000.0)["lane:deep_cut"] == pytest.approx(0.2)          # floored, not 0 (would disable the lane)
    store.rec_model.set_weight("lane:neighbourhood", 99.0, now=1000.0)
    assert store.rec_model.get_weights(now=1000.0)["lane:neighbourhood"] == pytest.approx(3.0)     # capped
