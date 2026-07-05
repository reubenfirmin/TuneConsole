"""Tests for #88: the SESSION term in _apply_mood (src/yt_playlist/rec/scoring.py).

`layers.session_tilt` is monkeypatched at the scoring module's import site (`scoring.layers`),
since scoring imports the `layers` module (not the bare function), and the term must apply
ALONGSIDE the existing transient collaborative tilt and audio terms, not replace them.
"""
import numpy as np

from yt_playlist.rec import rec_params, scoring


def _silence_other_terms(monkeypatch):
    """Neutralize the mood (collaborative) tilt and the audio tilt so a test can isolate the
    session term's contribution."""
    monkeypatch.setattr(scoring.transient, "centroid_tilt", lambda s, now, V, idx: None)
    monkeypatch.setattr(scoring, "_audio_tilt_boost", lambda s, now, idx, content_vecs=None: None)


def test_session_tilt_raises_targeted_candidate_by_alpha_times_cosine(monkeypatch, store):
    """A session tilt pointing (mostly) at candidate 'x' raises its score, relative to a no-tilt
    run, by exactly session_alpha * cosine(V[x], tilt) (V rows get L2-normalised before the dot,
    same as the existing mood term), and leaves an orthogonal candidate 'y' untouched."""
    _silence_other_terms(monkeypatch)

    # Non-unit rows so normalisation inside _apply_mood actually matters.
    V = np.array([[3.0, 4.0], [0.0, 1.0]], dtype=np.float64)
    idx = {"x": 0, "y": 1}
    tilt = np.array([1.0, 0.0], dtype=np.float64)   # already a unit vector, as session_tilt returns

    monkeypatch.setattr(scoring.layers, "session_tilt", lambda s, now, V, idx: tilt)
    session_alpha = rec_params.get_param(store, "session_alpha")

    base = np.zeros(2)
    with_tilt = scoring._apply_mood(base.copy(), store, 0.0, V, idx)

    monkeypatch.setattr(scoring.layers, "session_tilt", lambda s, now, V, idx: None)
    without_tilt = scoring._apply_mood(base.copy(), store, 0.0, V, idx)

    delta = with_tilt - without_tilt

    Vn = V / np.linalg.norm(V, axis=1, keepdims=True)
    expected_delta_x = session_alpha * float(Vn[0] @ tilt)   # cosine(V[x], tilt) = 3/5 = 0.6
    expected_delta_y = session_alpha * float(Vn[1] @ tilt)   # cosine(V[y], tilt) = 0.0

    assert expected_delta_x == 0.6 * session_alpha
    assert abs(delta[0] - expected_delta_x) < 1e-9
    assert abs(delta[1] - expected_delta_y) < 1e-9
    assert delta[1] == 0.0


def test_none_session_tilt_is_a_no_op(monkeypatch, store):
    """When session_tilt is None (quiet/gated), _apply_mood's output must equal the pre-#88
    function exactly: only the (still-present) mood and audio terms move the score."""
    monkeypatch.setattr(scoring.layers, "session_tilt", lambda s, now, V, idx: None)

    mood_tilt = np.array([0.0, 1.0], dtype=np.float64)
    monkeypatch.setattr(scoring.transient, "centroid_tilt", lambda s, now, V, idx: mood_tilt)
    audio_boost = np.array([0.2, -0.1], dtype=np.float64)
    monkeypatch.setattr(scoring, "_audio_tilt_boost",
                        lambda s, now, idx, content_vecs=None: audio_boost)

    V = np.array([[1.0, 2.0], [2.0, 1.0]], dtype=np.float64)
    idx = {"x": 0, "y": 1}
    base = np.array([0.1, 0.2])

    out = scoring._apply_mood(base.copy(), store, 0.0, V, idx)

    # Hand-computed via the pre-#88 formula: only the mood term and the (pre-existing) audio term.
    Vn = V / np.linalg.norm(V, axis=1, keepdims=True)
    w = rec_params.get_param(store, "audio_transient_w")
    expected = base + scoring.MOOD_ALPHA * (Vn @ mood_tilt) + w * audio_boost

    assert np.allclose(out, expected)


def test_session_term_composes_with_mood_tilt(monkeypatch, store):
    """Both the mood (collaborative) tilt and the session tilt present at once: their deltas add,
    each computed independently against the same L2-normalised V rows."""
    monkeypatch.setattr(scoring, "_audio_tilt_boost", lambda s, now, idx, content_vecs=None: None)

    mood_tilt = np.array([0.0, 1.0], dtype=np.float64)
    session_tilt = np.array([1.0, 0.0], dtype=np.float64)
    monkeypatch.setattr(scoring.transient, "centroid_tilt", lambda s, now, V, idx: mood_tilt)
    monkeypatch.setattr(scoring.layers, "session_tilt", lambda s, now, V, idx: session_tilt)

    V = np.array([[3.0, 4.0], [1.0, 0.0]], dtype=np.float64)
    idx = {"x": 0, "y": 1}
    base = np.zeros(2)

    out = scoring._apply_mood(base.copy(), store, 0.0, V, idx)

    session_alpha = rec_params.get_param(store, "session_alpha")
    Vn = V / np.linalg.norm(V, axis=1, keepdims=True)
    expected = (base
                + scoring.MOOD_ALPHA * (Vn @ mood_tilt)
                + session_alpha * (Vn @ session_tilt))

    assert np.allclose(out, expected)
    # Sanity: both terms actually moved candidate x's score (mood + session both have a
    # nonzero component along x's direction), proving this isn't a degenerate no-op composition.
    assert abs(scoring.MOOD_ALPHA * (Vn[0] @ mood_tilt)) > 0
    assert abs(session_alpha * (Vn[0] @ session_tilt)) > 0
