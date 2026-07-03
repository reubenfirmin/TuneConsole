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


# --- X recent-mood: ordered-by-recency keys ---
def test_recent_keys_ordered_returns_latest_first(store):
    iid = store.upsert_identity("me", "c", None, True)
    store.history.add_history_snapshot(iid, 1000.0, ["old1", "old2"])
    store.history.add_history_snapshot(iid, 2000.0, ["new1"])
    assert store.history.recent_keys_ordered(0, limit=2) == ["new1"] or \
           store.history.recent_keys_ordered(0)[0] == "new1"     # newest snapshot first
    assert store.history.recent_keys_ordered(1500.0) == ["new1"]  # window cutoff respected


# --- Z set_weight is clamped like nudge_weight ---
def test_set_weight_clamps(store):
    import pytest
    store.rec.set_weight("lane:deep_cut", 0.0, now=1000.0)
    assert store.get_weights(now=1000.0)["lane:deep_cut"] == pytest.approx(0.2)          # floored, not 0 (would disable the lane)
    store.rec.set_weight("lane:explore", 99.0, now=1000.0)
    assert store.get_weights(now=1000.0)["lane:explore"] == pytest.approx(3.0)           # capped
