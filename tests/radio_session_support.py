"""Function-scoped radio controller fixture with deterministic playlist I/O."""
import json
import time
from types import SimpleNamespace

import pytest
from yt_playlist.library import playlist_generated
from yt_playlist.web import radio_session
from yt_playlist.web.radio_session import PlaylistRadio


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
        tid = store.tracks.upsert_track(track["video_id"], track["title"], track["artist"], "", None)
        track["key"] = store.tracks.catalog_subject(tid)["identity_key"]

    class Candidates:
        def __init__(self, store, now, tuning=None, *, now_source=None, comparison=None):
            self.comparison = getattr(now_source, 'comparison', comparison)
            self.tuning = radio_session.radio_tuning.controls(tuning or radio_session.radio_tuning.DEFAULTS)
            self.now_sampled_at = now
            self.encoding_model = store.settings.get_setting("rec_content_model")
            self.now_posterior, self.now_gain = {}, 0.
            self.by_video = {t["video_id"]: t for t in pool}
            self.by_key = {t["key"]: t for t in pool}
            self.fingerprint = "fixed-test-candidates"
            self.modes = {}

        def refresh_now(self, now):
            self.now_sampled_at = now

        def refresh_metadata(self, tracks):
            return False

        def fits(self, track, anchor=None):
            return True

        def retain(self, tracks):
            self.by_video.update((t["video_id"], t) for t in tracks)
            self.by_key.update((t["key"], t) for t in tracks)

        def cluster_for(self, track):
            return int(track["video_id"][1:]) // 8

        def similarity(self, a, b):
            return float(a["key"] == b["key"])

        def seed_offer(self, n=16):
            return pool[:n]

        def next_track(self, history, *, target=None, feedback=None, excluded=(), current=None):
            used = {t["video_id"] for t in history} | set(excluded)
            ordered = list(reversed(pool)) if self.tuning["comfort"] < .5 else pool
            return next((t for t in ordered if t["video_id"] not in used), None)

        def direction_anchor(self, history, anchors, *, excluded=(), feedback=None):
            return self.next_track(history + anchors, excluded=excluded)

    monkeypatch.setattr(radio_session, "RadioCandidates", Candidates)

    def create(store, title, tracks, client, now, identity):
        assert title.startswith("Radio Session · ")
        assert identity == store.identities.get_master_identity().id
        remote.ids = [t["video_id"] for t in tracks]
        bridge.native_ids = list(remote.ids)
        pid = store.playlists.upsert_playlist(identity, "PLSESSION", title, len(tracks), "", now)
        track_ids = store.tracks.track_ids_for_videos(remote.ids)
        store.playlists.set_playlist_tracks(pid, [track_ids[video] for video in remote.ids])
        return {"new_ytm": "PLSESSION", "pid": pid, "added": len(tracks)}

    monkeypatch.setattr(playlist_generated, "create_generated_playlist", create)
    yield controller, remote, frames, workers, pool
    controller.stop()
    controller.work.stop_executor()


def play(controller, video, playlist="PLSESSION", **extra):
    controller.ctx.bridge.now_playing = {"video_id": video, "playlist_ytm_id": playlist,
                                       "paused": False, "position": 30, "duration": 300, **extra}
    controller.ctx.bridge.now_playing_seen_at = time.monotonic()
    controller.listening.observe(controller.session)
    controller.advance(controller.session)
