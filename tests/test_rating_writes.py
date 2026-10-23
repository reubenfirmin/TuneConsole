"""Explicit intents remain ordered through retries and native reconciliation."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tests.conftest import FakeClient
from yt_playlist.library import ratings
from yt_playlist.library.ops import PlaylistOps
from yt_playlist.web.routes.bridge import build


@pytest.mark.parametrize("outcome", ["success", "retry", "rejected", "lost_reply"])
def test_operations_instances_order_same_upload_and_allow_unrelated_work(store, monkeypatch, outcome):
    main = store.identities.upsert_identity("Main", "bridge", None, True)
    other = store.identities.upsert_identity("Other", "bridge", "other", False)
    entered, release, queued = threading.Event(), threading.Event(), threading.Event()
    calls, remote = [], {}
    attempts = 0

    class Client(FakeClient):
        def rate_song(self, video_id, status):
            nonlocal attempts
            calls.append((video_id, status))
            if video_id == "song" and status == "LIKE":
                attempts += 1
                entered.set()
                assert release.wait(5)
                if outcome == "rejected":
                    return {"status": "STATUS_FAILED"}
                remote[video_id] = status
                if outcome == "lost_reply" or (outcome == "retry" and attempts < 3):
                    raise TimeoutError("rating response lost")
            remote[video_id] = status
            return {"status": "STATUS_SUCCEEDED"}

    client = Client()
    original = ratings.with_retry
    monkeypatch.setattr(ratings, "with_retry", lambda fn: original(fn, sleep=lambda _: None))
    ops = lambda: PlaylistOps(store, lambda: {main: client}, lambda: 100)

    def unlike():
        queued.set()
        return ops().set_rating("song", "INDIFFERENT")

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(ops().set_rating, "song", "LIKE")
        try:
            assert entered.wait(5)
            last = pool.submit(unlike)
            assert queued.wait(5)
            pool.submit(ops().set_rating, "different", "LIKE").result(timeout=5)
            pool.submit(ratings.set_rating, store, other, "song", "DISLIKE", FakeClient(), 100).result(timeout=5)
            assert ("song", "INDIFFERENT") not in calls
        finally:
            release.set()
        if outcome in ("rejected", "lost_reply"):
            with pytest.raises((ValueError, TimeoutError)):
                first.result(timeout=5)
        else:
            first.result(timeout=5)
        last.result(timeout=5)
    assert calls[-1] == ("song", "INDIFFERENT")
    assert remote["song"] == "INDIFFERENT"
    assert store.tracks.item_rating(main, "song") == {
        "status": "INDIFFERENT", "source": "api", "observed_at": 100}
    assert store.tracks.item_rating(other, "song")["status"] == "DISLIKE"


def test_native_reconciliation_holds_the_turn_and_late_confirmation_clock_cannot_reject_final_intent(store):
    owner = store.identities.upsert_identity("Main", "bridge", None, True)
    entered, release, second_sent = threading.Event(), threading.Event(), threading.Event()
    client = FakeClient()
    original = client.rate_song

    def rate(video, status):
        if status == "INDIFFERENT":
            second_sent.set()
        return original(video, status)

    client.rate_song = rate

    def native(identity_id):
        entered.set()
        assert release.wait(5)
        ratings.record_confirmed(store, identity_id, "song", "LIKE", 200)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ratings.set_rating, store, owner, "song", "LIKE", client, 100,
                            after_confirmed=native)
        try:
            assert entered.wait(5)
            last = pool.submit(ratings.set_rating, store, owner, "song", "INDIFFERENT", client, 110)
            assert not second_sent.wait(.1)
        finally:
            release.set()
        first.result(timeout=5)
        last.result(timeout=5)
    assert client.rated == [("song", "LIKE"), ("song", "INDIFFERENT")]
    assert store.tracks.item_rating(owner, "song") == {
        "status": "INDIFFERENT", "source": "api", "observed_at": 200}


def test_failed_player_mirror_cannot_report_a_confirmed_rating_as_rejected(store):
    owner = store.identities.upsert_identity("Main", "bridge", None, True)
    ops = PlaylistOps(store, lambda: {owner: FakeClient()}, lambda: 100)

    def failed(identity_id):
        raise TimeoutError("Native thumb response lost")

    assert ops.set_rating("song", "LIKE", after_confirmed=failed) == {
        "likeStatus": "LIKE", "player_error": "Native thumb response lost"}
    assert store.tracks.item_rating(owner, "song")["status"] == "LIKE"


def test_cancelled_http_waiter_keeps_native_work_owned_and_next_intent_ordered(store):
    owner = store.identities.upsert_identity("Main", "bridge", None, True)
    remote = FakeClient()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        native = []

        async def control(payload, timeout):
            native.append(payload)
            if payload["likeStatus"] == "LIKE":
                entered.set()
                await release.wait()
            return 200, '{"ok":true}'

        bridge = SimpleNamespace(connected=True, now_playing={"video_id": "song"}, request_control=control)
        ctx = SimpleNamespace(store=store, bridge=bridge, now_fn=lambda: 100,
            ops=lambda: PlaylistOps(store, lambda: {owner: remote}, lambda: 100))
        app = FastAPI()
        app.include_router(build(ctx))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            payload = {"action": "like", "videoId": "song", "likeStatus": "LIKE"}
            first = asyncio.create_task(client.post("/now-playing/rate", json=payload))
            await asyncio.wait_for(entered.wait(), 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert ctx.work.active
            last = asyncio.create_task(client.post("/now-playing/rate", json={**payload, "likeStatus": "INDIFFERENT"}))
            try:
                async def both_admitted():
                    while len(ctx.work.active) < 2:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(both_admitted(), 5)
                assert remote.rated == [("song", "LIKE")]
            finally:
                release.set()
            response = await asyncio.wait_for(last, 5)
            assert response.json() == {"ok": True, "error": ""}
            assert await asyncio.to_thread(ctx.work.wait, 5)
            assert [p["likeStatus"] for p in native] == ["LIKE", "INDIFFERENT"]
            assert all(p["videoId"] == "song" and not p["skip"] for p in native)

    asyncio.run(scenario())
    assert remote.rated == [("song", "LIKE"), ("song", "INDIFFERENT")]
    assert store.tracks.item_rating(owner, "song")["status"] == "INDIFFERENT"
