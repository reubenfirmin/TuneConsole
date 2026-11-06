"""#75 live play ingestion: a {type:'play'} bridge frame becomes a play_events row, feeds the
(track, day) history model, stamps plays-freshness, and folds likeStatus into likes/dislikes."""
from yt_playlist.library import live_plays
from yt_playlist.util.matching import identity_key
import pytest


def _configure_accounts(s, *, with_brand=False):
    s.accounts.upsert_account("main", "bridge", None, True)
    if with_brand:
        s.accounts.upsert_account("band", "bridge", "10345", False)
    return s


def _ctx(s):
    return type("C", (), {"store": s})()


def _msg(**over):
    m = {"type": "play", "title": "Song", "artist": "Artist", "thumbnail": "",
         "likeStatus": "INDIFFERENT", "videoId": "v1", "playlist": "", "brandId": ""}
    m.update(over)
    return m


def test_play_persists_event_day_model_and_freshness(store):
    s = _configure_accounts(store)
    assert live_plays.handle_play_event(_ctx(s), _msg(), 1000.0) is True
    key = identity_key("Song", "Artist")
    evs = s.history.play_events_since(0)
    assert [e["identity_key"] for e in evs] == [key]
    # the (track, day) model saw the play too (bare key resolves to the event's day)
    n = s.history.listening_day_counts().get(key,0)
    assert n == 1
    assert s.settings.get_setting("last_plays_sync_at") == "1000.0"


def test_unverified_metadata_does_not_write_track_history_or_rating(store):
    s = _configure_accounts(store)
    assert live_plays.handle_play_event(_ctx(s), _msg(metadataVerified=False, likeStatus="LIKE"), 1000) is False
    assert s.history.play_events_since(0) == []
    assert s.conn.execute("SELECT COUNT(*) FROM listening_observations").fetchone()[0] == 0
    assert s.tracks.identity_key_for_video("v1") is None
    assert s.settings.get_setting("last_plays_sync_at") is None
    assert live_plays.handle_play_event(_ctx(s), _msg(metadataVerified=True), 1001) is True


def test_native_upload_proof_records_known_song_without_importing_unverified_credits_or_rating(store):
    s = _configure_accounts(store)
    video = 'FDuJmIOtT7I'
    title = "Most Precious Love (DF's Future 3000 Extended Mix) (feat. Barbara Tucker)"
    s.tracks.upsert_track(video, title, 'Blaze', 'Most Precious Love', 480)
    before = s.tracks.track_detail(video)
    msg = _msg(videoId=video, title='An uploader title', artist='Uploader channel',
               metadataVerified=False, videoVerified=True, likeStatus='DISLIKE')
    assert live_plays.handle_play_event(_ctx(s), msg, 1000)
    assert s.tracks.track_detail(video) == before
    evidence, more = s.tracks.track_play_ledger(before['recording_id'])
    assert not more and len(evidence) == 1 and evidence[0]['kind'] == 'play'
    assert s.history.play_events_since(0)[0]['identity_key'] == before['identity_key']
    assert not s.rec_model.disliked_identity_keys()
    assert s.tracks.item_rating(1, video) is None
    # Hydrated credits can follow without manufacturing a second play.
    assert not live_plays.handle_play_event(_ctx(s), _msg(videoId=video, title=title, artist='Blaze',
                                                        metadataVerified=True), 1001)
    assert len(s.history.play_events_since(0)) == 1


@pytest.mark.parametrize('overrides', [
    {'videoVerified': False}, {'videoId': 'unknown'}, {'brandKnown': False},
    {'brandId': 'unknown'}, {'muted': True}, {'tabMuted': True}, {'paused': True}, {'volume': 0},
])
def test_unverified_native_play_requires_known_upload_account_and_audible_playback(store, overrides):
    s = _configure_accounts(store)
    s.tracks.upsert_track('v1', 'Canonical title', 'Canonical artist', '', 180)
    msg = _msg(**{'metadataVerified': False, 'videoVerified': True, **overrides})
    assert not live_plays.handle_play_event(_ctx(s), msg, 1000)
    assert not s.history.play_events_since(0)
    assert s.tracks.identity_key_for_video('unknown') is None


@pytest.mark.parametrize("silent", [{"muted": True}, {"tabMuted": True}, {"volume": 0}, {"paused": True}])
def test_silent_play_does_not_write_but_unmute_records_once(store, silent):
    s = _configure_accounts(store)
    assert not live_plays.handle_play_event(_ctx(s), _msg(likeStatus="LIKE", **silent), 1000)
    assert s.history.play_events_since(0) == []
    assert s.tracks.identity_key_for_video("v1") is None
    assert not s.rec_model.recent_liked_keys()
    assert live_plays.handle_play_event(_ctx(s), _msg(), 1001)
    assert not live_plays.handle_play_event(_ctx(s), _msg(), 1002)
    assert len(s.history.play_events_since(0)) == 1


def test_rereport_does_not_double_count_or_restamp(store):
    s = _configure_accounts(store)
    live_plays.handle_play_event(_ctx(s), _msg(), 1000.0)
    assert live_plays.handle_play_event(_ctx(s), _msg(likeStatus="LIKE"), 1030.0) is False
    key = identity_key("Song", "Artist")
    assert len(s.history.play_events_since(0)) == 1
    n = s.history.listening_day_counts().get(key,0)
    assert n == 1
    assert s.settings.get_setting("last_plays_sync_at") == "1000.0"


def test_like_status_feeds_dislike_model(store):
    s = _configure_accounts(store)
    live_plays.handle_play_event(_ctx(s), _msg(likeStatus="DISLIKE"), 1000.0)
    assert identity_key("Song", "Artist") in s.rec_model.disliked_identity_keys()


def test_like_status_feeds_like_channel(store):
    s = _configure_accounts(store)
    live_plays.handle_play_event(_ctx(s), _msg(likeStatus="LIKE"), 1000.0)
    assert identity_key("Song", "Artist") in set(s.rec_model.recent_liked_keys())


def test_brand_id_selects_matching_identity_and_unknown_reports_are_skipped(store):
    s = _configure_accounts(store, with_brand=True)
    ids = {i.brand_account_id: i.id for i in s.accounts.get_accounts()}
    live_plays.handle_play_event(_ctx(s), _msg(brandId="10345"), 1000.0)
    assert not live_plays.handle_play_event(_ctx(s), _msg(title="Other", brandId="nope"), 2000.0)
    evs = s.history.play_events_since(0)
    assert evs[0]["account_id"] == ids["10345"]
    assert len(evs)==1


def test_untitled_or_unconfigured_is_skipped(store):
    # Exercise the empty account state before provisioning this private consumer.
    assert live_plays.handle_play_event(_ctx(store), _msg(), 1000.0) is False
    assert store.history.play_events_since(0) == []
    s = _configure_accounts(store)
    assert live_plays.handle_play_event(_ctx(s), _msg(title=""), 1000.0) is False


def test_playlist_provenance_recorded(store):
    s = _configure_accounts(store)
    live_plays.handle_play_event(_ctx(s), _msg(playlist="PLabc"), 1000.0)
    assert s.history.play_events_since(0)[0]["playlist_ytm_id"] == "PLabc"


def test_main_remote_account_matches_without_a_configured_master(store):
    s = store
    s.accounts.upsert_account("only", "bridge", None, False)     # configured, but nothing is master
    ident = s.accounts.get_accounts()[0].id
    assert live_plays.handle_play_event(_ctx(s), _msg(), 1000.0) is True
    assert s.history.play_events_since(0)[0]["account_id"] == ident


def test_live_play_and_history_backfill_share_browser_date(store):
    import datetime

    s = _configure_accounts(store)
    now = datetime.datetime(2026, 10, 21, 0, 30, tzinfo=datetime.timezone.utc).timestamp()
    assert live_plays.handle_play_event(_ctx(s), _msg(timeZone="America/Costa_Rica"), now)
    assert s.history.record_history_plays(1, now + 3600, [("song|artist", "Today")],
                                  source_timezone="America/Costa_Rica") == 0
    want = datetime.datetime(2026, 10, 20, 12, tzinfo=datetime.timezone.utc).timestamp()
    assert s.conn.execute("SELECT day_ts FROM listening_days").fetchone()[0] == want
    assert s.history.play_events_since(0)[0]["played_at"] == now


def test_selected_album_playback_is_context_evidence_not_organic_track_learning(store):
    from yt_playlist.core import album_playback_context
    from yt_playlist.rec import choice_evidence, layers

    s = _configure_accounts(store, with_brand=True)
    s.tracks.upsert_track('v1', 'Song', 'Artist', None, None)
    s.tracks.upsert_track('v2', 'Next', 'Artist', None, None)
    oid = choice_evidence.issue(s, 'albums', [{'id': 'album', 'keys': ['song|artist']}],
                                990, context={})
    choice_evidence.respond(s, oid, 'choice', 991, option_id='album', action_id='chosen')
    with s.conn:
        album_playback_context.put(s.conn, {'opportunity_id': oid,
            'browse': 'album', 'playlist': 'OLAK-album', 'videos': ['v1', 'v2'],
            'at': 991, 'action_id': 'chosen'})
    assert live_plays.handle_play_event(_ctx(s), _msg(playlist='OLAK-album'), 1000)
    assert s.conn.execute('SELECT is_generated FROM play_events').fetchone()[0] == 1
    assert layers._plays(s, 0) == []
    assert choice_evidence.compile_report(s, 1001)['formats']['albums']['starts'] == 1
    # The first verified account owns the pending queue; another account is not attributed.
    pending = album_playback_context.load(s.conn)
    assert pending['account_id'] == 1
    assert choice_evidence.album_playback_context(s, _msg(playlist='OLAK-album'), 1001,
                                                 account_id=2) is None
    assert choice_evidence.album_playback_context(s, _msg(playlist='OLAK-album', videoId='outside'),
                                                 1001, account_id=1) is None
    # Album continuation stays quarantined even though it contains multiple tracks.
    assert live_plays.handle_play_event(_ctx(s), _msg(playlist='OLAK-album', title='Next', videoId='v2'), 1200)
    assert layers._plays(s, 0) == []
    # A later independent direct choice has its own organic evidence.
    assert live_plays.handle_play_event(_ctx(s), _msg(), 5000)
    assert [row['identity_key'] for row in layers._plays(s, 0)] == ['song|artist']


def test_album_attribution_reads_committed_context_during_another_write(file_backed_store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from yt_playlist.core import album_playback_context
    from yt_playlist.rec import choice_evidence

    store = file_backed_store
    writing, release = Event(), Event()
    try:
        account = store.accounts.upsert_account('main', 'bridge', None, True)
        pending = {'opportunity_id': 'chosen', 'browse': 'album', 'playlist': 'OLAK-album',
                   'videos': ['v1'], 'at': 991, 'account_id': account}
        with store._lock, store.conn:
            album_playback_context.put(store.conn, pending)

        def write_next_queue():
            with store._lock, store.conn:
                album_playback_context.put(store.conn, {**pending, 'playlist': 'OLAK-next'})
                writing.set()
                assert release.wait(10)

        with ThreadPoolExecutor(max_workers=1) as executor:
            writer = executor.submit(write_next_queue)
            try:
                assert writing.wait(5)
                # A refill or playback writer may be mid-transaction. Attribution
                # must see the committed queue without touching that transaction.
                actual = choice_evidence.album_playback_context(
                    store, _msg(playlist='OLAK-album'), 1000, account_id=account)
                assert actual == pending
            finally:
                release.set()
            writer.result(timeout=5)
        assert album_playback_context.load(store.conn)['playlist'] == 'OLAK-next'
    finally:
        release.set()
