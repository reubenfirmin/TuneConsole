import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from yt_playlist.library import executor
from yt_playlist.web import radio_session
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_playlist_tail import PlaylistPending
from yt_playlist.web.routes import radio_session as radio_routes


class Playlist:
    def __init__(self):
        self.ids = []
        self.adds = []
        self.removes = []
        self.fail_add = False
        self.lose_response = False
        self.fail_read = False
        self.after_add = lambda: None

    def get_playlist(self, playlist, limit=None):
        assert playlist == "PLSESSION"
        if self.fail_read:
            self.fail_read = False
            raise RuntimeError("Read response lost")
        return {"tracks": [{"videoId": vid, "setVideoId": "set-" + vid} for vid in self.ids],
                "trackCount": len(self.ids)}

    def add_playlist_items(self, playlist, ids):
        assert playlist == "PLSESSION"
        self.adds.append(list(ids))
        if self.fail_add:
            return {"status": "STATUS_FAILED"}
        self.ids.extend(ids)
        self.after_add()
        if self.lose_response:
            raise RuntimeError("Add response lost")
        return {"status": "STATUS_SUCCEEDED"}

    def remove_playlist_items(self, playlist, tracks):
        assert playlist == "PLSESSION"
        ids = [t["videoId"] for t in tracks]
        self.removes.append(ids)
        for video in ids:
            self.ids.remove(video)
        return {"status": "STATUS_SUCCEEDED"}


@pytest.fixture
def poc(monkeypatch, store):
    remote, frames, workers = Playlist(), [], []
    identity = store.identities.upsert_identity("main", "ref", None, True)
    bridge = SimpleNamespace(connected=True, now_playing=None, now_playing_seen_at=None,
                             send_control=frames.append, requests=[], native_ids=[], auto_adopt=True)

    async def request_control(frame):
        bridge.requests.append(frame)
        player = dict(bridge.now_playing or {})
        if frame["refresh"] and bridge.auto_adopt:
            bridge.native_ids = list(remote.ids)
        video = player.get("video_id")
        expected = frame["videoIds"]
        native = bridge.native_ids
        synced = (player.get("playlist_ytm_id") == frame["playlist"]
                  and video in expected and video in native
                  and expected[expected.index(video) + 1:] == native[native.index(video) + 1:])
        return 200, json.dumps({"ok": True, "player": player, "playback_synced": synced,
            "native": {"available": True, "videoIds": native},
            "refresh": "requested" if frame["refresh"] else "not requested"})

    bridge.request_control = request_control
    ctx = SimpleNamespace(bridge=bridge, store=store,
                          now_fn=lambda: 1234.0, client_provider=lambda: {identity: remote})
    controller = PlaylistRadio(ctx)
    ctx.radio = controller
    monkeypatch.setattr(controller.work, "start_thread", lambda *args, **kw: workers.append((args, kw)))
    pool = [{"key": f"k{i}", "video_id": f"v{i}", "title": f"Song {i}", "artist": f"Artist {i}"}
            for i in range(1, 33)]
    for track in pool:
        tid = store.tracks.upsert_track(track["video_id"], track["title"], track["artist"], "")
        track["key"] = store.tracks.catalog_subject(tid)["identity_key"]

    class Candidates:
        def __init__(self, *args):
            self.by_video = {t["video_id"]: t for t in pool}
            self.by_key = {t["key"]: t for t in pool}
            self.fingerprint = "fixed-test-candidates"
            self.modes = {}

        def retain(self, tracks):
            self.by_video.update((t["video_id"], t) for t in tracks)
            self.by_key.update((t["key"], t) for t in tracks)

        def cluster_for(self, track):
            return int(track["video_id"][1:]) // 8

        def similarity(self, a, b):
            return float(a["key"] == b["key"])

        def seed_offer(self, n=16):
            return pool[:n]

        def next_track(self, history, *, target=None, feedback=None, excluded=()):
            used = {t["video_id"] for t in history} | set(excluded)
            return next((t for t in pool if t["video_id"] not in used), None)

        def direction_anchor(self, history, anchors, *, excluded=(), feedback=None):
            return self.next_track(history + anchors, excluded=excluded)

    monkeypatch.setattr(radio_session, "RadioCandidates", Candidates)

    def create(store, title, tracks, client, now, identity):
        assert title.startswith("Radio Session · ")
        assert identity == store.identities.get_master_identity().id
        remote.ids = [t["video_id"] for t in tracks]
        bridge.native_ids = list(remote.ids)
        pid = store.playlists.upsert_playlist(identity, "PLSESSION", title, len(tracks), "", now)
        return {"new_ytm": "PLSESSION", "pid": pid, "added": len(tracks)}

    monkeypatch.setattr(executor, "create_generated_playlist", create)
    yield controller, remote, frames, workers, pool
    controller.stop()
    controller.work.stop_executor()


def play(controller, video, playlist="PLSESSION", **extra):
    controller.ctx.bridge.now_playing = {"video_id": video, "playlist_ytm_id": playlist,
                                       "paused": False, "position": 30, "duration": 300, **extra}
    controller.ctx.bridge.now_playing_seen_at = time.monotonic()
    controller._browser(controller.session)
    controller.advance(controller.session)


def test_three_to_start_then_one_append_per_song_and_no_playback_queue_commands(poc):
    c, remote, frames, workers, _ = poc
    assert c.start()["ok"]
    assert remote.ids == ["v1", "v2", "v3"]
    assert len(workers) == 1
    assert frames == [{"type": "navigate", "url":
                       "https://music.youtube.com/watch?v=v1&list=PLSESSION"}]
    play(c, "v1")
    assert remote.adds == []
    play(c, "v2")
    assert remote.adds == []
    assert c.status()["ahead"] == 1
    for i in range(3, 8):
        play(c, f"v{i}")
        assert remote.ids == [f"v{j}" for j in range(1, i + 2)]
        assert c.status()["ahead"] == 1
        play(c, f"v{i}")  # Duplicate observations cannot append extra songs.
        assert len(remote.adds) == i - 2
    assert len(frames) == 1  # Playback navigation happens only at explicit start.
    assert all(frame["type"] == "radio-session" for frame in c.ctx.bridge.requests)
    assert c.ctx.store.playlists.get_playlist_track_keys(c.session.local_id) == [
        t["key"] for t in c.session.tracks]


def test_skip_forward_catches_up_and_going_back_does_not_append(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v3")
    assert remote.ids == ["v1", "v2", "v3", "v4"]
    assert c.status()["ahead"] == 1
    play(c, "v1")
    assert remote.adds == [["v4"]]


@pytest.mark.parametrize("video,playlist,extra", [
    ("v3", "OTHER", {}), ("unknown", "PLSESSION", {}),
    ("v3", "", {}), ("v3", "PLSESSION", {"paused": True}),
])
def test_only_playback_in_this_playlist_advances_it(poc, video, playlist, extra):
    c, remote, *_ = poc
    c.start()
    play(c, video, playlist, **extra)
    assert remote.adds == []


def test_stale_playback_is_ignored(poc):
    c, remote, *_ = poc
    c.start()
    c.ctx.bridge.now_playing = {"video_id": "v3", "playlist_ytm_id": "PLSESSION"}
    c._browser(c.session)
    c.session.browser_seen_at = time.monotonic() - 20
    c.advance(c.session)
    assert remote.adds == []


def test_lost_add_response_is_read_back_without_duplicate_append(poc):
    c, remote, *_ = poc
    c.start()
    remote.lose_response = True
    play(c, "v3")
    play(c, "v3")
    assert remote.ids == ["v1", "v2", "v3", "v4"]
    assert remote.adds == [["v4"]]
    assert c.status()["ahead"] == 1


def test_uncertain_read_after_append_is_recovered_before_another_write(poc):
    c, remote, *_ = poc
    c.start()
    remote.after_add = lambda: setattr(remote, "fail_read", True)
    with pytest.raises(RuntimeError, match="Read response lost"):
        play(c, "v3")
    assert c.status()["ahead"] == 0
    assert c.session.pending["video_id"] == "v4"
    play(c, "v3")
    assert remote.adds == [["v4"]]
    assert c.status()["ahead"] == 1


def test_rejected_append_stays_pending_until_confirmed(poc):
    c, remote, *_ = poc
    c.start()
    remote.fail_add = True
    play(c, "v3")
    assert len(c.status()["tracks"]) == 3
    assert c.session.pending["video_id"] == "v4"
    remote.fail_add = False
    play(c, "v3")
    assert remote.adds == [["v4"], ["v4"]]
    assert c.status()["ahead"] == 1


def test_playback_can_reach_an_append_whose_confirmation_was_lost(poc):
    c, remote, *_ = poc
    c.start()
    remote.after_add = lambda: setattr(remote, "fail_read", True)
    with pytest.raises(RuntimeError, match="Read response lost"):
        play(c, "v3")
    remote.after_add = lambda: None
    play(c, "v4")
    assert remote.ids == ["v1", "v2", "v3", "v4", "v5"]
    assert remote.adds == [["v4"], ["v5"]]
    assert c.status()["ahead"] == 1


def test_stop_during_append_prevents_the_next_addition(poc):
    c, remote, *_ = poc
    c.start()
    remote.after_add = c.stop
    play(c, "v3")
    assert remote.ids == ["v1", "v2", "v3", "v4"]
    assert not c.status()["active"]


def test_external_edits_halt_monitor_without_overwriting_playlist(poc):
    c, remote, *_ = poc
    c.start()
    remote.ids.append("manual-song")
    c.ctx.bridge.now_playing = {"video_id": "v3", "playlist_ytm_id": "PLSESSION"}
    class ImmediateEvent(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()
    c.session.stop = ImmediateEvent()
    c._monitor(c.session)
    assert not c.status()["active"]
    assert "changed elsewhere" in c.status()["error"]
    assert remote.ids == ["v1", "v2", "v3", "manual-song"]
    assert remote.adds == []


def test_stop_preserves_playlist_and_prevents_future_appends(poc):
    c, remote, frames, *_ = poc
    c.start()
    assert c.stop()["ok"]
    play(c, "v2")
    assert remote.ids == ["v1", "v2", "v3"]
    assert len(frames) == 1
    assert not c.status()["active"]


def test_start_does_not_replace_running_session(poc):
    c, remote, _, workers, _ = poc
    assert c.start()["ok"]
    again = c.start()
    assert again["ok"] and again["joined"]
    assert len(workers) == 1
    assert remote.ids == ["v1", "v2", "v3"]


def test_start_requires_three_recommendations_before_creating_playlist(poc):
    c, remote, frames, workers, pool = poc
    del pool[2:]
    assert not c.start()["ok"]
    assert remote.ids == []
    assert frames == workers == []


@pytest.mark.parametrize("unavailable", ["extension", "client"])
def test_start_requires_connection_and_an_idle_existing_radio(poc, unavailable):
    c, remote, frames, workers, _ = poc
    if unavailable == "extension":
        c.ctx.bridge.connected = False
    elif unavailable == "client":
        c.ctx.client_provider = lambda: {}
    assert not c.start()["ok"]
    assert remote.ids == []
    assert frames == workers == []


def test_exhausted_catalog_does_not_repeat_songs(poc):
    c, remote, _, _, pool = poc
    del pool[3:]
    c.start()
    play(c, "v3")
    assert c.status()["exhausted"]
    assert remote.adds == []


def test_stop_during_start_prevents_navigation_and_monitor(poc, monkeypatch):
    c, remote, frames, workers, _ = poc
    create = executor.create_generated_playlist

    def stop_during_create(*args):
        result = create(*args)
        c.stop()
        return result

    monkeypatch.setattr(executor, "create_generated_playlist", stop_during_create)
    assert not c.start()["ok"]
    assert remote.ids == ["v1", "v2", "v3"]
    assert frames == workers == []


@pytest.mark.parametrize("disconnected", [False, True])
def test_monitor_exits_when_application_closes_or_extension_disconnects(poc, disconnected):
    c, *_ = poc
    c.start()
    play(c, "v1")
    assert c.active  # End the explicit-start navigation grace before disconnecting.
    if disconnected:
        c.ctx.bridge.connected = False
    else:
        c.work.close_admission()
    # Real event with an immediate first observation keeps the shutdown test fast.
    class ImmediateEvent(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()
    c.session.stop = ImmediateEvent()
    c._monitor(c.session)
    assert c.session.stop.is_set()
    assert not c.session.monitoring
    if disconnected:
        assert not c.status()["active"]


def test_routes_use_independent_playlist_session(poc, monkeypatch):
    c, remote, *_ = poc
    c.ctx.templates = Jinja2Templates(directory="src/yt_playlist/web/templates")
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        assert client.get("/radio/status").json()["active"] is False
        assert client.post("/radio/start").json()["ok"]
        play(c, "v3")
        assert client.get("/radio/status").json()["ahead"] == 1
        assert client.post("/radio/stop").json()["active"] is False
    assert not c.ctx.radio.active
    assert remote.ids == ["v1", "v2", "v3", "v4"]


def test_radio_page_is_registered_and_assets_are_served(store):
    from yt_playlist.web.app import create_app

    with TestClient(create_app(store, lambda: {}, background_work=False), base_url="http://127.0.0.1") as client:
        response = client.get("/#radio")
        assert response.status_code == 200
        assert response.url.fragment == "radio"
        assert client.get("/static/radio.js").status_code == 200
        assert not client.get("/radio/status").json()["active"]
