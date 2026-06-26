"""Audio-aware transient tilt (#45): recent listening should be expressible as a direction in the
audio-aware CONTENT vector space, so ranking can lean toward the SOUND (tempo / energy / mood) of what
you have been playing, not only its genre/era/artist facets. This is the producer primitive
(transient.audio_centroid_tilt); the scorer applies it (see the wiring note in the fork report)."""
import numpy as np

from yt_playlist.rec import embed, rec_params, scoring, transient


class _FakeDao:
    def __init__(self, content, audio):
        self._c, self._a = content, audio

    def track_content(self):
        return self._c

    def track_audio_features(self):
        return self._a


def _store_with_content(tmp_path, monkeypatch, content, audio):
    from yt_playlist.core.store import Store
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    monkeypatch.setattr(embed, "RecDao", lambda store: _FakeDao(content, audio))
    embed.build_content_and_store(s)            # persist audio-aware content vectors
    return s


def test_tilt_is_none_when_no_recent_listening(tmp_path, monkeypatch):
    content = {"hi|a": ("Techno", "2000")}
    audio = {"hi|a": {"energy": 0.9, "bpm": 150.0}}
    s = _store_with_content(tmp_path, monkeypatch, content, audio)
    # content vectors exist, but nothing has been played/liked/mooded recently
    assert transient.audio_centroid_tilt(s, now=1000.0) is None


def test_tilt_leans_toward_the_sound_of_recent_plays(tmp_path, monkeypatch):
    # same genre and era for all three, so ONLY audio can separate them
    content = {k: ("Techno", "2000") for k in ("hi1|a", "hi2|a", "lo|b")}
    audio = {
        "hi1|a": {"energy": 0.95, "bpm": 150.0, "danceability": 0.90},
        "hi2|a": {"energy": 0.92, "bpm": 148.0, "danceability": 0.88},
        "lo|b":  {"energy": 0.08, "bpm": 88.0,  "danceability": 0.20},
    }
    s = _store_with_content(tmp_path, monkeypatch, content, audio)
    iid = s.upsert_identity("main", "cred", None, True)
    s.add_history_snapshot(iid, 1000.0, ["hi1|a"])      # recently played a high-energy track

    tilt = transient.audio_centroid_tilt(s, now=1000.0)
    assert tilt is not None
    assert abs(float(np.linalg.norm(tilt)) - 1.0) < 1e-6    # unit direction

    _, CV, cidx = embed.load_content_vectors(s)
    sim_hi = float(CV[cidx["hi2|a"]] @ tilt)               # other high-energy track
    sim_lo = float(CV[cidx["lo|b"]] @ tilt)                # low-energy track
    assert sim_hi > sim_lo, "tilt should sit nearer the high-energy sound just played"


def test_tilt_degrades_gracefully_when_recent_track_has_no_content_vector(tmp_path, monkeypatch):
    content = {"hi|a": ("Techno", "2000")}
    audio = {"hi|a": {"energy": 0.9}}
    s = _store_with_content(tmp_path, monkeypatch, content, audio)
    iid = s.upsert_identity("main", "cred", None, True)
    s.add_history_snapshot(iid, 1000.0, ["unknown|x"])     # no content vector for this key
    assert transient.audio_centroid_tilt(s, now=1000.0) is None   # no contribution, no error


def test_tilt_is_none_when_content_model_unbuilt(tmp_path):
    from yt_playlist.core.store import Store
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    iid = s.upsert_identity("main", "cred", None, True)
    s.add_history_snapshot(iid, 1000.0, ["hi|a"])
    assert transient.audio_centroid_tilt(s, now=1000.0) is None   # no content vectors at all


# --- the scorer wiring (#45): _apply_mood folds the audio tilt into per-track scores -----------------
def _store_three_tracks_with_recent_play(tmp_path, monkeypatch):
    # same genre and era for all three so ONLY audio separates them; a high-energy track played recently.
    content = {k: ("Techno", "2000") for k in ("hi1|a", "hi2|a", "lo|b")}
    audio = {
        "hi1|a": {"energy": 0.95, "bpm": 150.0, "danceability": 0.90},
        "hi2|a": {"energy": 0.92, "bpm": 148.0, "danceability": 0.88},
        "lo|b":  {"energy": 0.08, "bpm": 88.0,  "danceability": 0.20},
    }
    s = _store_with_content(tmp_path, monkeypatch, content, audio)
    iid = s.upsert_identity("main", "cred", None, True)
    s.add_history_snapshot(iid, 1000.0, ["hi1|a"])              # recent high-energy play
    return s


def test_apply_mood_boosts_warm_candidate_matching_recent_sound(tmp_path, monkeypatch):
    s = _store_three_tracks_with_recent_play(tmp_path, monkeypatch)
    idx = {"hi2|a": 0, "lo|b": 1}            # warm candidates; the played track itself is not a candidate
    V = np.zeros((2, 4))                     # collaborative space; centroid_tilt is None (hi1 not in idx)
    out = scoring._apply_mood(np.zeros(2), s, 1000.0, V, idx)
    assert out[0] > out[1], "the warm candidate matching the recently-played sound should rank higher"


def test_audio_transient_w_zero_disables_boost(tmp_path, monkeypatch):
    s = _store_three_tracks_with_recent_play(tmp_path, monkeypatch)
    idx = {"hi2|a": 0, "lo|b": 1}
    V = np.zeros((2, 4))
    on = scoring._apply_mood(np.zeros(2), s, 1000.0, V, idx)        # default weight steers
    rec_params.set_param(s, "audio_transient_w", 0.0)
    off = scoring._apply_mood(np.zeros(2), s, 1000.0, V, idx)
    assert on[0] > off[0], "the audio_transient_w knob must actually add the boost"
    assert off[0] == off[1] == 0.0, "weight 0 turns the audio tilt off entirely"
