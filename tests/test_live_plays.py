"""#75 live play ingestion: a {type:'play'} bridge frame becomes a play_events row, feeds the
(track, day) history model, stamps plays-freshness, and folds likeStatus into likes/dislikes."""
from yt_playlist.core.store import Store
from yt_playlist.library import live_plays
from yt_playlist.util.matching import identity_key


def _store(with_brand=False):
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    if with_brand:
        s.upsert_identity("band", "bridge", "10345", False)
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
    evs = s.play_events_since(0)
    assert [e["identity_key"] for e in evs] == [key]
    # the (track, day) model saw the play too (bare key resolves to the event's day)
    n = s.conn.execute("SELECT COUNT(*) FROM history_items WHERE identity_key=?", (key,)).fetchone()[0]
    assert n == 1
    assert s.get_setting("last_plays_sync_at") == "1000.0"


def test_rereport_does_not_double_count_or_restamp():
    s = _store()
    live_plays.handle_play_event(_ctx(s), _msg(), 1000.0)
    assert live_plays.handle_play_event(_ctx(s), _msg(likeStatus="LIKE"), 1030.0) is False
    key = identity_key("Song", "Artist")
    assert len(s.play_events_since(0)) == 1
    n = s.conn.execute("SELECT COUNT(*) FROM history_items WHERE identity_key=?", (key,)).fetchone()[0]
    assert n == 1
    assert s.get_setting("last_plays_sync_at") == "1000.0"


def test_like_status_feeds_dislike_model():
    s = _store()
    live_plays.handle_play_event(_ctx(s), _msg(likeStatus="DISLIKE"), 1000.0)
    assert identity_key("Song", "Artist") in s.disliked_identity_keys()


def test_like_status_feeds_like_channel():
    s = _store()
    live_plays.handle_play_event(_ctx(s), _msg(likeStatus="LIKE"), 1000.0)
    assert identity_key("Song", "Artist") in set(s.recent_liked_keys())


def test_brand_id_selects_matching_identity_else_master():
    s = _store(with_brand=True)
    ids = {i.brand_account_id: i.id for i in s.get_identities()}
    master = s.get_master_identity().id
    live_plays.handle_play_event(_ctx(s), _msg(brandId="10345"), 1000.0)
    live_plays.handle_play_event(_ctx(s), _msg(title="Other", brandId="nope"), 2000.0)
    evs = s.play_events_since(0)
    assert evs[0]["identity_id"] == ids["10345"]
    assert evs[1]["identity_id"] == master


def test_untitled_or_unconfigured_is_skipped():
    s = _store()
    assert live_plays.handle_play_event(_ctx(s), _msg(title=""), 1000.0) is False
    empty = Store(":memory:"); empty.init_schema()      # no identities provisioned
    assert live_plays.handle_play_event(_ctx(empty), _msg(), 1000.0) is False
    assert empty.play_events_since(0) == []


def test_playlist_provenance_recorded():
    s = _store()
    live_plays.handle_play_event(_ctx(s), _msg(playlist="PLabc"), 1000.0)
    assert s.play_events_since(0)[0]["playlist_ytm_id"] == "PLabc"


def test_no_master_falls_back_to_first_identity():
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("only", "bridge", None, False)     # configured, but nothing is master
    ident = s.get_identities()[0].id
    assert live_plays.handle_play_event(_ctx(s), _msg(), 1000.0) is True
    assert s.play_events_since(0)[0]["identity_id"] == ident
