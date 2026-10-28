"""Unscanned uploads retain confirmed feedback and resolve outside the bridge receive loop."""
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.core.store import Store
from yt_playlist.library import player_events, ratings, sync
from yt_playlist.rec import rec_worker
from yt_playlist.web.app import create_app


def _event(action="like", **outcome):
    return {"kind": "rate", "url": f"https://music.youtube.com/youtubei/v1/like/{action}",
            "brandKnown": True,
            "body": json.dumps({"target": {"videoId": "unseen"}}), "phase": "settled",
            "success": True, "httpStatus": 200, **outcome}


def _song(video_id):
    return {"videoDetails": {"videoId": video_id, "title": "Unseen song", "author": "Artist"}}


@pytest.mark.parametrize("action", ["like", "dislike"])
def test_unseen_confirmed_rating_survives_restart_and_teaches_without_playlist(tmp_path, action):
    path = tmp_path / "ratings.db"
    store = Store(path)
    store.init_schema()
    iid = store.identities.upsert_identity("main", "cred", None, True)
    calls = []
    ctx = SimpleNamespace(store=store, rec_worker=SimpleNamespace(
        trigger_rating_update=lambda: calls.append("scheduled")))
    assert player_events.handle_player_event(ctx, _event(action), now=100)
    assert calls == ["scheduled"]
    assert store.tracks.identity_key_for_video("unseen") is None
    store.conn.close()

    store = Store(path)
    store.init_schema()
    keys = ratings.reconcile_pending(store, iid, SimpleNamespace(get_song=_song))
    assert keys == {"unseen song|artist"}
    assert store.conn.execute("SELECT COUNT(*) FROM library_memberships").fetchone()[0] == 0
    if action == "like":
        assert store.rec_signals.get_theme("artist:Artist") == 1
        assert store.rec_model.recent_liked_with_ts() == [("unseen song|artist", 100)]
    else:
        assert store.rec_model.disliked_with_ts() == [("unseen song|artist", 100)]
        assert "unseen song|artist" in store.rec_model.suppressed_keys("for_you", 100)
    assert ratings.reconcile_pending(store, iid, SimpleNamespace(get_song=_song)) == set()
    store.conn.close()


def test_failed_metadata_retries_during_normal_sync(store, monkeypatch):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    player_events.handle_player_event(SimpleNamespace(store=store), _event(), 100)
    monkeypatch.setattr(ratings, "with_retry", lambda fn: fn())

    def unavailable(video_id):
        raise RuntimeError("offline")

    assert ratings.reconcile_pending(store, iid, SimpleNamespace(get_song=unavailable)) == set()
    assert store.tracks.item_rating(iid, "unseen")["status"] == "LIKE"
    client = FakeClient(playlists=[{"playlistId": "P", "title": "Mix", "count": 0}], tracks={"P": []})
    client.get_song = _song
    sync.sync_identity(store, iid, client, now=200)
    assert store.rec_model.recent_liked_with_ts() == [("unseen song|artist", 100)]


def test_mismatched_metadata_is_not_bound_to_the_rating(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    ratings.record_confirmed(store, iid, "unseen", "DISLIKE", 100)
    assert ratings.reconcile_pending(store, iid, SimpleNamespace(get_song=lambda _: _song("other"))) == set()
    assert store.tracks.identity_key_for_video("unseen") is None
    assert store.rec_model.disliked_identity_keys() == set()
    assert store.tracks.pending_action_ratings(iid)[0]["video_id"] == "unseen"


def test_unlike_arriving_during_metadata_fetch_prevents_stale_learning(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    ratings.record_confirmed(store, iid, "unseen", "LIKE", 100)

    def resolve(video_id):
        ratings.record_confirmed(store, iid, video_id, "INDIFFERENT", 101)
        return _song(video_id)

    assert ratings.reconcile_pending(store, iid, SimpleNamespace(get_song=resolve)) == set()
    assert store.rec_model.recent_liked_keys() == []
    assert store.rec_signals.get_theme("artist:Artist") is None


def test_failed_native_request_does_not_queue_learning(store):
    store.identities.upsert_identity("main", "cred", None, True)
    calls = []
    ctx = SimpleNamespace(store=store, rec_worker=SimpleNamespace(
        trigger_rating_update=lambda: calls.append(True)))
    player_events.handle_player_event(ctx, _event(success=False, httpStatus=403), 100)
    assert calls == []
    assert store.tracks.pending_action_ratings(1) == []


def test_backend_rating_retains_pending_feedback_without_waiting_for_metadata(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    writes = []
    remote = SimpleNamespace(get_song=lambda _: pytest.fail("metadata must be resolved by the worker"),
                             rate_song=lambda *args: writes.append(args))
    ratings.set_rating(store, iid, "unseen", "LIKE", remote, 100)
    assert writes == [("unseen", "LIKE")]
    assert store.tracks.identity_key_for_video("unseen") is None
    assert store.tracks.pending_action_ratings(iid) == [
        {"video_id": "unseen", "status": "LIKE", "observed_at": 100}]
    assert ratings.reconcile_pending(store, iid, SimpleNamespace(get_song=_song)) == {"unseen song|artist"}
    assert store.rec_model.recent_liked_with_ts() == [("unseen song|artist", 100)]
    assert store.rec_signals.get_theme("artist:Artist") == 1


@pytest.mark.parametrize("endpoint", ["/track/like", "/now-playing/rate"])
def test_both_heart_routes_schedule_unknown_subjects_after_confirming_rating(store, monkeypatch, endpoint):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    reads = []
    remote = SimpleNamespace(rate_song=lambda *args: None,
        get_song=lambda video_id: reads.append(video_id) or _song(video_id))
    app = create_app(store, lambda: {iid: remote}, now_fn=lambda: 100, background_work=False)
    worker = app.state.ctx.rec_worker
    worker.enabled, worker.debounce_s = True, 0
    queued = []
    monkeypatch.setattr(worker, "_start_loop", lambda: queued.append(worker._loop))
    client = TestClient(app, base_url="http://127.0.0.1")
    response = (client.post(endpoint, data={"video_id": "unseen", "on": "1"})
                if endpoint == "/track/like" else
                client.post(endpoint, json={"videoId": "unseen", "action": "like", "likeStatus": "LIKE"}))
    assert response.status_code == 200
    if endpoint == "/now-playing/rate":
        assert response.json()["ok"] is True
    assert store.tracks.item_rating(iid, "unseen")["status"] == "LIKE"
    assert len(queued) == 1 and reads == []
    assert store.tracks.identity_key_for_video("unseen") is None
    queued.pop()()
    assert reads == ["unseen"]
    assert store.rec_model.recent_liked_with_ts() == [("unseen song|artist", 100)]


def test_later_catalog_observation_resolves_pending_rating_without_network(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    ratings.record_confirmed(store, iid, "unseen", "DISLIKE", 100)
    store.tracks.observe_track("unseen", "Unseen song", "Artist", now=200)
    assert ratings.reconcile_pending(store, iid, None) == {"unseen song|artist"}
    assert store.rec_model.disliked_with_ts() == [("unseen song|artist", 100)]


def test_rating_worker_coalesces_and_resolves_before_background_refresh(store, monkeypatch):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    ratings.record_confirmed(store, iid, "unseen", "LIKE", 100)
    calls = []
    worker = rec_worker.RecWorker(SimpleNamespace(store=store, now_fn=lambda: 200,
        client_provider=lambda: {iid: SimpleNamespace(get_song=_song)},
        logger=logging.getLogger("test")), debounce_s=0)
    queued = []
    monkeypatch.setattr(worker, "_start_loop", lambda: queued.append(worker._loop))
    monkeypatch.setattr(worker, '_do_rebuild', lambda: calls.append('refresh'))
    worker.trigger_rating_update()
    worker.trigger_rating_update()
    assert len(queued) == 1
    assert store.tracks.identity_key_for_video("unseen") is None
    queued.pop()()
    assert calls == ['refresh']
    assert store.rec_refresh.revision() is not None
    assert not worker.busy
