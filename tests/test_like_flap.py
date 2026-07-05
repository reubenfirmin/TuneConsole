# The like-ratchet fix: YTM's likeStatus is per-context, so a liked track that also sits in
# ordinary playlists reads INDIFFERENT there. These tests pin the three guards that stop the
# resulting flap from corrupting the taste model:
#   1. sync's rated map is priority-ordered (LIKE beats INDIFFERENT; only DISLIKE beats LIKE)
#   2. player-pipeline INDIFFERENT never clears (the sync's whole-run INDIFFERENT owns un-like)
#   3. graduation is once-EVER per key (the stamp survives clear/re-record cycles)
import json

from yt_playlist.library import live_plays, player_events, sync
from yt_playlist.rec import rec_params, recommend
from yt_playlist.util.matching import identity_key
from tests.conftest import FakeClient


def _t(status):
    return {"videoId": "v1", "title": "Song", "artists": [{"name": "Band"}],
            "album": {"name": "X"}, "duration_seconds": 200, "likeStatus": status}


def _client(order):
    """One library where the same track reads LIKE in playlist A and INDIFFERENT in playlist B;
    `order` controls which playlist the sync fetches first (the old map was last-writer-wins)."""
    pls = [{"playlistId": "A", "title": "Liked-ish", "count": 1},
           {"playlistId": "B", "title": "Ordinary", "count": 1}]
    if order == "indifferent_last":
        pls = pls
    else:
        pls = list(reversed(pls))
    return FakeClient(playlists=pls, tracks={"A": [_t("LIKE")], "B": [_t("INDIFFERENT")]})


KEY = identity_key("Song", "Band")


def test_flap_like_wins_within_a_run_both_orders(store):
    iid = store.upsert_identity("main", "cred", None, True)
    for order in ("indifferent_last", "like_last"):
        sync.sync_identity(store, iid, _client(order), now=1000.0)
        assert KEY in store.recent_liked_keys(), order          # the like held
        assert store.like_provenance(KEY) == "sync", order


def test_flap_graduates_at_most_once_across_three_runs(store):
    iid = store.upsert_identity("main", "cred", None, True)
    for i, order in enumerate(("indifferent_last", "like_last", "indifferent_last")):
        sync.sync_identity(store, iid, _client(order), now=1000.0 + i)
    # the graduation ledger was fed exactly once, at the low sync-like weight; before the fix each
    # run cleared and re-recorded the like, feeding the ledger daily until the axis hit the cap
    assert store.get_theme("artist:Band") == rec_params.SOURCE_W_LIKE_SYNCED
    assert store.recent_liked_keys() == [KEY]


def test_whole_run_indifferent_still_clears(store):
    iid = store.upsert_identity("main", "cred", None, True)
    sync.sync_identity(store, iid, _client("indifferent_last"), now=1000.0)
    assert KEY in store.recent_liked_keys()
    # a genuine un-like: EVERY context now reads INDIFFERENT
    gone = FakeClient(playlists=[{"playlistId": "A", "title": "Liked-ish", "count": 1},
                                 {"playlistId": "B", "title": "Ordinary", "count": 1}],
                      tracks={"A": [_t("INDIFFERENT")], "B": [_t("INDIFFERENT")]})
    sync.sync_identity(store, iid, gone, now=2000.0)
    assert KEY not in store.recent_liked_keys()


def test_dislike_still_beats_like_within_a_run(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fake = FakeClient(playlists=[{"playlistId": "A", "title": "a", "count": 1},
                                 {"playlistId": "B", "title": "b", "count": 1}],
                      tracks={"A": [_t("DISLIKE")], "B": [_t("LIKE")]})
    sync.sync_identity(store, iid, fake, now=1000.0)
    assert KEY in store.disliked_identity_keys()
    assert KEY not in store.recent_liked_keys()


def _ctx(s):
    return type("C", (), {"store": s})()


def _play_msg(**over):
    m = {"type": "play", "title": "Song", "artist": "Band", "thumbnail": "",
         "likeStatus": "INDIFFERENT", "videoId": "v1", "playlist": "", "brandId": ""}
    m.update(over)
    return m


def test_pevent_indifferent_never_clears_a_like(store):
    store.upsert_identity("main", "bridge", None, True)
    store.record_like(KEY, 900.0, provenance="action")
    # the player readout shows INDIFFERENT before YTM hydrates the real rating
    live_plays.handle_play_event(_ctx(store), _play_msg(likeStatus="INDIFFERENT"), 1000.0)
    assert KEY in store.recent_liked_keys()


def test_pevent_like_records_action_provenance(store):
    store.upsert_identity("main", "bridge", None, True)
    live_plays.handle_play_event(_ctx(store), _play_msg(likeStatus="LIKE"), 1000.0)
    assert store.like_provenance(KEY) == "action"
    assert store.recent_liked_with_ts() == [(KEY, 1000.0)]     # feeds the transient model


def test_rate_removelike_pevent_does_not_clear_a_like(store):
    store.upsert_identity("main", "bridge", None, True)
    store.upsert_track("v1", "Song", "Band", "", None)
    store.record_like(KEY, 900.0, provenance="action")
    body = json.dumps({"target": {"videoId": "v1"}})
    msg = {"kind": "rate", "url": ".../youtubei/v1/like/removelike", "body": body, "brandId": ""}
    assert player_events.handle_player_event(_ctx(store), msg, 1000.0) is True   # persisted raw
    assert KEY in store.recent_liked_keys()                    # but the like is untouched


def test_rate_like_pevent_acts_with_action_provenance(store):
    store.upsert_identity("main", "bridge", None, True)
    store.upsert_track("v1", "Song", "Band", "", None)
    body = json.dumps({"target": {"videoId": "v1"}})
    msg = {"kind": "rate", "url": ".../youtubei/v1/like/like", "body": body, "brandId": ""}
    player_events.handle_player_event(_ctx(store), msg, 1000.0)
    assert store.like_provenance(KEY) == "action"


# --- the once-ever graduation stamp ---

def _genre_track(store):
    tid = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(tid, "Techno")
    fam = recommend.genre_map.family("Techno")
    return f"genre:{fam}"


def test_like_clear_rerecord_does_not_regraduate(store):
    axis = _genre_track(store)
    recommend.apply_dislikes(store, {KEY: "LIKE"}, 1000.0, provenance="action")
    assert store.get_theme(axis) == rec_params.SOURCE_W_LIKE           # graduated once
    recommend.apply_dislikes(store, {KEY: "INDIFFERENT"}, 2000.0)      # genuine un-like: row gone
    assert KEY not in store.recent_liked_keys()
    recommend.apply_dislikes(store, {KEY: "LIKE"}, 3000.0, provenance="action")   # re-liked
    assert KEY in store.recent_liked_keys()                            # the like itself is back
    assert store.get_theme(axis) == rec_params.SOURCE_W_LIKE           # but NO second graduation


def test_dislike_clear_rerecord_does_not_regraduate(store):
    axis = _genre_track(store)
    recommend.apply_dislikes(store, {KEY: "DISLIKE"}, 1000.0)
    assert store.get_theme(axis) == -rec_params.SOURCE_W_DISLIKE
    # a like supersedes (clears the dislike row), then the user dislikes again
    recommend.apply_dislikes(store, {KEY: "LIKE"}, 2000.0)
    assert KEY not in store.disliked_identity_keys()
    theme_after_like = store.get_theme(axis)
    recommend.apply_dislikes(store, {KEY: "DISLIKE"}, 3000.0)
    assert KEY in store.disliked_identity_keys()                       # suppression is back
    assert store.get_theme(axis) == theme_after_like                   # no second negative feed
