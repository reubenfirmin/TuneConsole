# tests/test_negative_graduation.py
"""#84 (mirrors #54 on the permanent side): a dislike or negative vibe is a verdict on the tracks
themselves, not on an artist's whole catalogue. graduate_moods must not graduate `artist:` axes on
negative signed events, even though the genre/era ledger keeps accruing normally. Positive signals
(likes, positive vibe taps) still graduate artists as before."""
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
    t1 = store.upsert_track("v1", "Rebel Yell", "Billy Idol", None, None, 1)
    store.set_track_genre(t1, "jazz")
    store.set_track_year(t1, "1983")
    t2 = store.upsert_track("v2", "White Wedding", "Billy Idol", None, None, 1)
    store.set_track_genre(t2, "jazz")
    store.set_track_year(t2, "1982")
    k1 = identity_key("Rebel Yell", "Billy Idol")
    k2 = identity_key("White Wedding", "Billy Idol")
    return k1, k2


def test_two_dislikes_do_not_graduate_artist_but_genre_still_accrues(store):
    """#84: two dislikes of the same artist (2 x source_w_dislike 1.0 == 2.0 > THEME_THRESHOLD 1.2)
    used to permanently down-weight artist:Billy Idol -- the exact leak #54 fixed on the transient
    side. They must not now, while the genre ledger (same math) still crosses and graduates down."""
    k1, k2 = _two_billy_idol_tracks(store)
    graduation.apply_dislikes(store, {k1: "DISLIKE", k2: "DISLIKE"}, now=100.0)

    # The artist axis was never even touched: no ledger entry, no permanent weight change.
    assert store.get_theme("artist:Billy Idol") is None
    w = store.get_weights(now=100.0)
    assert w.get("artist:Billy Idol", 1.0) == 1.0

    # The genre ledger did accrue and cross THEME_THRESHOLD (1.0 + 1.0 = 2.0 > 1.2), graduating down.
    # VERIFY: source_w_dislike=1.0, signed=-1.0/call, presence weight 1/1 each -> -1.0, -1.0 -> -2.0.
    # copysign(1.2, -2.0) == -1.2 discounted -> -2.0 - (-1.2) == -0.8 remainder.
    assert store.get_theme("genre:jazz") == pytest.approx(-0.8)
    assert w.get("genre:jazz", 1.0) < 1.0

    # The graduation log carries no artist rows at all (none were ever logged), and definitely none
    # for a negative (down-graduating) artist crossing.
    rows = store.recent_graduations(limit=50)
    assert not any(r["axis"].startswith("artist:") for r in rows)
    assert any(r["axis"] == "genre:jazz" for r in rows)


def test_positive_graduate_moods_still_graduates_artist(store):
    """Positive signals (likes, positive vibe taps) are unaffected: a signed=+1 mood event that
    crosses THEME_THRESHOLD still graduates the artist axis, same as before #84."""
    tid = store.upsert_track("v1", "S", "Coltrane", None, None, 1)
    store.set_track_genre(tid, "jazz")
    store.set_track_year(tid, "1960")
    k = identity_key("S", "Coltrane")

    graduation.graduate_moods(store, [k], 1, now=1.0)             # +1.0, below 1.2
    assert store.get_weights(now=1.0).get("artist:Coltrane", 1.0) == 1.0
    graduation.graduate_moods(store, [k], 1, now=2.0)             # total 2.0 -> crosses, graduates up

    w = store.get_weights(now=2.0)
    assert w.get("artist:Coltrane", 1.0) > 1.0
    assert store.get_theme("artist:Coltrane") == pytest.approx(0.8)   # 2.0 - 1.2 remainder
    rows = store.recent_graduations(limit=50)
    assert any(r["axis"] == "artist:Coltrane" for r in rows)


def test_negative_vibe_tap_also_skips_artist(store):
    """The fix is sign-based, not source-label-based: a negative *vibe* tap (source_label='mood', not
    'dislike') must also skip the artist axis, exactly like apply_dislikes does."""
    k1, k2 = _two_billy_idol_tracks(store)
    source_w_vibe = rec_params.get_param(store, "source_w_vibe")

    graduation.graduate_moods(store, [k1], -1, now=1.0, source=source_w_vibe, source_label="mood")
    graduation.graduate_moods(store, [k2], -1, now=2.0, source=source_w_vibe, source_label="mood")

    assert store.get_theme("artist:Billy Idol") is None
    assert store.get_weights(now=2.0).get("artist:Billy Idol", 1.0) == 1.0
    # The genre ledger still accrued under the same negative vibe (same math as the dislike case).
    assert store.get_theme("genre:jazz") == pytest.approx(-0.8)
    rows = store.recent_graduations(limit=50)
    assert not any(r["axis"].startswith("artist:") for r in rows)


def test_repeat_apply_dislikes_accrues_once_not_twice(store):
    """The once-not-twice invariant on an ACCRUING axis (the old test_dislike_sync guard asserted it
    on the artist axis, which #84 emptied): re-applying the same dislikes (a re-sync re-capturing
    the same likeStatus) must not double the genre ledger. record_dislike's first-seen dedup is the
    mechanism; this pins it on the ledger side."""
    k1, k2 = _two_billy_idol_tracks(store)
    graduation.apply_dislikes(store, {k1: "DISLIKE", k2: "DISLIKE"}, now=100.0)
    graduation.apply_dislikes(store, {k1: "DISLIKE", k2: "DISLIKE"}, now=200.0)
    assert store.get_theme("genre:jazz") == pytest.approx(-0.8)   # not -2.6: accrued once
