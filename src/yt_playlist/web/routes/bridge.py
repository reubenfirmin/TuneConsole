"""Extension transport, browser-origin authentication, and playback HTTP endpoints.

The browser stamps extension origins; web pages cannot forge them. Saved-playlist
radio observes playback separately, so its writes never block this response pump.
"""
import asyncio
import json
import logging
import time
from urllib.parse import urlencode, urlparse, parse_qs

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from yt_playlist.core.activity import activity_for
from yt_playlist.library import live_plays, player_events, ratings
from yt_playlist.rec import measurement

logger = logging.getLogger(__name__)
EXTENSION_ID = "opmmbolbdgdkphfocffpjpgmelkkjkcn"  # Chrome Web Store build
DEV_EXTENSION_ID = "edhplcadobipneepllhkkajckoammpnk"  # unpacked extension/ manifest key
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
EXTENSION_ORIGINS = {EXTENSION_ORIGIN, f"chrome-extension://{DEV_EXTENSION_ID}"}


def _now_playing_snapshot(msg, previous=None):
    snapshot = {"title": msg.get("title"), "artist": msg.get("artist"),
                "thumbnail": msg.get("thumbnail"), "likeStatus": msg.get("likeStatus"),
                "video_id": msg.get("videoId"), "paused": bool(msg.get("paused"))}
    # Old sensors omit context on heartbeats. Preserve it only for the same video.
    for field, key in (("playlist", "playlist_ytm_id"), ("albumBrowseId", "album_browse_id")):
        if field in msg:
            snapshot[key] = msg.get(field) or ""
        elif previous and previous.get("video_id") == snapshot["video_id"] and key in previous:
            snapshot[key] = previous[key]
    return snapshot


def build(ctx) -> APIRouter:
    router = APIRouter()
    bridge = ctx.bridge

    @router.get("/bridge/status")
    def bridge_status():
        radio = getattr(ctx, "radio", None)
        now_playing = bridge.now_playing
        seen_at = getattr(bridge, "now_playing_seen_at", None)
        if now_playing is not None and seen_at is not None and time.monotonic() - seen_at > 8:
            bridge.now_playing = now_playing = None
            bridge.now_playing_seen_at = None
        if now_playing is not None:
            store = getattr(ctx, "store", None)
            if getattr(store, "tracks", None) is not None:
                rating = ratings.main_rating(store, now_playing.get("video_id"))
                if rating:
                    now_playing = {**now_playing, "likeStatus": rating["status"]}
            collection = getattr(getattr(ctx, "store", None), "collection", None)
            resolve = getattr(collection, "playback_source", None)
            source = resolve(now_playing.get("playlist_ytm_id"), now_playing.get("video_id"),
                             now_playing.get("album_browse_id")) if resolve else None
            if source:
                url = (f"/playlist/{source['id']}" if source["kind"] == "playlist" else
                       "/album?" + urlencode({"browse": source["browse"]}))
                source = {"kind": source["kind"], "title": source["title"], "url": url, "external": False}
            now_playing = {**now_playing, "source": source}
        return {"connected": bridge.connected, "now_playing": now_playing,
                "sensor_health": getattr(bridge, "sensor_health", None),
                "radio": bool(radio is not None and radio.active)}

    @router.post("/play")
    async def play(request: Request):
        try:
            url = (await request.json()).get("url") or ""
        except Exception:
            url = ""
        if not url.startswith("https://music.youtube.com/"):
            return {"ok": False, "error": "url must be a music.youtube.com link"}
        try:
            bridge.send_control({"type": "navigate", "url": url})
            query = parse_qs(urlparse(url).query)
            video_id = (query.get("v") or [None])[0]
            if video_id:
                measurement.record_action(ctx.store, "start", ctx.now_fn(), video_id=video_id)
            elif query.get("list"):
                playlist = query["list"][0].removeprefix("RDAMPL")
                for pl in ctx.store.playlists.get_playlists():
                    if pl.ytm_playlist_id == playlist:
                        measurement.record_action(ctx.store, "start", ctx.now_fn(), playlist_id=pl.id)
                        break
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/now-playing/rate")
    async def now_playing_rate(request: Request):
        try:
            data = await request.json()
            action, video_id = data.get("action"), data.get("videoId")
            like_status = data.get("likeStatus")
        except Exception:
            action = video_id = None
            like_status = None
        if action not in ("like", "dislike"):
            return {"ok": False, "error": "action must be 'like' or 'dislike'"}
        if not isinstance(video_id, str) or not video_id:
            return {"ok": False, "error": "displayed video ID is required"}
        if like_status is not None and like_status not in ("LIKE", "DISLIKE", "INDIFFERENT"):
            return {"ok": False, "error": "invalid rating state"}
        if like_status is None:
            # Compatibility with older pages that sent a toggle instead of the desired state.
            rating = ratings.main_rating(ctx.store, video_id)
            playing = bridge.now_playing or {}
            current = rating["status"] if rating else (
                playing.get("likeStatus") if playing.get("video_id") == video_id else None)
            want = "LIKE" if action == "like" else "DISLIKE"
            like_status = "INDIFFERENT" if current == want else want
        try:
            logger.info("rating requested: video=%s action=%s desired=%s connected=%s",
                        video_id, action, like_status, bridge.connected)
            # ytmusicapi performs the write through the authenticated extension transport.
            # Run it off the event loop so that transport's WebSocket replies can be received.
            await activity_for(ctx).to_thread(ctx.ops().set_rating, video_id, like_status)
            if like_status == "LIKE":
                measurement.record_action(ctx.store, "save", ctx.now_fn(), video_id=video_id)
            logger.info("rating confirmed: video=%s desired=%s", video_id, like_status)
            return {"ok": True, "error": ""}
        except Exception as exc:
            logger.warning("rating failed: video=%s desired=%s", video_id, like_status, exc_info=True)
            return {"ok": False, "error": str(exc)}

    @router.post("/now-playing/toggle")
    async def now_playing_toggle():
        try:
            bridge.send_control({"type": "playpause"})
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.websocket("/bridge/ws")
    async def bridge_ws(ws: WebSocket):
        if ws.headers.get("origin") not in EXTENSION_ORIGINS:
            logger.warning("bridge handshake rejected (origin %r)", ws.headers.get("origin"))
            await ws.close(code=1008)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        send_lock = asyncio.Lock()

        async def send(frame):
            async with send_lock:
                await ws.send_json(frame)

        async def keepalive():
            # MV3 service workers otherwise sleep after about 30 seconds without messages.
            try:
                while True:
                    await asyncio.sleep(20)
                    await send({"ping": 1})
            except Exception:
                pass

        conn_id = bridge.connect(send, loop)
        ping_task = asyncio.create_task(keepalive())
        store = getattr(ctx, "store", None)
        if store is not None:
            store.settings.set_setting("bridge_paired", "1")
        clear = getattr(ctx, "clear_all_auth_expired", None)
        if callable(clear):
            clear()
        logger.info("extension bridge connected")
        try:
            while True:
                msg = await ws.receive_json()
                if not isinstance(msg, dict):
                    logger.warning("malformed bridge frame, ignoring: %r", msg)
                    continue
                kind = msg.get("type")
                if kind == "sensor-health":
                    try:
                        counts = {key: max(0, int(msg.get(field) or 0)) for key, field in (
                            ("ytm_tabs", "ytmTabs"), ("responding_tabs", "respondingTabs"),
                            ("reinjected_tabs", "reinjectedTabs"), ("reloaded_tabs", "reloadedTabs"))}
                    except (TypeError, ValueError):
                        logger.warning("malformed sensor-health frame, ignoring: %r", msg)
                        continue
                    bridge.sensor_health = {**counts, "healthy": bool(msg.get("healthy")),
                        "recovering": bool(msg.get("recovering")), "error": str(msg.get("error") or "")[:500]}
                    continue
                if kind == "now-heartbeat":
                    if msg.get("title"):
                        bridge.now_playing = _now_playing_snapshot(msg, bridge.now_playing)
                    if bridge.now_playing is not None:
                        bridge.now_playing_seen_at = time.monotonic()
                    continue
                if kind == "play":
                    bridge.now_playing = _now_playing_snapshot(msg)
                    bridge.now_playing_seen_at = time.monotonic()
                    if (msg.get("paused") or msg.get("muted") or msg.get("tabMuted")
                            or msg.get("volume") == 0):
                        continue
                    now = getattr(ctx, "now_fn", time.time)()
                    if store is not None:
                        try:
                            await activity_for(ctx).to_thread(live_plays.handle_play_event, ctx, msg, now)
                        except Exception:
                            logger.warning("failed to persist play event", exc_info=True)
                    continue
                if kind == "pevent":
                    now = getattr(ctx, "now_fn", time.time)()
                    if msg.get("kind") == "state" and bridge.now_playing is not None:
                        sv, nv = msg.get("videoId"), bridge.now_playing.get("video_id")
                        if not sv or not nv or sv == nv:
                            bridge.now_playing["paused"] = msg.get("state") == "paused"
                    elif msg.get("kind") == "bye":
                        bridge.now_playing = None
                        bridge.now_playing_seen_at = None
                    if store is not None:
                        try:
                            await activity_for(ctx).to_thread(player_events.handle_player_event, ctx, msg, now)
                        except Exception:
                            logger.warning("failed to persist player event", exc_info=True)
                    continue
                try:
                    req_id, status, body = int(msg["id"]), int(msg["status"]), msg["body"]
                except (KeyError, ValueError, TypeError):
                    logger.warning("malformed bridge frame, ignoring: %r", msg)
                    continue
                bridge.resolve(req_id, status, body, source_timezone=msg.get("timeZone"))
        except WebSocketDisconnect:
            pass
        finally:
            ping_task.cancel()
            bridge.disconnect(conn_id)
            bridge.now_playing = None
            bridge.now_playing_seen_at = None
            bridge.sensor_health = None
            try:
                if getattr(ctx, "radio", None):
                    getattr(ctx.radio, "suspend", ctx.radio.stop)()
            except Exception:
                logger.warning("failed to stop Radio Session on disconnect", exc_info=True)
            logger.info("extension bridge disconnected")

    return router
