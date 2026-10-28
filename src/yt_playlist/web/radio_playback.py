"""Native playback observation/control. No session or playlist mutation policy."""
import asyncio
import json
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from yt_playlist.core.bridge import BridgeError


class PlaybackUnavailable(Exception):
    pass


@dataclass(frozen=True)
class PlaybackRequest:
    playlist: str
    video_ids: tuple[str, ...]
    refresh: bool = False


@dataclass(frozen=True)
class PlaybackObservation:
    state: dict
    seen_at: float

    def player(self, playlist: str, *, allow_paused=False):
        player = self.state.get("player") or {}
        if ((player.get("paused") and not allow_paused) or player.get("playlist_ytm_id") != playlist
                or time.monotonic() - self.seen_at > 8):
            return None
        return player


class RadioPlayback:
    def __init__(self, bridge):
        self.bridge = bridge

    def current(self):
        playing = self.bridge.now_playing or {}
        seen = getattr(self.bridge, "now_playing_seen_at", None)
        if (not self.bridge.connected or seen is None or time.monotonic() - seen > 8
                or playing.get("paused") or not playing.get("video_id")):
            return {}
        return playing

    def observe(self, request: PlaybackRequest) -> PlaybackObservation:
        payload = {"playlist": request.playlist, "videoIds": list(request.video_ids), "refresh": request.refresh}
        try:
            status, body = asyncio.run(self.bridge.request_control({"type": "radio-session", **payload}))
            result = json.loads(body)
            if status == 0 and result.get("error") == "blocked by allowlist":
                status, body = asyncio.run(self.bridge.request_control({"type": "playlist-session-poc", **payload}))
                result = json.loads(body)
            if status != 200 or not result.get("ok"):
                raise PlaybackUnavailable(result.get("error") or f"observer response {status}")
        except (BridgeError, ValueError) as exc:
            raise PlaybackUnavailable(str(exc)) from exc
        return PlaybackObservation(result, time.monotonic())

    def navigate(self, playlist: str, video: str, *, intent=None):
        return self.bridge.send_control({"type": "navigate", "url":
            "https://music.youtube.com/watch?" + urlencode({"v": video, "list": playlist})}, intent=intent)

    def next(self, video: str):
        try:
            status, body = asyncio.run(self.bridge.request_control({"type": "next", "videoId": video}, timeout=10))
            return status, json.loads(body)
        except (BridgeError, ValueError) as exc:
            return 0, {"error": str(exc)}
