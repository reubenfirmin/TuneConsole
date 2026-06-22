from yt_playlist import rec_params, recommend


def test_transient_constants_present():
    assert rec_params.MOOD_RECENCY_ALPHA == 0.35
    assert rec_params.MOOD_EVENT_CAP == 200
    assert rec_params.STALE_DECAY_HALFLIFE_D == 3
    assert rec_params.PLAY_TRANSIENT_W == 0.30
    assert rec_params.DISLIKE_TRANSIENT_W == 1.50
    assert rec_params.RECENT_PLAY_LIMIT == 50
    assert rec_params.FACET_GAIN == 0.6
    assert rec_params.FACET_MULT_MIN == 0.1 and rec_params.FACET_MULT_MAX == 2.5
    assert rec_params.DISLIKE_SUPPRESS_DAYS == 365
    assert rec_params.THEME_THRESHOLD == 1.2
    assert rec_params.GRADUATE_UP == 1.05 and rec_params.GRADUATE_DOWN == 0.95
    assert rec_params.SYNC_STALE_S == 24 * 3600
    assert recommend.SYNC_STALE_S == rec_params.SYNC_STALE_S      # back-compat re-export
