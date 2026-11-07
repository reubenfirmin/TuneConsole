"""Audio-aware transient tilt (#45): recent listening should be expressible as a direction in the
audio-aware CONTENT vector space, so ranking can lean toward the SOUND (tempo / energy / mood) of what
you have been playing, not only its genre/era/artist facets. This is the producer primitive
(transient.audio_centroid_tilt); the scorer applies it (see the wiring note in the fork report)."""
import numpy as np

from yt_playlist.rec import embedding_vectors as embed, rec_params, scoring, transient
from tests import model_support
from tests.conftest import seed_catalog_track
from tests.repository_mock_support import no_database, transient_store


class _FakeDao:
    def __init__(self, content, audio):
        self._c, self._a = content, audio

    def library_keys(self):
        return set(self._c) | set(self._a)

    def track_content(self):
        return self._c

    def track_audio_features(self):
        return self._a


def _store_with_content(s, monkeypatch, content, audio):
    for key in set(content) | set(audio):
        title, artist = key.split('|', 1)
        seed_catalog_track(s, key, title, artist)
    for method in ("library_keys", "track_content", "track_audio_features"):
        monkeypatch.setattr(s.rec_query, method, getattr(_FakeDao(content, audio), method))
    model_support.build_content_model(s)       # persist audio-aware content vectors
    return s


def test_tilt_is_none_when_no_recent_listening(transient_store):
    s = transient_store
    s.rec_model.get_rec_content_vectors.return_value = [
        ('hi|a', np.array([1., 0.], dtype=np.float32).tobytes())]
    # content vectors exist, but nothing has been played/liked/mooded recently
    assert transient.audio_centroid_tilt(s, now=1000.0) is None


def test_tilt_leans_toward_the_sound_of_recent_plays(file_backed_store, monkeypatch):
    # same genre and era for all three, so ONLY audio can separate them
    content = {k: ("Techno", "2000") for k in ("hi1|a", "hi2|a", "lo|b")}
    audio = {
        "hi1|a": {"energy": 0.95, "bpm": 150.0, "danceability": 0.90},
        "hi2|a": {"energy": 0.92, "bpm": 148.0, "danceability": 0.88},
        "lo|b":  {"energy": 0.08, "bpm": 88.0,  "danceability": 0.20},
    }
    s = _store_with_content(file_backed_store, monkeypatch, content, audio)
    iid = s.accounts.upsert_account("main", "cred", None, True)
    s.history.record_play_event(iid, "hi1|a", None, 1000.0, source_timezone="UTC")

    tilt = transient.audio_centroid_tilt(s, now=1000.0)
    assert tilt is not None
    assert abs(float(np.linalg.norm(tilt)) - 1.0) < 1e-6    # unit direction

    _, CV, cidx = embed.load_content_vectors(s)
    sim_hi = float(CV[cidx["hi2|a"]] @ tilt)               # other high-energy track
    sim_lo = float(CV[cidx["lo|b"]] @ tilt)                # low-energy track
    assert sim_hi > sim_lo, "tilt should sit nearer the high-energy sound just played"

    # This same genuine fitted model owns warm scoring and its disable control.
    # The played track is outside the candidate index, so only audio can steer.
    idx = {'hi2|a': 0, 'lo|b': 1}
    V = np.zeros((2, 4))
    on = scoring._apply_mood(np.zeros(2), s, 1000., V, idx)
    assert on[0] > on[1], 'the warm candidate matching recent sound should rank higher'
    rec_params.set_param(s, 'audio_transient_w', 0.)
    off = scoring._apply_mood(np.zeros(2), s, 1000., V, idx)
    assert on[0] > off[0], 'the audio_transient_w knob must actually add the boost'
    assert off[0] == off[1] == 0., 'weight 0 turns the audio tilt off entirely'


def test_tilt_degrades_gracefully_when_recent_track_has_no_content_vector(transient_store):
    s = transient_store
    s.rec_model.get_rec_content_vectors.return_value = [
        ('hi|a', np.array([1., 0.], dtype=np.float32).tobytes())]
    s.history.recent_plays_with_ts.return_value = [('unknown|x', 1000.)]
    assert transient.audio_centroid_tilt(s, now=1000.0) is None   # no contribution, no error


def test_tilt_is_none_when_content_model_unbuilt(transient_store):
    s = transient_store
    s.history.recent_plays_with_ts.return_value = [('hi|a', 1000.)]
    assert transient.audio_centroid_tilt(s, now=1000.0) is None   # no content vectors at all
