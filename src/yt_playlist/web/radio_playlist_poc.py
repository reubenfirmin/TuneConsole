"""Playlist-only radio experiment, separate from the existing radio controller.

Start with three songs in a new YouTube playlist; append enough songs to keep two
after the currently playing song. Never edit the browser's playback queue, prime
tracks, switch decks, or navigate again after the initial playlist launch.
"""
import logging
import random
import threading
import time
from dataclasses import dataclass, field

from yt_playlist.core.activity import activity_for
from yt_playlist.library import executor
from yt_playlist.rec import radio

logger = logging.getLogger(__name__)
TITLE = "Radio Session"
LOOKAHEAD = 2


@dataclass
class Session:
    client: object
    stop: threading.Event = field(default_factory=threading.Event)
    picks: object = field(default_factory=radio.RadioSession)
    playlist: str | None = None
    tracks: list = field(default_factory=list)
    position: int = -1
    pending: dict | None = None
    error: str | None = None
    exhausted: bool = False
    starting: bool = True
    library: list | None = None
    used_videos: set = field(default_factory=set)


class PlaylistRadio:
    def __init__(self, ctx):
        self.ctx = ctx
        self.work = activity_for(ctx)
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.session = None

    def status(self):
        with self.lock:
            s = self.session
            return {
                "active": bool(s and not s.stop.is_set()),
                "starting": bool(s and s.starting),
                "playlist_id": s.playlist if s else None,
                "playlist_url": (f"https://music.youtube.com/playlist?list={s.playlist}"
                                 if s and s.playlist else None),
                "position": s.position if s else -1,
                "ahead": max(0, len(s.tracks) - s.position - 1) if s and s.position >= 0 else None,
                "tracks": list(s.tracks) if s else [],
                "error": s.error if s else None,
                "exhausted": bool(s and s.exhausted),
                "connected": self.ctx.bridge.connected,
            }

    def stop(self):
        with self.lock:
            if self.session:
                self.session.stop.set()
        return {"ok": True, **self.status()}

    def _pick(self, s):
        pick = radio.pick_next(self.ctx.store, s.picks, self.ctx.now_fn())
        if pick is None:
            # The experiment must work without a built taste model. Fall back to
            # shuffled library songs rather than blocking playlist playback.
            if s.library is None:
                s.library = self.ctx.store.rec_query.library_songs()
                random.shuffle(s.library)
            reserved = radio._exclusions(s.picks)
            while s.library:
                candidate = s.library.pop()
                if (candidate.get("video_id") and candidate["key"] not in reserved
                        and candidate["video_id"] not in s.used_videos):
                    pick = candidate
                    break
        if pick is not None:
            # Reuse recommendation exclusions/artist caps as in-memory bookkeeping
            # for this playlist. This never sends a playback-queue command.
            s.picks.queue.append(pick)
            s.used_videos.add(pick["video_id"])
            radio.note_dispatch(s.picks, pick)
        return pick

    def _remote_ids(self, s):
        return [t.get("videoId") for t in s.client.get_playlist(s.playlist, limit=None)["tracks"]]

    def start(self):
        with self.operation:
            with self.lock:
                if self.session and not self.session.stop.is_set():
                    return {"ok": False, "reason": "A Radio Session is already running."}
                if getattr(getattr(self.ctx, "radio", None), "active", False):
                    return {"ok": False, "reason": "Stop the existing radio before starting this experiment."}
                identity, client = next(iter((self.ctx.client_provider() or {}).items()), (None, None))
                if client is None or not self.ctx.bridge.connected:
                    return {"ok": False, "reason": "Connect the YouTube Music extension first."}
                s = self.session = Session(client)
            try:
                seeds = []
                for _ in range(LOOKAHEAD + 1):
                    if s.stop.is_set():
                        return {"ok": False, "reason": "Stopped during start."}
                    pick = self._pick(s)
                    if pick is None:
                        raise ValueError("Fewer than three playable songs are available. Sync your YouTube Music library first.")
                    seeds.append(pick)
                if s.stop.is_set():
                    return {"ok": False, "reason": "Stopped during start."}
                result = executor.create_generated_playlist(
                    self.ctx.store, TITLE, seeds, client, self.ctx.now_fn(), identity)
                with self.lock:
                    s.playlist = result["new_ytm"]
                # Do not launch a partial playlist or assume that an API write succeeded.
                if self._remote_ids(s) != [t["video_id"] for t in seeds]:
                    raise ValueError("YouTube did not confirm all three starting songs. Start a new session to retry.")
                with self.lock:
                    s.tracks = seeds
                    if s.stop.is_set():
                        return {"ok": False, "reason": "Stopped during start."}
                    self.ctx.bridge.send_control({"type": "navigate", "url":
                        radio.playlist_watch_url(seeds[0]["video_id"], s.playlist)})
                    self.work.start_thread(self._monitor, args=(s,), name="radio-playlist-poc")
            except Exception as exc:
                logger.warning("Radio Session start failed", exc_info=True)
                with self.lock:
                    s.error = str(exc)
                    s.stop.set()
                return {"ok": False, "reason": str(exc), **self.status()}
            finally:
                with self.lock:
                    s.starting = False
            return {"ok": True, **self.status()}

    def _monitor(self, s):
        try:
            while not s.stop.wait(2):
                if not self.work.accepting or not self.ctx.bridge.connected:
                    break
                try:
                    self.advance(s)
                except Exception as exc:
                    logger.warning("Radio Session append failed", exc_info=True)
                    with self.lock:
                        s.error = str(exc)
        finally:
            s.stop.set()

    def advance(self, s):
        """Observe actual playback; append only to this session's saved playlist.

        Read back every addition. Keep an uncertain append pending so the next
        observation checks whether it landed before considering another write.
        """
        with self.operation:
            if s.stop.is_set():
                return
            np = self.ctx.bridge.now_playing or {}
            seen_at = getattr(self.ctx.bridge, "now_playing_seen_at", None)
            if (np.get("paused") or np.get("playlist_ytm_id") != s.playlist
                    or seen_at is None or time.monotonic() - seen_at > 8):
                return
            with self.lock:
                known = s.tracks + ([s.pending] if s.pending else [])
                position = next((i for i, t in enumerate(known)
                                 if t["video_id"] == np.get("video_id")), None)
                if position is None:
                    return
                s.position = position
            while not s.stop.is_set() and len(s.tracks) - position - 1 < LOOKAHEAD:
                if s.pending is None:
                    if s.exhausted:
                        return
                    s.pending = self._pick(s)
                    if s.pending is None:
                        with self.lock:
                            s.exhausted = True
                        return
                expected = [t["video_id"] for t in s.tracks]
                desired = expected + [s.pending["video_id"]]
                actual = self._remote_ids(s)
                if actual == expected:
                    if s.stop.is_set():
                        return
                    # One append, no automatic write retries. Read back even if the
                    # response is lost: YouTube may already have accepted the song.
                    try:
                        s.client.add_playlist_items(s.playlist, [s.pending["video_id"]])
                    except Exception:
                        logger.warning("Radio Session append response failed; checking playlist", exc_info=True)
                    actual = self._remote_ids(s)
                if actual == desired:
                    with self.lock:
                        s.tracks.append(s.pending)
                        s.pending = None
                        s.error = None
                elif actual == expected:
                    raise ValueError("YouTube has not confirmed the new song yet; retrying on the next observation.")
                else:
                    with self.lock:
                        s.stop.set()
                    raise ValueError("The playlist was changed outside this session. Start a new session to continue.")
