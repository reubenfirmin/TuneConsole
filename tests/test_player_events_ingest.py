"""#91 pevent ingestion: raw persistence per kind, and the single wired consumer (rate -> model)."""
import json
import pytest

from yt_playlist.core.store import Store
from yt_playlist.library import player_events
from yt_playlist.util.matching import identity_key


def _store():
    s = Store(":memory:"); s.init_schema()
    s.identities.upsert_identity("main", "bridge", None, True)
    return s


def _ctx(s):
    return type("C", (), {"store": s})()


def test_playback_kind_persists_fields_and_payload():
    s = _store()
    msg = {"type": "pevent", "kind": "track_exit", "videoId": "v1", "position": 19.5,
           "duration": 400.0, "playlist": "PL9", "shuffle": "true", "repeat": "NONE", "brandId": ""}
    assert player_events.handle_player_event(_ctx(s), msg, 1000.0) is True
    e = s.player_events.player_events_since(0)[0]
    assert e["kind"] == "track_exit" and e["video_id"] == "v1" and e["position"] == 19.5
    assert e["playlist_ytm_id"] == "PL9"
    assert json.loads(e["payload"]) == {"shuffle": "true", "repeat": "NONE"}


def test_unknown_kind_rejected():
    s = _store()
    assert player_events.handle_player_event(_ctx(s), {"kind": "evil"}, 1000.0) is False
    assert s.player_events.player_events_since(0) == []


def test_rate_on_known_track_feeds_model():
    s = _store()
    s.tracks.upsert_track("vidX", "Song", "Artist", "", None)
    body = json.dumps({"target": {"videoId": "vidX"}})
    msg = {"kind": "rate", "url": "https://music.youtube.com/youtubei/v1/like/dislike",
           "brandKnown": True,
           "phase": "settled", "success": True, "httpStatus": 200,
           "body": body, "href": "https://music.youtube.com/watch?v=vidX", "brandId": ""}
    assert player_events.handle_player_event(_ctx(s), msg, 1000.0) is True
    assert identity_key("Song", "Artist") in s.rec_model.disliked_identity_keys()
    e = s.player_events.player_events_since(0, kind="rate")[0]
    assert e["video_id"] == "vidX" and json.loads(e["payload"])["action"] == "dislike"


def test_rate_removelike_maps_to_indifferent_and_unknown_track_is_raw_only():
    s = _store()
    body = json.dumps({"target": {"videoId": "ghost"}})
    msg = {"kind": "rate", "url": ".../youtubei/v1/like/removelike", "body": body, "brandId": "", "brandKnown": True}
    assert player_events.handle_player_event(_ctx(s), msg, 1000.0) is True   # persisted raw
    assert s.rec_model.disliked_identity_keys() == set()                               # but no model write


def test_playlist_edit_extracts_playlist_and_video():
    s = _store()
    body = json.dumps({"playlistId": "PLmine", "actions": [{"addedVideoId": "v9", "action": "ACTION_ADD_VIDEO"}]})
    msg = {"kind": "playlist_edit", "url": ".../youtubei/v1/browse/edit_playlist", "body": body, "brandId": "", "brandKnown": True}
    player_events.handle_player_event(_ctx(s), msg, 1000.0)
    e = s.player_events.player_events_since(0)[0]
    assert e["playlist_ytm_id"] == "PLmine" and e["video_id"] == "v9"


def test_curation_body_is_truncated_and_bad_json_tolerated():
    s = _store()
    msg = {"kind": "feedback", "url": ".../youtubei/v1/feedback", "body": "{" + "x" * 9000, "brandId": "", "brandKnown": True}
    assert player_events.handle_player_event(_ctx(s), msg, 1000.0) is True
    payload = json.loads(s.player_events.player_events_since(0)[0]["payload"])
    assert len(payload["body"]) <= 4096


def test_rate_url_with_querystring_still_parses_action():
    s = _store()
    s.tracks.upsert_track("vidQ", "Song", "Artist", "", None)
    body = json.dumps({"target": {"videoId": "vidQ"}})
    msg = {"kind": "rate", "url": "https://music.youtube.com/youtubei/v1/like/dislike?key=abc&prettyPrint=false",
           "brandKnown": True,
           "phase": "settled", "success": True, "httpStatus": 200,
           "body": body, "brandId": ""}
    assert player_events.handle_player_event(_ctx(s), msg, 1000.0) is True
    assert identity_key("Song", "Artist") in s.rec_model.disliked_identity_keys()


def test_unconfigured_store_skips():
    empty = Store(":memory:"); empty.init_schema()
    assert player_events.handle_player_event(_ctx(empty), {"kind": "tick", "videoId": "v"}, 1.0) is False


@pytest.mark.parametrize("action", ["like", "dislike"])
@pytest.mark.parametrize("outcome,confirmed", [({}, False), ({"phase": "intent"}, False),
    ({"phase": "settled", "success": False, "httpStatus": 403}, False),
    ({"phase": "settled", "success": False, "httpStatus": 500}, False),
    ({"phase": "settled", "success": False, "httpStatus": 0, "error": "offline"}, False),
    ({"phase": "settled", "success": True, "httpStatus": 200}, True)])
def test_rating_requires_confirmation_but_retains_raw_outcomes(action, outcome, confirmed):
    s = _store()
    s.tracks.upsert_track("v", "Song", "Artist", "", None)
    msg = {"kind": "rate", "url": f"https://music.youtube.com/youtubei/v1/like/{action}",
           "brandKnown": True,
           "body": json.dumps({"target": {"videoId": "v"}}), **outcome}
    assert player_events.handle_player_event(_ctx(s), msg, 1000.0)
    rating = s.tracks.item_rating(1, "v")
    assert (rating is not None) == confirmed
    if confirmed:
        assert rating == {"status": action.upper(), "source": "api", "observed_at": 1000.0}
    key = s.tracks.identity_key_for_video("v")
    assert (key in s.rec_model.recent_liked_keys()) == (confirmed and action == "like")
    assert (key in s.rec_model.disliked_identity_keys()) == (confirmed and action == "dislike")
    rows = s.player_events.player_events_since(0)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert all(payload[k] == v for k, v in outcome.items())


@pytest.mark.parametrize('kind', sorted(player_events.CURATION_KINDS))
@pytest.mark.parametrize('known', [None, False, 0, 1, 'true'])
def test_unknown_or_legacy_curation_never_creates_account_evidence(kind, known):
    s = _store()
    s.tracks.upsert_track('v', 'Song', 'Artist', '', None)
    msg = {'kind': kind, 'url': 'https://music.youtube.com/youtubei/v1/like/dislike',
           'body': json.dumps({'target': {'videoId': 'v'}}),
           'phase': 'settled', 'success': True, 'httpStatus': 200, 'brandId': ''}
    if known is not None:
        msg['brandKnown'] = known
    assert not player_events.handle_player_event(_ctx(s), msg, 1000)
    assert s.player_events.player_events_since(0) == []
    assert s.tracks.item_rating(1, 'v') is None
    assert s.rec_model.disliked_identity_keys() == set()
