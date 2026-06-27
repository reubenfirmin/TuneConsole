"""Task 1 (#50): the audio tilt can score against a supplied content-vector source (the cold path)
without changing the warm path (default content_vecs=None -> library content vectors)."""
import numpy as np
import pytest

from yt_playlist.rec import scoring


def test_apply_mood_default_content_vecs_uses_library_loader(monkeypatch, store):
    # Default (content_vecs=None): the audio term loads LIBRARY content vectors, today's behavior.
    monkeypatch.setattr(scoring.transient, "audio_centroid_tilt",
                        lambda s, now: np.array([1.0, 0.0], dtype=np.float64))
    called = {}

    def loader(s):
        called["lib"] = True
        return [], None, {}

    monkeypatch.setattr(scoring.embed, "load_content_vectors", loader)
    V = np.eye(2, dtype=np.float32)
    out = scoring._apply_mood(np.zeros(2), store, 0.0, V, {"a": 0, "b": 1})
    assert called.get("lib") is True            # default path consulted the library loader
    assert np.allclose(out, np.zeros(2))        # CV is None -> audio term is a no-op, scores unchanged


def test_audio_tilt_boost_uses_supplied_content_vecs(monkeypatch, store):
    # A provided (keys, CV, cidx) triple is used verbatim; the library loader must NOT run.
    monkeypatch.setattr(scoring.transient, "audio_centroid_tilt",
                        lambda s, now: np.array([1.0, 0.0], dtype=np.float64))
    monkeypatch.setattr(scoring.embed, "load_content_vectors",
                        lambda s: pytest.fail("library loader must not run for the cold path"))
    CV = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    boost = scoring._audio_tilt_boost(store, 0.0, {"x": 0, "y": 1},
                                      content_vecs=(["x", "y"], CV, {"x": 0, "y": 1}))
    assert boost is not None
    assert boost[0] == 1.0 and boost[1] == 0.0   # cos to the [1,0] tilt
