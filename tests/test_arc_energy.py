"""Tests for the audio-driven 'arc energy' composite + candidate-pool fallback (issue #37)."""
import pytest

from yt_playlist.rec import arc_energy, genre_map, journeys


def _ae(keys, genres, audio):
    return arc_energy.arc_energies(keys, genres, audio)


# --- composite blend math (BPM-led: 0.5*norm_bpm + 0.3*energy + 0.2*danceability) ---

def test_norm_bpm_clamps_to_unit_range():
    assert arc_energy._norm_bpm(60) == pytest.approx(0.0)
    assert arc_energy._norm_bpm(180) == pytest.approx(1.0)
    assert arc_energy._norm_bpm(120) == pytest.approx(0.5)
    assert arc_energy._norm_bpm(40) == pytest.approx(0.0)    # below floor -> clamped
    assert arc_energy._norm_bpm(240) == pytest.approx(1.0)   # above ceiling -> clamped


def test_blend_uses_all_three_features():
    # norm_bpm(120)=0.5, energy=0.8, dance=0.6 -> 0.5*0.5 + 0.3*0.8 + 0.2*0.6 = 0.61
    out = _ae(["k"], {"k": "psytrance"},
              {"k": {"bpm": 120, "energy": 0.8, "danceability": 0.6}})
    assert out["k"] == pytest.approx(0.61)


def test_blend_renormalizes_weights_over_present_features():
    # only bpm + energy present -> (0.5*norm_bpm(60) + 0.3*1.0) / (0.5+0.3) = 0.3/0.8 = 0.375
    out = _ae(["k"], {"k": "psytrance"}, {"k": {"bpm": 60, "energy": 1.0}})
    assert out["k"] == pytest.approx(0.375)


def test_blend_single_feature_passes_through():
    assert _ae(["k"], {"k": "x"}, {"k": {"bpm": 180}})["k"] == pytest.approx(1.0)
    assert _ae(["k"], {"k": "x"}, {"k": {"energy": 0.4}})["k"] == pytest.approx(0.4)
    assert _ae(["k"], {"k": "x"}, {"k": {"danceability": 0.7}})["k"] == pytest.approx(0.7)


def test_non_arc_audio_features_are_ignored():
    # mood/loudness/key etc. must not enter the composite; only bpm/energy/dance count.
    out = _ae(["k"], {"k": "x"},
              {"k": {"energy": 0.5, "mood_happy": 0.9, "loudness": -3.0, "music_key": "C"}})
    assert out["k"] == pytest.approx(0.5)


# --- candidate-pool fallback chain ---

def test_missing_track_falls_back_to_subgenre_average():
    # x has no features; peer y shares the subgenre (psytrance) and is enriched -> x gets y's value.
    out = _ae(["x", "y"], {"x": "psytrance", "y": "psytrance"},
              {"y": {"bpm": 120}})    # norm_bpm(120)=0.5
    assert out["y"] == pytest.approx(0.5)
    assert out["x"] == pytest.approx(0.5)


def test_subgenre_average_means_over_enriched_peers():
    out = _ae(["x", "y", "z"],
              {"x": "psytrance", "y": "psytrance", "z": "psytrance"},
              {"y": {"bpm": 60}, "z": {"bpm": 180}})    # 0.0 and 1.0 -> mean 0.5
    assert out["x"] == pytest.approx(0.5)


def test_missing_track_falls_back_to_family_average_when_no_subgenre_peer():
    # x (psytrance) has no enriched same-subgenre peer, but z (goa trance) shares the trance family.
    out = _ae(["x", "z"], {"x": "psytrance", "z": "goa trance"},
              {"z": {"bpm": 180}})    # 1.0
    assert out["x"] == pytest.approx(1.0)


def test_falls_back_to_curated_family_constant_when_pool_has_no_features():
    # No enriched track anywhere in the pool -> the curated genre_map.energy constant (trance=0.75).
    out = _ae(["x"], {"x": "psytrance"}, {})
    assert out["x"] == pytest.approx(genre_map.energy("psytrance"))
    assert out["x"] == pytest.approx(0.75)


def test_unknown_genre_with_no_features_defaults_to_half():
    out = _ae(["x"], {"x": "totally-made-up-genre"}, {})
    assert out["x"] == pytest.approx(0.5)


def test_enriched_track_uses_its_own_value_not_the_average():
    # y is enriched (1.0) and x is not; y must keep its own raw, not be pulled toward the avg.
    out = _ae(["x", "y"], {"x": "psytrance", "y": "psytrance"}, {"y": {"bpm": 180}})
    assert out["y"] == pytest.approx(1.0)


def test_returns_value_for_every_key():
    keys = ["a", "b", "c"]
    out = _ae(keys, {"a": "psytrance", "b": "goa trance", "c": "techno"}, {"a": {"bpm": 120}})
    assert set(out) == set(keys)
    assert all(0.0 <= v <= 1.0 for v in out.values())


# --- integration: real per-track BPM drives a middle-peaked energy arc ---

def test_energy_arc_peaks_in_middle_with_real_bpm():
    # 12 tracks, ascending real BPM; energy_arc must put the highest-BPM tracks in the interior.
    keys = [f"k{i}" for i in range(12)]
    genres = {k: "psytrance" for k in keys}
    bpms = [60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160, 170]
    audio = {k: {"bpm": b} for k, b in zip(keys, bpms)}
    ae = _ae(keys, genres, audio)

    def feat(k):
        return {"artist": k, "genre": genres[k], "energy": ae[k],
                "decade": None, "plays": 0, "recency": 0.0}

    order = journeys.journey_order(keys, "energy_arc", seed=1, feat=feat)
    third = len(order) // 3
    mid_avg = sum(ae[k] for k in order[third:2 * third]) / third
    end_avg = sum(ae[k] for k in order[:third] + order[2 * third:]) / (len(order) - third)
    assert mid_avg > end_avg
