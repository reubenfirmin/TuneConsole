"""Fresh ranks discoveries with evidence; metadata-only fallbacks are explicitly unscored."""
import numpy as np
import pytest

from yt_playlist.rec import surfaces, scoring, discover, rec_params
from yt_playlist.rec.scoring import PlaylistTaste


class _Proj:
    """genre -> a fixed collaborative vector; None genre -> zero vector (no usable features)."""
    def predict(self, genre, year=None, audio=None, artist=None):
        return {"techno": np.array([1.0, 0.0]), "ambient": np.array([0.0, 1.0]),
                None: np.array([0.0, 0.0])}[genre]


def _patch(monkeypatch, proj=None):
    proj = _Proj() if proj is None else proj
    monkeypatch.setattr(surfaces, "playlist_taste",
                        lambda s: PlaylistTaste(["p"], np.array([[1.0, 0.0]]), np.array([1.0])))
    monkeypatch.setattr(discover.ContentProjection, "fit", classmethod(lambda cls, s: proj))
    monkeypatch.setattr(surfaces.embed, "load_discovered_content_vectors", lambda s, **kw: ([], None, {}))
    # #85 staleness_factor is gone; isolate taste-only ranking by zeroing both tilt producers directly.
    monkeypatch.setattr(scoring.transient, "centroid_tilt", lambda s, now, V, idx: None)
    monkeypatch.setattr(scoring.transient, "audio_centroid_tilt", lambda s, now, **kwargs: None)
    monkeypatch.setattr(surfaces, "discovery_facet_weight", lambda s, fam, now: 1.0)


def _row(key, genre, audio=None):
    return {"identity_key": key, "genre": genre, "year": None, "audio": audio or {}, "artist": key[0],
            "video_id": "v" + key, "title": key.upper(), "album": "", "thumbnail": None}


def test_ranks_by_taste_fit(monkeypatch, store):
    pool = [_row("t|a", "techno"), _row("m|b", "ambient")]
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool, raising=False)
    _patch(monkeypatch)
    out = surfaces.cold_candidates(store, 0.0)
    assert out and out[0].key == "t|a"          # techno aligns with the [1,0] taste centroid
    assert out[0].lane == "cold"


@pytest.mark.parametrize("offset", [-0.8, -0.2, 0.4], ids=["negative", "zero", "positive"])
@pytest.mark.parametrize("weight,cap,expected", [
    (2.0, 1.4, ["b|b", "a|a", "c|c", "d|d", "n|n"]),
    (0.5, 1.4, ["a|a", "c|c", "b|b", "d|d", "n|n"]),
    (2.0, 1.0, ["a|a", "b|b", "c|c", "d|d", "n|n"]),
])
def test_genre_controls_keep_their_direction_and_bound(monkeypatch, store, offset, weight, cap, expected):
    fits = {"ambient": offset + 0.3, "techno": offset + 0.2,
            "jazz": offset + 0.1, "folk": offset}

    class Projection:
        def predict(self, genre, year=None, audio=None, artist=None):
            if genre is None:
                return np.zeros(2)
            fit = fits[genre]
            return np.array([fit, np.sqrt(1 - fit ** 2)])

    pool = [_row("n|n", None)] + [_row(key, genre) for key, genre in
            [("a|a", "ambient"), ("b|b", "techno"), ("c|c", "jazz"), ("d|d", "folk")]]
    pool[0]["year"] = "2000"  # eligible fallback, but this projection cannot score its year
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool)
    _patch(monkeypatch, Projection())
    # Exercise stored genre preferences and the real discovery overlay, including the public cap.
    monkeypatch.setattr(surfaces, "discovery_facet_weight", scoring.discovery_facet_weight)
    before = [t.key for t in surfaces.cold_candidates(store, 100.0)]
    assert before == ["a|a", "b|b", "c|c", "d|d", "n|n"]
    store.set_weight("genre:techno", weight, lo=0.0, hi=2.0, now=100.0)
    rec_params.set_param(store, "axis_weight_cap", cap)
    assert [t.key for t in surfaces.cold_candidates(store, 100.0)] == expected


def test_no_metadata_is_not_surfaced(monkeypatch, store):
    pool = [_row("n|c", None), _row("m|b", "ambient"), _row("t|a", "techno")]
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool, raising=False)
    _patch(monkeypatch)
    out = surfaces.cold_candidates(store, 0.0)
    assert [t.key for t in out] == ["t|a", "m|b"]
    assert all(t.score is not None for t in out)


def test_no_projection_keeps_discovery_order(monkeypatch, store):
    pool = [_row("n|c", None), _row("t|a", "techno")]
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool, raising=False)
    _patch(monkeypatch)
    monkeypatch.setattr(discover.ContentProjection, "fit", classmethod(lambda cls, s: None))
    out = surfaces.cold_candidates(store, 0.0)
    assert [t.key for t in out] == ["t|a"]
    assert out[0].score is None


def test_unknown_fit_is_not_mistaken_for_a_better_score(monkeypatch, store):
    pool = [_row("n|c", None), _row("t|a", "techno")]
    pool[0]["year"] = "2000"
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool)
    _patch(monkeypatch)
    monkeypatch.setattr(surfaces, "playlist_taste",
                        lambda s: PlaylistTaste(["p"], np.array([[-1., 0.]]), np.array([1.])))
    assert [t.key for t in surfaces.cold_candidates(store, 0.0)] == ["t|a", "n|c"]


def test_no_taste_model_keeps_discoveries(monkeypatch, store):
    pool = [_row("n|c", None), _row("t|a", "techno")]
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool)
    _patch(monkeypatch)
    monkeypatch.setattr(surfaces, "playlist_taste", lambda s: None)
    assert [t.key for t in surfaces.cold_candidates(store, 0.0)] == ["t|a"]


def test_metadata_fallback_respects_dismissals_and_artist_mutes(monkeypatch, store):
    pool = [_row("dismissed|a", None), _row("muted|b", None), _row("keep|c", None)]
    for row in pool:
        row["year"] = "2000"
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool)
    _patch(monkeypatch)
    store.record_feedback("for_you", "dismissed|a", "dismiss", now=1)
    monkeypatch.setattr(store, "muted_artists", lambda: {"m"})
    assert [t.key for t in surfaces.cold_candidates(store, 2)] == ["keep|c"]


def test_no_projection_still_respects_muted_genres(monkeypatch, store):
    pool = [_row("n|c", None), _row("t|a", "techno")]
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool)
    _patch(monkeypatch)
    monkeypatch.setattr(discover.ContentProjection, "fit", classmethod(lambda cls, s: None))
    monkeypatch.setattr(surfaces, "discovery_facet_weight",
                        lambda s, fam, now: None if fam == "techno" else 1.0)
    assert surfaces.cold_candidates(store, 0.0) == []


def test_muted_family_excluded(monkeypatch, store):
    pool = [_row("t|a", "techno")]
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool, raising=False)
    _patch(monkeypatch)
    monkeypatch.setattr(surfaces, "discovery_facet_weight", lambda s, fam, now: None)  # muted family
    assert surfaces.cold_candidates(store, 0.0) == []


def test_large_pool_evaluates_feedback_once_per_genre_family(monkeypatch, store):
    pool = [_row(f"t{i}|artist{i}", "techno") for i in range(500)] + [_row("m|b", "ambient")]
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool)
    _patch(monkeypatch)
    calls = []
    def weight(store, family, now):
        calls.append(family)
        return None if family == "ambient" else 1.0
    monkeypatch.setattr(surfaces, "discovery_facet_weight", weight)
    out = surfaces.cold_candidates(store, 0.0)
    assert len(out) == 500 and all(t.genre == "techno" for t in out)
    assert calls == ["techno", "ambient"]
