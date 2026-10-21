import pytest

from yt_playlist.rec import rec_params


def test_source_weights_seeded():
    assert rec_params.SOURCE_W_VIBE == 1.0
    assert rec_params.SOURCE_W_LIKE == 1.0
    assert rec_params.SOURCE_W_DISLIKE == 1.0
    assert rec_params.SOURCE_W_SLIDER == 0.5
    assert rec_params.SOURCE_W_PLAY == 0.08
    assert rec_params.PLAY_GRAD_SESSION_CAP == 0.4


from yt_playlist.core.store import Store
from yt_playlist.rec import graduation, scoring, rec_params
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key


def _store_with_genre(keys_genre):
    s = Store(":memory:")
    s.init_schema()
    for key, (vid, title, artist, genre) in keys_genre.items():
        tid = s.tracks.upsert_track(vid, title, artist, None, None)
        s.tracks.set_track_genre(tid, genre)
    return s


def _store_with_play_history(genre="Techno", n=10, now=1000.0, w_play=0.5):
    """A fresh store whose recent history is n distinct tracks of one genre (a dominant family), with
    source_w_play raised so a handful of daily exposures crosses THEME_THRESHOLD (the 0.08 default
    would need ~18 days, too slow for a unit test). Returns (store, now, axis)."""
    s = Store(":memory:")
    s.init_schema()
    iid = s.identities.upsert_identity("me", "cred", None, True)
    keys = []
    for i in range(n):
        tid = s.tracks.upsert_track(f"v{i}", f"song{i}", "band", None, None)
        s.tracks.set_track_genre(tid, genre)
        keys.append(identity_key(f"song{i}", "band"))
    s.history.add_history_snapshot(iid, now, keys)
    rec_params.set_param(s, "source_w_play", w_play)
    return s, now, f"genre:{genre_map.family(genre)}"


def test_vibe_graduation_unchanged_regression():
    # One key in genre 'Techno'; a strong "a lot" vibe (signed=2) at source 1.0 crosses
    # THEME_THRESHOLD (1.2) in a single event, exactly as today.
    s = _store_with_genre({"song|band": ("v1", "song", "band", "Techno")})
    graduation.graduate_moods(s, ["song|band"], 2.0, 1000.0, source=rec_params.SOURCE_W_VIBE)
    fam = genre_map.family("Techno")
    # #85 GRADUATE_UP itself is > 1.0 (no more flat post-nudge shrink); read at the same `now` as the
    # nudge so time-proportional reversion doesn't erode it before the assertion runs.
    assert s.rec_model.get_weights(now=1000.0)[f"genre:{fam}"] > 1.0   # nudged once, from prior 1.0


def test_play_source_is_weak():
    # A single weak play (source 0.08, signed +1) does NOT cross the threshold.
    s = _store_with_genre({"song|band": ("v1", "song", "band", "Techno")})
    graduation.graduate_moods(s, ["song|band"], 1.0, 1000.0, source=rec_params.SOURCE_W_PLAY)
    fam = genre_map.family("Techno")
    assert f"genre:{fam}" not in s.rec_model.get_weights()   # below threshold -> no permanent nudge yet
    assert abs(s.rec_surface.get_theme(f"genre:{fam}")) == rec_params.SOURCE_W_PLAY   # but ledger accumulated


@pytest.mark.parametrize("legacy_pause", [False, True])
def test_play_exposure_graduates_over_several_days(legacy_pause):
    # Sustained recent listening graduates the permanent weight up via daily exposure (mirrors the
    # held-slider mechanic). Plays feed only the transient; this is the single funnel into permanent.
    s, now, axis = _store_with_play_history("Techno", n=10)
    if legacy_pause:
        s.settings.set_setting("rec_param:graduation_enabled", "0")
    day = 86400
    before = s.rec_model.get_weights(now=now).get(axis, 1.0)
    for d in range(8):
        graduation.graduate_play_exposure(s, now + d * day)
    # #85 read at the last nudge's `now`, else real-wall-clock reversion would erase the graduation
    after = s.rec_model.get_weights(now=now + 7 * day).get(axis, 1.0)
    assert after > before, "sustained daily listening should graduate the permanent weight up"


@pytest.mark.parametrize("source", ["mood", "slider", "play"])
def test_explicit_genre_mute_survives_every_graduation_source(source):
    s, now, axis = _store_with_play_history()
    s.rec_model.set_weight(axis, 0.0, lo=0.0, hi=2.0, now=now)
    s.rec_model.set_lean(axis, 2.0, now)
    for day in range(1, 9):
        later = now + day * 86400
        if source == "mood":
            graduation.graduate_moods(s, ["song0|band"], 2.0, later)
        elif source == "slider":
            graduation.graduate_slider_exposure(s, later)
        else:
            graduation.graduate_play_exposure(s, later)
        assert s.rec_model.get_weights(now=later)[axis] == 0.0
        assert scoring.discovery_facet_weight(s, "techno", later) is None
    # Each funnel actually crossed the threshold; this must not pass just because nothing learned.
    assert any(row["axis"] == axis and row["source"] == source for row in s.rec_surface.graduation_audit_rows())


def test_play_exposure_idempotent_within_a_day():
    # Re-running the same fast sync the same UTC day must NOT re-count (the #46 regression guard).
    s, now, axis = _store_with_play_history("Techno", n=10)
    graduation.graduate_play_exposure(s, now)
    score_after_first = s.rec_surface.get_theme(axis)
    graduation.graduate_play_exposure(s, now)
    graduation.graduate_play_exposure(s, now)
    assert s.rec_surface.get_theme(axis) == score_after_first, "same-day re-runs must not re-count"


def test_play_exposure_stops_without_recent_plays():
    # No recent plays -> play_facet_leans empty -> exposure is a no-op (the funnel's off-switch).
    s = _store_with_genre({"s|band": ("v", "s", "band", "Techno")})
    axis = f"genre:{genre_map.family('Techno')}"
    before = s.rec_model.get_weights().get(axis, 1.0)
    graduation.graduate_play_exposure(s, 1000.0 + 86400)
    assert s.rec_model.get_weights().get(axis, 1.0) == before, "no recent plays -> no graduation"


def test_radio_only_listening_day_graduates_nothing():
    # #93v2: a day of purely radio-queued listening (every play carrying the radio playlist's
    # provenance, plus the provenance-free history_items shadows the next YTM sync writes for those
    # same plays) must accrue NO ledger score and NO permanent weight for the played genre. Without
    # the exclusion, radio's own picks would graduate transient leans into standing taste day after
    # day - the slow-burn arm of the feedback loop.
    s = Store(":memory:")
    s.init_schema()
    iid = s.identities.upsert_identity("me", "cred", None, True)
    s.settings.set_setting("radio_playlist_ytm", "PLRADIO")
    now = 100 * 86400 + 1000.0
    keys = []
    for i in range(10):
        tid = s.tracks.upsert_track(f"v{i}", f"song{i}", "band", None, None)
        s.tracks.set_track_genre(tid, "Techno")
        k = identity_key(f"song{i}", "band")
        keys.append(k)
        s.history.record_play_event(iid, k, f"v{i}", now + i * 4000, playlist_ytm_id="PLRADIO")
    s.history.record_history_plays(iid, now + 50000, keys)             # the same plays, post-sync shadows
    rec_params.set_param(s, "source_w_play", 0.5)              # same fast lane as the positive test
    axis = f"genre:{genre_map.family('Techno')}"
    day = 86400
    before = s.rec_model.get_weights(now=now).get(axis, 1.0)
    for d in range(8):
        graduation.graduate_play_exposure(s, now + d * day)
    assert not s.rec_surface.get_theme(axis), "radio-only plays must not accrue ledger score"   # None = never bumped
    assert s.rec_model.get_weights(now=now + 7 * day).get(axis, 1.0) == before, \
        "radio-only listening must not graduate a permanent weight"


def test_play_graduated_day_roundtrip():
    s = _store_with_genre({"song|band": ("v1", "song", "band", "Techno")})
    assert s.rec_surface.get_play_graduated_day("genre:techno") is None
    s.rec_surface.set_play_graduated_day("genre:techno", "2026-06-25")
    assert s.rec_surface.get_play_graduated_day("genre:techno") == "2026-06-25"
    s.rec_surface.set_play_graduated_day("genre:techno", "2026-06-26")     # upsert, not duplicate
    assert s.rec_surface.get_play_graduated_day("genre:techno") == "2026-06-26"


def test_axis_weights_fold_standing_lean():
    s = _store_with_genre({"s|band": ("v", "s", "band", "Techno")})
    s.rec_model.set_lean("genre:" + genre_map.family("Techno"), 1.5, 1000.0)
    mult = scoring._axis_weights_for(s, ["s|band"], now=1000.0)
    assert mult is not None
    assert mult["s|band"] > 1.0    # the standing lean lifts this track's multiplier
