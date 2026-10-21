"""Serving fingerprints change with policy/model changes, and cached ranks retain their build."""
import numpy as np

from yt_playlist.rec import measurement, rec_params


def test_policy_fingerprint_tracks_preferences_parameters_and_actual_vectors(store):
    baseline, _ = measurement.serving_policy(store, 100)
    assert measurement.serving_policy(store, 100)[0] == baseline
    rec_params.set_param(store, "axis_weight_cap", 2)
    cap_version, config = measurement.serving_policy(store, 100)
    assert cap_version != baseline and config["params"]["axis_weight_cap"] == 2
    store.rec_model.set_weight("genre:jazz", 0, lo=0, now=100)
    mute_version, config = measurement.serving_policy(store, 100)
    assert mute_version != cap_version and config["weights"]["genre:jazz"] == 0
    store.rec_model.replace_rec_vectors([("song|artist", np.array([1., 0.], dtype=np.float32).tobytes())])
    first_model = measurement.serving_policy(store, 100)[0]
    store.rec_model.replace_rec_vectors([("song|artist", np.array([0., 1.], dtype=np.float32).tobytes())])
    assert measurement.serving_policy(store, 100)[0] != first_model


def test_policy_fingerprint_includes_temporary_home_ordering(store):
    baseline = measurement.serving_policy(store, 100)[0]
    store.settings.set_setting("home_mix_vibe", "focus")
    version, config = measurement.serving_policy(store, 100)
    assert version != baseline and config["home"]["vibe"] == "focus"


def test_cached_score_and_build_policy_survive_new_rendering_preferences(store):
    tracks = measurement.stamp_pool(store, [{"key": "song|artist", "score": .42}], 100)
    rec_params.set_param(store, "axis_weight_cap", 2)
    measurement.record_recommendations(store, "home", tracks, 150)
    row = store.conn.execute("SELECT id FROM rec_served_impressions").fetchone()
    trace = store.rec_surface.served_recommendation(row["id"])
    assert trace["score"] == .42
    assert trace["provenance"]["score_available"] is True
    assert trace["model_version"] != tracks[0]["pool_policy"]
    assert trace["configuration"]["params"]["axis_weight_cap"] == 2
    assert trace["pool_configuration"]["params"]["axis_weight_cap"] == 1.4


def test_policy_fingerprint_tracks_playlist_taste_without_vector_rebuild(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.tracks.upsert_track("a", "A", "Artist", None, None)
    b = store.tracks.upsert_track("b", "B", "Artist", None, None)
    pid = store.playlists.upsert_playlist(iid, "PL", "Listening", 1, "h", 0)
    store.rec_model.replace_rec_vectors([(k, np.array(v, dtype=np.float32).tobytes())
                              for k, v in [("a|artist", [1., 0.]), ("b|artist", [0., 1.])]])
    store.playlists.set_playlist_tracks(pid, [a])
    first = measurement.serving_policy(store, 100)[0]
    store.playlists.set_playlist_tracks(pid, [b])
    assert measurement.serving_policy(store, 100)[0] != first
