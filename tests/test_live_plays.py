"""#75 live play ingestion: a {type:'play'} bridge frame becomes a play_events row, feeds the
(track, day) history model, stamps plays-freshness, and folds likeStatus into likes/dislikes."""
from yt_playlist.core.store import Store
from yt_playlist.library import live_plays
from yt_playlist.util.matching import identity_key
import pytest


def _store(with_brand=False):
    s = Store(":memory:"); s.init_schema()
    s.identities.upsert_identity("main", "bridge", None, True)
    if with_brand:
        s.identities.upsert_identity("band", "bridge", "10345", False)
    return s


def _ctx(s):
    return type("C", (), {"store": s})()


def _msg(**over):
    m = {"type": "play", "title": "Song", "artist": "Artist", "thumbnail": "",
         "likeStatus": "INDIFFERENT", "videoId": "v1", "playlist": "", "brandId": ""}
    m.update(over)
    return m


def test_play_persists_event_day_model_and_freshness():
    s = _store()
    assert live_plays.handle_play_event(_ctx(s), _msg(), 1000.0) is True
    key = identity_key("Song", "Artist")
    evs = s.history.play_events_since(0)
    assert [e["identity_key"] for e in evs] == [key]
    # the (track, day) model saw the play too (bare key resolves to the event's day)
    n = s.history.listening_day_counts().get(key,0)
    assert n == 1
    assert s.settings.get_setting("last_plays_sync_at") == "1000.0"


def test_unverified_metadata_does_not_write_track_history_or_rating():
    s = _store()
    assert live_plays.handle_play_event(_ctx(s), _msg(metadataVerified=False, likeStatus="LIKE"), 1000) is False
    assert s.history.play_events_since(0) == []
    assert s.conn.execute("SELECT COUNT(*) FROM history_items").fetchone()[0] == 0
    assert s.tracks.identity_key_for_video("v1") is None
    assert s.settings.get_setting("last_plays_sync_at") is None
    assert live_plays.handle_play_event(_ctx(s), _msg(metadataVerified=True), 1001) is True


@pytest.mark.parametrize("silent", [{"muted": True}, {"tabMuted": True}, {"volume": 0}, {"paused": True}])
def test_silent_play_does_not_write_but_unmute_records_once(silent):
    s = _store()
    assert not live_plays.handle_play_event(_ctx(s), _msg(likeStatus="LIKE", **silent), 1000)
    assert s.history.play_events_since(0) == []
    assert s.tracks.identity_key_for_video("v1") is None
    assert not s.rec_model.recent_liked_keys()
    assert live_plays.handle_play_event(_ctx(s), _msg(), 1001)
    assert not live_plays.handle_play_event(_ctx(s), _msg(), 1002)
    assert len(s.history.play_events_since(0)) == 1


def test_rereport_does_not_double_count_or_restamp():
    s = _store()
    live_plays.handle_play_event(_ctx(s), _msg(), 1000.0)
    assert live_plays.handle_play_event(_ctx(s), _msg(likeStatus="LIKE"), 1030.0) is False
    key = identity_key("Song", "Artist")
    assert len(s.history.play_events_since(0)) == 1
    n = s.history.listening_day_counts().get(key,0)
    assert n == 1
    assert s.settings.get_setting("last_plays_sync_at") == "1000.0"


def test_like_status_feeds_dislike_model():
    s = _store()
    live_plays.handle_play_event(_ctx(s), _msg(likeStatus="DISLIKE"), 1000.0)
    assert identity_key("Song", "Artist") in s.rec_model.disliked_identity_keys()


def test_like_status_feeds_like_channel():
    s = _store()
    live_plays.handle_play_event(_ctx(s), _msg(likeStatus="LIKE"), 1000.0)
    assert identity_key("Song", "Artist") in set(s.rec_model.recent_liked_keys())


def test_brand_id_selects_matching_identity_and_unknown_reports_are_skipped():
    s = _store(with_brand=True)
    ids = {i.brand_account_id: i.id for i in s.identities.get_identities()}
    live_plays.handle_play_event(_ctx(s), _msg(brandId="10345"), 1000.0)
    assert not live_plays.handle_play_event(_ctx(s), _msg(title="Other", brandId="nope"), 2000.0)
    evs = s.history.play_events_since(0)
    assert evs[0]["identity_id"] == ids["10345"]
    assert len(evs)==1


def test_untitled_or_unconfigured_is_skipped():
    s = _store()
    assert live_plays.handle_play_event(_ctx(s), _msg(title=""), 1000.0) is False
    empty = Store(":memory:"); empty.init_schema()      # no identities provisioned
    assert live_plays.handle_play_event(_ctx(empty), _msg(), 1000.0) is False
    assert empty.history.play_events_since(0) == []


def test_playlist_provenance_recorded():
    s = _store()
    live_plays.handle_play_event(_ctx(s), _msg(playlist="PLabc"), 1000.0)
    assert s.history.play_events_since(0)[0]["playlist_ytm_id"] == "PLabc"


def test_main_remote_account_matches_without_a_configured_master():
    s = Store(":memory:"); s.init_schema()
    s.identities.upsert_identity("only", "bridge", None, False)     # configured, but nothing is master
    ident = s.identities.get_identities()[0].id
    assert live_plays.handle_play_event(_ctx(s), _msg(), 1000.0) is True
    assert s.history.play_events_since(0)[0]["identity_id"] == ident


def test_live_play_and_history_backfill_share_browser_date():
    import datetime

    s = _store()
    now = datetime.datetime(2026, 10, 21, 0, 30, tzinfo=datetime.timezone.utc).timestamp()
    assert live_plays.handle_play_event(_ctx(s), _msg(timeZone="America/Costa_Rica"), now)
    assert s.history.record_history_plays(1, now + 3600, [("song|artist", "Today")],
                                  source_timezone="America/Costa_Rica") == 0
    want = datetime.datetime(2026, 10, 20, 12, tzinfo=datetime.timezone.utc).timestamp()
    assert s.conn.execute("SELECT day_ts FROM listening_days").fetchone()[0] == want
    assert s.history.play_events_since(0)[0]["played_at"] == now
