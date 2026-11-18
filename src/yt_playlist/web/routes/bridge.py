"""Extension transport, browser-origin authentication, and playback HTTP endpoints.

The browser stamps extension origins; web pages cannot forge them. Saved-playlist
radio observes playback separately, so its writes never block this response pump.
"""
import asyncio
import json
import logging
import time

from anyio import CancelScope
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from starlette.concurrency import run_in_threadpool

from yt_playlist.core.activity import activity_for
from yt_playlist.controllers.playback_observations import player_snapshot as _now_playing_snapshot
from yt_playlist.web.context import Ctx

logger = logging.getLogger(__name__)
EXTENSION_ID = "opmmbolbdgdkphfocffpjpgmelkkjkcn"  # Chrome Web Store build
DEV_EXTENSION_ID = "edhplcadobipneepllhkkajckoammpnk"  # unpacked extension/ manifest key
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
EXTENSION_ORIGINS = {EXTENSION_ORIGIN, f"chrome-extension://{DEV_EXTENSION_ID}"}


def _capture_diagnostics(value):
    """Retain bounded sensor structure, never arbitrary page data or song/account values."""
    if not isinstance(value, dict):
        return None
    result = {key: item if type(item := value.get(key)) is bool else None for key in (
        "hasVideo", "accountKnown", "metadataVerified", "paused", "muted", "barHasVideo",
        "barMatchesVideo", "ratingMatchesVideo", "barHasTitle", "barHasArtist",
        "barTitleMatches", "barArtistMatches")}
    for key in ("barCandidates", "ratingCandidates"):
        count = value.get(key)
        result[key] = count if type(count) is int and 0 <= count <= 32 else None
    sources = value.get("sources")
    result["sources"] = []
    if isinstance(sources, list):
        allowed = {root + suffix for root in ("bar", "rating")
                   for suffix in (".data", ".polymerController.data", ".__data.data")}
        for source in sources[:6]:
            if (not isinstance(source, dict) or not isinstance(source.get("source"), str)
                    or source["source"] not in allowed):
                continue
            item = {"source": source["source"], "present": source.get("present") is True}
            for key in ("keys", "targetKeys"):
                keys = source.get(key)
                item[key] = [field[:32] for field in keys[:8] if isinstance(field, str)] \
                    if isinstance(keys, list) else []
            result["sources"].append(item)
    return result


def _record_connection(ctx):
    # Compatibility hook for independently assembled transports. The command
    # owns persistence, and the socket never waits here before receiving replies.
    return Ctx.bridge_connection(ctx).record()


def build(ctx, *, observations=None) -> APIRouter:
    router = APIRouter()
    bridge = ctx.bridge
    if observations is None:
        observations = Ctx.bridge_observations(ctx, connected=lambda: _record_connection(ctx))
    # Suspension runs in a worker. A new socket must not take ownership while
    # the previous owner's suspension is still changing the Radio controller.
    lifecycle_lock = asyncio.Lock()

    @router.get("/bridge/status")
    def bridge_status():
        if getattr(ctx, 'store', None) is None:
            return Ctx.playback_commands(ctx, monotonic=time.monotonic).status()
        with ctx.store.read_view() as reader:
            return Ctx.playback_commands(ctx, monotonic=time.monotonic, reader=reader).status()

    @router.post("/play")
    async def play(request: Request):
        try:
            url = (await request.json()).get("url") or ""
        except Exception:
            url = ""
        if not url.startswith("https://music.youtube.com/"):
            return {"ok": False, "error": "url must be a music.youtube.com link"}
        commands = Ctx.playback_commands(ctx)
        intent = commands.admit()
        return await activity_for(ctx).to_thread(commands.play, url, intent)

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
        try:
            result = await Ctx.playback_commands(ctx).rate(action, video_id, like_status)
        except Exception:
            logger.warning("rating failed: video=%s desired=%s", video_id, like_status, exc_info=True)
            return {"ok": False, "error": "Rating not confirmed. Check it in YouTube Music before trying again."}
        # Player or local-history trouble cannot undo the confirmed provider write.
        if result.get("player_error"):
            return {"ok": True, "error": "", "player_error":
                    "Rating saved. Some updates are not confirmed; check it in YouTube Music."}
        return {"ok": True, "error": ""}

    @router.post("/now-playing/next")
    async def now_playing_next(request: Request):
        video_id = None
        try:
            video_id = (await request.json()).get("videoId")
            if not isinstance(video_id, str) or not video_id:
                return {"ok": False, "error": "displayed video ID is required"}
            result = await Ctx.playback_commands(ctx).skip(video_id)
            if not result.get("ok"):
                logger.warning("skip not confirmed: video=%s reason=%s", video_id, result.get("error"))
            return result
        except Exception as exc:
            logger.warning("skip failed: video=%s", video_id, exc_info=True)
            return {"ok": False, "error": str(exc)}

    @router.post("/now-playing/toggle")
    async def now_playing_toggle(action: str = "toggle", resume: bool = False, videoId: str = "", confirm: bool = False):
        return await Ctx.playback_commands(ctx, monotonic=time.monotonic).toggle(action, resume, videoId, confirm)

    @router.websocket("/bridge/ws")
    async def bridge_ws(ws: WebSocket):
        if ws.headers.get("origin") not in EXTENSION_ORIGINS:
            logger.warning("bridge handshake rejected (origin %r)", ws.headers.get("origin"))
            await ws.close(code=1008)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        send_lock = asyncio.Lock()
        send_failed = asyncio.Event()

        async def send(frame):
            async with send_lock:
                try:
                    await ws.send_json(frame)
                except Exception:
                    send_failed.set()
                    raise

        async def receive():
            frame = asyncio.create_task(ws.receive_json())
            lost = asyncio.create_task(send_failed.wait())
            try:
                done, _ = await asyncio.wait({frame, lost}, return_when=asyncio.FIRST_COMPLETED)
                if lost in done:
                    raise WebSocketDisconnect()
                return frame.result()
            finally:
                frame.cancel()
                lost.cancel()
                # Preserve the original scope cancellation while joining both
                # children; another cancellation at gather can replace it with
                # an unscoped CancelledError and leak through ASGI shutdown.
                with CancelScope(shield=True):
                    await asyncio.gather(frame, lost, return_exceptions=True)

        async def keepalive():
            # MV3 service workers otherwise sleep after about 30 seconds without messages.
            try:
                while True:
                    await asyncio.sleep(20)
                    await send({"ping": 1})
            except Exception:
                pass

        async with lifecycle_lock:
            conn_id = bridge.connect(send, loop)
        ping_task = asyncio.create_task(keepalive())
        store = getattr(ctx, "store", None)
        try:
            observations.connection()
            logger.info("extension bridge connected")
            # A concurrent command/keepalive can fail while pairing or play
            # persistence runs off-thread. Starlette then marks the socket
            # disconnected before receive_json can observe a disconnect frame.
            while ws.application_state == WebSocketState.CONNECTED:
                msg = await receive()
                # No await separates this check from presence updates or reply
                # resolution. Only this event loop installs socket owners.
                if bridge.connection_id != conn_id:
                    break
                if not isinstance(msg, dict):
                    logger.warning("malformed bridge frame, ignoring: %r", msg)
                    continue
                kind = msg.get("type")
                if kind == "rating-reconciliation":
                    state = msg.get("state")
                    if state not in ("pending", "recovered", "cancelled", "failed"):
                        continue
                    # One pending frame and one terminal frame per deferred command. API success
                    # remains independent; preserve versions and first/last binding evidence here.
                    log = logger.warning if state == "failed" else logger.info
                    log("native rating reconciliation %s: %s", state,
                        json.dumps({key: msg.get(key) for key in (
                            "ratingId", "videoId", "desiredStatus", "attempts", "elapsedMs", "error", "first", "last")},
                            ensure_ascii=True)[:8000])
                    continue
                if kind == "sensor-health":
                    try:
                        counts = {key: max(0, int(msg.get(field) or 0)) for key, field in (
                            ("ytm_tabs", "ytmTabs"), ("responding_tabs", "respondingTabs"),
                            ("reinjected_tabs", "reinjectedTabs"), ("reloaded_tabs", "reloadedTabs"))}
                    except (TypeError, ValueError):
                        logger.warning("malformed sensor-health frame, ignoring: %r", msg)
                        continue
                    bridge.playback_confirmation = msg.get("playbackConfirmation") is True
                    bridge.account_bound_next = msg.get('accountBoundNext') is True
                    bridge.sensor_health = {**counts, "healthy": bool(msg.get("healthy")),
                        "recovering": bool(msg.get("recovering")), "error": str(msg.get("error") or "")[:500]}
                    if counts["ytm_tabs"] == 0:
                        bridge.resumable_player = None
                    versions = msg.get("versions")
                    if isinstance(versions, list):
                        bridge.sensor_health["versions"] = [
                            {"ok": bool(item.get("ok")), **{
                                key: value if type(value := item.get(key)) is int and 0 <= value < 2**53 else None
                                for key in ("tabId", "receiverRevision", "mainVersion", "mainRevision", "mainAgeMs")},
                             "capture": _capture_diagnostics(item.get("capture"))}
                            for item in versions[:8] if isinstance(item, dict)]
                    continue
                if kind == "player-closed":
                    bridge.now_playing = bridge.resumable_player = None
                    bridge.now_playing_seen_at = None
                    continue
                if kind == "now-heartbeat":
                    if msg.get("title"):
                        bridge.now_playing = _now_playing_snapshot(msg, bridge.now_playing)
                        bridge.resumable_player = dict(bridge.now_playing)
                    if bridge.now_playing is not None:
                        bridge.now_playing_seen_at = time.monotonic()
                    if msg.get("title"):
                        observations.prepare_radio(bridge.now_playing or {}, msg)
                    continue
                if kind == "play":
                    bridge.now_playing = _now_playing_snapshot(msg)
                    bridge.resumable_player = dict(bridge.now_playing)
                    bridge.now_playing_seen_at = time.monotonic()
                    if (msg.get("paused") or msg.get("muted") or msg.get("tabMuted")
                            or msg.get("volume") == 0):
                        continue
                    observations.prepare_radio(bridge.now_playing or {}, msg)
                    now = getattr(ctx, "now_fn", time.time)()
                    if store is not None:
                        try:
                            observations.receive('live', msg, now)
                        except Exception:
                            logger.warning("failed to persist play event", exc_info=True)
                    continue
                if kind == "pevent":
                    now = getattr(ctx, "now_fn", time.time)()
                    if msg.get("kind") == "state":
                        for player in (bridge.now_playing, bridge.resumable_player):
                            if player is not None:
                                sv, nv = msg.get("videoId"), player.get("video_id")
                                if not sv or not nv or sv == nv:
                                    player["paused"] = msg.get("state") == "paused"
                    elif msg.get("kind") == "bye":
                        bridge.now_playing = None
                        bridge.resumable_player = None
                        bridge.now_playing_seen_at = None
                    if store is not None:
                        try:
                            observations.receive('player', msg, now)
                        except Exception:
                            logger.warning("failed to persist player event", exc_info=True)
                    continue
                try:
                    req_id, status, body = int(msg["id"]), int(msg["status"]), msg["body"]
                except (KeyError, ValueError, TypeError):
                    logger.warning("malformed bridge frame, ignoring: %r", msg)
                    continue
                bridge.resolve(req_id, status, body, source_timezone=msg.get("timeZone"),
                               response_headers=msg.get("headers"))
        except WebSocketDisconnect:
            pass
        finally:
            ping_task.cancel()
            # Socket cancellation must not skip suspension or release ownership
            # while a replacement is waiting to use the Radio controller.
            with CancelScope(shield=True):
                await asyncio.gather(ping_task, return_exceptions=True)
                async with lifecycle_lock:
                    if bridge.disconnect(conn_id):
                        try:
                            await run_in_threadpool(observations.retire)
                        except Exception:
                            logger.warning("failed to stop Radio Session on disconnect", exc_info=True)
                        logger.info("extension bridge disconnected")

    return router
