import numpy as np
from yt_playlist.rec import embed


class FakeDao:
    def __init__(self, content, audio=None):
        self._c = content
        self._a = audio or {}

    def track_content(self):
        return self._c

    def track_audio_features(self):
        return self._a


def test_content_features_tokens(monkeypatch):
    # techno → family techno; 1995 → decade 1990
    content = {"s|a": ("Techno", "1995"), "t|b": ("Deep House", "2001"), "u|c": ("Techno", None)}
    monkeypatch.setattr(embed, "RecDao", lambda store: FakeDao(content))
    feats, rows = embed.content_features(content)
    assert "fam:techno" in feats          # genre-family token present
    assert any(t.startswith("dec:1990") for t in feats)
    rowmap = dict(rows)
    assert rowmap["u|c"]                   # techno-only track still has the family feature
    # the two techno tracks share their family column
    fam_col = feats["fam:techno"]
    assert fam_col in rowmap["s|a"] and fam_col in rowmap["u|c"]


def test_build_content_vectors_normalized_and_filtered(tmp_path, monkeypatch):
    from yt_playlist.core.store import Store
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    content = {"s|a": ("Techno", "1995"), "t|b": ("Techno", "1990")}
    monkeypatch.setattr(embed, "RecDao", lambda store: FakeDao(content))
    keys, V = embed.build_content_vectors(s)
    assert set(keys) == {"s|a", "t|b"}
    norms = np.linalg.norm(V, axis=1)
    assert np.allclose(norms, 1.0)         # L2-normalized
    # same family + same decade, no audio ⇒ identical vectors ⇒ cosine 1.0
    assert float(V[0] @ V[1]) > 0.99


def test_build_content_vectors_excludes_untagged(tmp_path, monkeypatch):
    from yt_playlist.core.store import Store
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    monkeypatch.setattr(embed, "RecDao", lambda store: FakeDao({}))   # no genre, no audio anywhere
    keys, V = embed.build_content_vectors(s)
    assert keys == [] and V.shape[0] == 0


def test_audio_features_sharpen_within_genre(tmp_path, monkeypatch):
    """Same genre, but audio (energy/bpm) separates: a high-energy track should be MORE similar to
    another high-energy track than to a low-energy one — even though all three share the genre."""
    from yt_playlist.core.store import Store
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    content = {k: ("Techno", "2000") for k in ("hi1", "hi2", "lo")}
    audio = {
        "hi1": {"energy": 0.95, "bpm": 150.0, "danceability": 0.9},
        "hi2": {"energy": 0.92, "bpm": 148.0, "danceability": 0.88},
        "lo":  {"energy": 0.10, "bpm": 90.0,  "danceability": 0.2},
    }
    monkeypatch.setattr(embed, "RecDao", lambda store: FakeDao(content, audio))
    keys, V = embed.build_content_vectors(s)
    idx = {k: i for i, k in enumerate(keys)}
    sim_hi = float(V[idx["hi1"]] @ V[idx["hi2"]])
    sim_cross = float(V[idx["hi1"]] @ V[idx["lo"]])
    assert sim_hi > sim_cross               # audio pulls the two high-energy tracks together


def test_track_with_audio_but_no_genre_is_included(tmp_path, monkeypatch):
    """A track with only audio (no genre) still gets a content vector from its audio block."""
    from yt_playlist.core.store import Store
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    content = {"g1": ("Techno", "2000"), "g2": ("Techno", "2000")}
    audio = {"g1": {"energy": 0.9, "bpm": 150.0}, "g2": {"energy": 0.85, "bpm": 145.0},
             "x":  {"energy": 0.88, "bpm": 148.0}}     # x has audio but no genre
    monkeypatch.setattr(embed, "RecDao", lambda store: FakeDao(content, audio))
    keys, V = embed.build_content_vectors(s)
    assert "x" in keys
    assert np.isclose(np.linalg.norm(V[keys.index("x")]), 1.0)


def test_music_scale_is_a_categorical_feature(tmp_path, monkeypatch):
    """Two same-genre tracks in the same musical scale are pulled closer than opposite scales."""
    from yt_playlist.core.store import Store
    s = Store(str(tmp_path / "t.db")); s.init_schema()
    content = {k: ("Techno", "2000") for k in ("m1", "m2", "maj")}
    audio = {"m1": {"music_scale": "minor"}, "m2": {"music_scale": "minor"},
             "maj": {"music_scale": "major"}}
    monkeypatch.setattr(embed, "RecDao", lambda store: FakeDao(content, audio))
    keys, V = embed.build_content_vectors(s)
    idx = {k: i for i, k in enumerate(keys)}
    assert float(V[idx["m1"]] @ V[idx["m2"]]) > float(V[idx["m1"]] @ V[idx["maj"]])
