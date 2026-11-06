"""#85 transient decay is wall-clock: an old event fades by age even with nothing newer,
and 50 rapid plays cannot rotate an event out of the window by rank."""
from tests.repository_mock_support import no_database, transient_store
from yt_playlist.rec import transient

DAY = 86400.0


def test_month_old_mood_tap_is_nearly_gone(transient_store):
    s = transient_store
    s.rec_query.track_genres.return_value = {'song|artist': 'techno'}
    now = 1000 * DAY
    s.rec_signals.recent_mood_events.return_value = [(now - 30 * DAY, +1, ['song|artist'])]
    leans = transient.facet_leans(s, now)
    val = leans.get("genre:techno", 0.0)
    assert 0 <= val < 0.06                                   # 30d at 7d half-life ~ 0.05
    fresh = transient.facet_leans(s, now - 30 * DAY + 60)    # same event when it was a minute old
    fval = fresh.get("genre:techno", 0.0)
    assert fval > 0.9


def test_rapid_plays_do_not_erase_a_recent_like(transient_store):
    # The old rank decay let 50 quick plays rotate a like to rank 50 (~0 weight). Wall-clock:
    # a like from an hour ago keeps ~full weight no matter how many plays follow it.
    s = transient_store
    now = 1000 * DAY
    s.rec_model.recent_liked_with_ts.return_value = [('loved|artist', now - 3600)]
    s.history.recent_plays_with_ts.return_value = [
        (f'p{i}|other', now - 1800 + i * 30) for i in range(50)]
    s.rec_query.track_genres.return_value = {
        'loved|artist': 'jazz', **{f'p{i}|other': 'techno' for i in range(50)}}
    leans = transient.facet_leans(s, now)
    jazz = leans.get("genre:jazz", 0.0)
    assert jazz > 0.4 * float(__import__("yt_playlist.rec.rec_params", fromlist=["x"]).get_param(s, "like_transient_w")) or jazz > 0.4


def test_staleness_factor_is_gone():
    assert not hasattr(transient, "staleness_factor")


def test_quiet_model_still_returns_empty_and_none(transient_store):
    s = transient_store
    assert transient.facet_leans(s, 1000.0) == {}
    assert transient.play_facet_leans(s, 1000.0) == {}
