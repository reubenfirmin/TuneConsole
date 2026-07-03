"""#84 skips enter the transient model: facet_leans / centroid_tilt / audio_centroid_tilt get a
NEGATIVE, wall-clock-decayed push from recent skips (recent_skips_with_ts), mirroring the dislike
channel but even weaker on the artist axis (drop_artist=True: a skip could be mood or context, not
distaste for the artist). play_facet_leans is unaffected: it feeds play graduation specifically,
and a skip must never graduate as a play.
"""
import numpy as np

from yt_playlist.core.store import Store
from yt_playlist.rec import transient

DAY = 86400.0


def _store():
    s = Store(":memory:")
    s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    return s


def _track(s, key, genre="techno", duration=200.0):
    """Registers a track under identity_key `key` ("title|artist") and gives it a genre so it
    carries a genre: facet. duration=200 keeps skip-shaped events (position<=120s, ratio<=0.30)
    easy to construct: position=20 -> ratio 0.1, well inside the skip band and above the 3s bounce
    floor."""
    title, artist = key.split("|")
    t = s.upsert_track(f"v_{key}", title, artist, "", duration)
    s.set_track_genre(t, genre)
    return t


def _skip(s, key, ts, position=20.0, duration=200.0):
    """A track_exit row shaped as a skip (verified by hand against classify_exit: ratio=20/200=0.1
    <= 0.30, position=20 <= 120, position=20 >= 3 (not a bounce) -> "skip")."""
    s.record_player_event(1, "track_exit", f"v_{key}", position, duration, None, None, ts)


def test_fresh_skip_pushes_genre_lean_negative_artist_untouched():
    s = _store()
    _track(s, "song|artist", genre="techno")
    now = 1000 * DAY
    _skip(s, "song|artist", now)                       # fresh: full weight
    leans = transient.facet_leans(s, now)
    assert leans.get("genre:techno", 0.0) < 0
    # #54/#84: a skip is an even weaker verdict on the artist than a dislike, so drop_artist=True
    # keeps the artist axis untouched entirely (not just less negative: exactly absent/0).
    assert leans.get("artist:artist", 0.0) == 0.0


def test_old_skip_beyond_lookback_contributes_nothing():
    s = _store()
    _track(s, "song|artist", genre="techno")
    now = 1000 * DAY
    # default skip_halflife_d=14 -> lookback = 4*14 = 56 days. 90 days is well past the lookback
    # window, so recent_skips_with_ts never even surfaces the row: contribution is exactly 0.
    _skip(s, "song|artist", now - 90 * DAY)
    leans = transient.facet_leans(s, now)
    assert leans.get("genre:techno", 0.0) == 0.0


def test_recent_skip_decays_toward_zero_with_age():
    s = _store()
    _track(s, "fresh|artist", genre="jazz")
    _track(s, "aging|artist", genre="jazz")
    now = 1000 * DAY
    _skip(s, "fresh|artist", now)
    _skip(s, "aging|artist", now - 13 * DAY)            # within lookback, near the half-life
    leans_fresh_only = {}
    s2 = _store()
    _track(s2, "fresh|artist", genre="jazz")
    _skip(s2, "fresh|artist", now)
    leans_fresh_only = transient.facet_leans(s2, now)
    leans_both = transient.facet_leans(s, now)
    # both contribute to the same genre:jazz facet; the aging skip's magnitude is much smaller than
    # the fresh one's, so the combined (more negative) lean is still closer to the fresh-only lean
    # than to double it.
    fresh_val = leans_fresh_only["genre:jazz"]
    both_val = leans_both["genre:jazz"]
    assert both_val < fresh_val                        # aging skip adds some further negative push
    assert both_val > 2 * fresh_val                     # but nowhere near doubling it


def test_centroid_tilt_cosine_to_skipped_track_decreases():
    # Base positive signal (a play) toward [0, 1] so centroid_tilt is defined either way; the
    # skipped track's own direction is [1, 0].
    V = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float64)
    idx = {"skip|artist": 0, "base|artist": 1}
    skip_dir = V[0] / np.linalg.norm(V[0])

    s_no_skip = _store()
    _track(s_no_skip, "base|artist", genre="jazz")
    iid = s_no_skip.upsert_identity("main2", "bridge2", None, True)
    now = 1000 * DAY
    s_no_skip.add_history_snapshot(iid, now, ["base|artist"])
    tilt_no_skip = transient.centroid_tilt(s_no_skip, now, V, idx)
    assert tilt_no_skip is not None
    cos_no_skip = float(np.dot(tilt_no_skip, skip_dir))

    s_skip = _store()
    _track(s_skip, "base|artist", genre="jazz")
    _track(s_skip, "skip|artist", genre="techno")
    iid2 = s_skip.upsert_identity("main2", "bridge2", None, True)
    s_skip.add_history_snapshot(iid2, now, ["base|artist"])
    _skip(s_skip, "skip|artist", now)
    tilt_with_skip = transient.centroid_tilt(s_skip, now, V, idx)
    assert tilt_with_skip is not None
    cos_with_skip = float(np.dot(tilt_with_skip, skip_dir))

    assert cos_with_skip < cos_no_skip                  # the skip pulls the tilt away from its own direction


def test_centroid_tilt_skip_only_is_not_quiet():
    # A skip alone (no mood/play/like) must still produce a defined (non-None) tilt: the quiet
    # gate has to know about skips too, or a skip-only session would silently vanish.
    s = _store()
    _track(s, "skip|artist", genre="techno")
    now = 1000 * DAY
    _skip(s, "skip|artist", now)
    V = np.array([[1.0, 0.0]], dtype=np.float64)
    idx = {"skip|artist": 0}
    tilt = transient.centroid_tilt(s, now, V, idx)
    assert tilt is not None
    assert tilt[0] < 0                                   # pushed away from the skipped track's own direction


def test_audio_centroid_tilt_cosine_to_skipped_track_decreases(monkeypatch):
    from yt_playlist.rec import embed

    CV = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float64)
    cidx = {"skip|artist": 0, "base|artist": 1}
    skip_dir = CV[0] / np.linalg.norm(CV[0])

    monkeypatch.setattr(embed, "load_content_vectors", lambda store: (list(cidx), CV, cidx))

    now = 1000 * DAY

    s_no_skip = _store()
    _track(s_no_skip, "base|artist", genre="jazz")
    iid = s_no_skip.upsert_identity("main2", "bridge2", None, True)
    s_no_skip.add_history_snapshot(iid, now, ["base|artist"])
    tilt_no_skip = transient.audio_centroid_tilt(s_no_skip, now)
    assert tilt_no_skip is not None
    cos_no_skip = float(np.dot(tilt_no_skip, skip_dir))

    s_skip = _store()
    _track(s_skip, "base|artist", genre="jazz")
    _track(s_skip, "skip|artist", genre="techno")
    iid2 = s_skip.upsert_identity("main2", "bridge2", None, True)
    s_skip.add_history_snapshot(iid2, now, ["base|artist"])
    _skip(s_skip, "skip|artist", now)
    tilt_with_skip = transient.audio_centroid_tilt(s_skip, now)
    assert tilt_with_skip is not None
    cos_with_skip = float(np.dot(tilt_with_skip, skip_dir))

    assert cos_with_skip < cos_no_skip


def test_play_facet_leans_unaffected_by_skips():
    s_no_skip = _store()
    _track(s_no_skip, "played|artist", genre="jazz")
    iid = s_no_skip.upsert_identity("main2", "bridge2", None, True)
    now = 1000 * DAY
    s_no_skip.add_history_snapshot(iid, now, ["played|artist"])
    leans_no_skip = transient.play_facet_leans(s_no_skip, now)

    s_skip = _store()
    _track(s_skip, "played|artist", genre="jazz")
    _track(s_skip, "skipped|artist", genre="techno")
    iid2 = s_skip.upsert_identity("main2", "bridge2", None, True)
    s_skip.add_history_snapshot(iid2, now, ["played|artist"])
    _skip(s_skip, "skipped|artist", now)                # a fresh skip, same store otherwise
    leans_with_skip = transient.play_facet_leans(s_skip, now)

    # play_facet_leans feeds play graduation specifically: a skip must never graduate as a play.
    assert leans_with_skip == leans_no_skip
