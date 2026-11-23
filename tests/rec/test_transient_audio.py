"""Recent plays can steer ranking toward similar recordings in the content space."""
import numpy as np

from yt_playlist.rec import embedding_vectors as embed, musical_profiles, rec_params, scoring, transient
from tests import model_support
from tests.conftest import seed_catalog_track
from tests.repository_mock_support import no_database, transient_store


def test_tilt_is_none_when_no_recent_listening(transient_store):
    s = transient_store
    s.rec_model.get_rec_content_vectors.return_value = [
        ('hi|a', np.array([1., 0.], dtype=np.float32).tobytes())]
    # content vectors exist, but nothing has been played/liked/mooded recently
    assert transient.audio_centroid_tilt(s, now=1000.0) is None


def test_tilt_leans_toward_the_sound_of_recent_plays(file_backed_store):
    s = file_backed_store
    measurements = {
        "hi1|a": {"intensity": 0.95, "bpm": 150.0, "danceability": 0.90},
        "hi2|a": {"intensity": 0.92, "bpm": 148.0, "danceability": 0.88},
        "lo|b": {"intensity": 0.08, "bpm": 88.0, "danceability": 0.20},
    }
    rows = []
    for key, values in measurements.items():
        title, artist = key.split('|', 1)
        seed_catalog_track(s, key, title, artist, video_id=title, genre='Techno', mb_year='2000')
        profile = musical_profiles.resolve([], overrides=values)
        rows.append((key, musical_profiles.canonical(profile), profile['version'],
                     musical_profiles.digest(profile)))
    s.musical_profiles.publish_recordings(rows, now=1000.0)
    assert s.rec_query.track_content() == dict.fromkeys(measurements, ('Techno', '2000'))
    assert model_support.build_content_model(s) == len(measurements)
    iid = s.accounts.upsert_account("main", None, True)
    s.history.record_play_event(iid, "hi1|a", "hi1", 1000.0, source_timezone="UTC")

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
    s.history.play_events_since.return_value = [{'identity_key': 'unknown|x', 'played_at': 1000.}]
    assert transient.audio_centroid_tilt(s, now=1000.0) is None   # no contribution, no error


def test_tilt_is_none_when_content_model_unbuilt(transient_store):
    s = transient_store
    s.history.play_events_since.return_value = [{'identity_key': 'hi|a', 'played_at': 1000.}]
    assert transient.audio_centroid_tilt(s, now=1000.0) is None   # no content vectors at all
