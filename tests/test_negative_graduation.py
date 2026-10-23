# tests/test_negative_graduation.py
"""Track negatives never establish permanent artist, genre, or era preferences."""
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import graduation, rec_params
from yt_playlist.util.matching import identity_key


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _two_billy_idol_tracks(store):
    """Two distinct tracks by the same artist, same genre family and decade, so the genre/era ledger
    accrues identically to the (buggy) artist ledger the two dislikes used to cross."""
    # "jazz" is used (rather than a real Billy Idol genre) because genre_map.family("jazz") ==
    # genre_map.subgenre("jazz") == "jazz", so it yields exactly one genre facet (no split across a
    # family + subgenre pair) -- keeps the threshold math single-axis and easy to hand-verify.
    t1 = store.tracks.upsert_track("v1", "Rebel Yell", "Billy Idol", None, None, 1)
    store.tracks.set_track_genre(t1, "jazz")
    store.tracks.set_track_year(t1, "1983")
    t2 = store.tracks.upsert_track("v2", "White Wedding", "Billy Idol", None, None, 1)
    store.tracks.set_track_genre(t2, "jazz")
    store.tracks.set_track_year(t2, "1982")
    k1 = identity_key("Rebel Yell", "Billy Idol")
    k2 = identity_key("White Wedding", "Billy Idol")
    return k1, k2


def test_two_dislikes_do_not_graduate_artist_genre_or_era(store):
    k1, k2 = _two_billy_idol_tracks(store)
    graduation.apply_dislikes(store, {k1: "DISLIKE", k2: "DISLIKE"}, now=100.0)

    w = store.rec_model.get_weights(now=100.0)
    for axis in ("artist:Billy Idol", "genre:jazz", "era:1980"):
        assert store.rec_surface.get_theme(axis) is None
        assert w.get(axis, 1.0) == 1.0
    assert store.rec_surface.recent_graduations(limit=50) == []
    assert store.rec_model.disliked_identity_keys() == {k1, k2}


def test_positive_graduate_moods_still_graduates_artist(store):
    """Positive signals (likes, positive vibe taps) are unaffected: a signed=+1 mood event that
    crosses THEME_THRESHOLD still graduates the artist axis, same as before #84."""
    tid = store.tracks.upsert_track("v1", "S", "Coltrane", None, None, 1)
    store.tracks.set_track_genre(tid, "jazz")
    store.tracks.set_track_year(tid, "1960")
    k = identity_key("S", "Coltrane")

    graduation.graduate_moods(store, [k], 1, now=1.0)             # +1.0, below 1.2
    assert store.rec_model.get_weights(now=1.0).get("artist:Coltrane", 1.0) == 1.0
    graduation.graduate_moods(store, [k], 1, now=2.0)             # total 2.0 -> crosses, graduates up

    w = store.rec_model.get_weights(now=2.0)
    assert w.get("artist:Coltrane", 1.0) > 1.0
    assert store.rec_surface.get_theme("artist:Coltrane") == pytest.approx(0.8)   # 2.0 - 1.2 remainder
    rows = store.rec_surface.recent_graduations(limit=50)
    assert any(r["axis"] == "artist:Coltrane" for r in rows)


def test_negative_vibe_tap_also_leaves_permanent_facets_alone(store):
    k1, k2 = _two_billy_idol_tracks(store)
    source_w_vibe = rec_params.get_param(store, "source_w_vibe")

    graduation.graduate_moods(store, [k1], -1, now=1.0, source=source_w_vibe, source_label="mood")
    graduation.graduate_moods(store, [k2], -1, now=2.0, source=source_w_vibe, source_label="mood")

    for axis in ("artist:Billy Idol", "genre:jazz", "era:1980"):
        assert store.rec_surface.get_theme(axis) is None
        assert store.rec_model.get_weights(now=2.0).get(axis, 1.0) == 1.0
    assert store.rec_surface.recent_graduations(limit=50) == []


def test_repeat_dislikes_preserve_original_evidence_time(store):
    k1, k2 = _two_billy_idol_tracks(store)
    graduation.apply_dislikes(store, {k1: "DISLIKE", k2: "DISLIKE"}, now=100.0)
    graduation.apply_dislikes(store, {k1: "DISLIKE", k2: "DISLIKE"}, now=200.0)
    assert set(store.rec_model.disliked_with_ts()) == {(k1, 100.0), (k2, 100.0)}
    assert store.rec_surface.get_theme("genre:jazz") is None


def test_deliberate_negative_facet_steering_still_graduates(store):
    graduation.graduate_facet(store, "genre:jazz", -2, now=100, source_label="slider")
    assert store.rec_model.get_weights(now=100)["genre:jazz"] < 1
