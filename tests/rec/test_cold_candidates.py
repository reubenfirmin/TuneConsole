"""Task 4 (#50): surfaces.cold_candidates ranks the discovered pool by taste (+ tilts), degrades
gracefully (no audio -> still ranked on genre/era; no metadata -> dropped; no projection -> empty),
and excludes a muted genre family."""
import numpy as np

from yt_playlist.rec import surfaces, scoring, discover
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
    monkeypatch.setattr(surfaces.embed, "load_discovered_content_vectors", lambda s: ([], None, {}))
    monkeypatch.setattr(scoring.transient, "staleness_factor", lambda s, now: 0.0)  # taste only, no tilt
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


def test_no_metadata_dropped(monkeypatch, store):
    pool = [_row("n|c", None)]                   # projection returns the zero vector -> dropped
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool, raising=False)
    _patch(monkeypatch)
    assert surfaces.cold_candidates(store, 0.0) == []


def test_no_projection_returns_empty(monkeypatch, store):
    pool = [_row("t|a", "techno")]
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool, raising=False)
    _patch(monkeypatch)
    monkeypatch.setattr(discover.ContentProjection, "fit", classmethod(lambda cls, s: None))
    assert surfaces.cold_candidates(store, 0.0) == []


def test_muted_family_excluded(monkeypatch, store):
    pool = [_row("t|a", "techno")]
    monkeypatch.setattr(store, "get_discovered_tracks", lambda: pool, raising=False)
    _patch(monkeypatch)
    monkeypatch.setattr(surfaces, "discovery_facet_weight", lambda s, fam, now: None)  # muted family
    assert surfaces.cold_candidates(store, 0.0) == []
