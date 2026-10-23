"""Extension transport, browser-origin authentication, and playback HTTP endpoints.

The browser stamps extension origins; web pages cannot forge them. Saved-playlist
radio observes playback separately, so its writes never block this response pump.
"""
import asyncio
import json
import logging
import math
import time
from urllib.parse import urlencode, urlparse, parse_qs

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

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
                "video_id": msg.get("videoId"), "paused": bool(msg.get("paused")),
                # Presence can come from an unverified heartbeat. Keep the producer's
                # exact tri-state available for diagnostics without treating it as a play.
                "metadata_verified": msg.get("metadataVerified")
                if type(msg.get("metadataVerified")) is bool else None}
    for key in ('position', 'duration'):
        value = msg.get(key)
        snapshot[key] = value if (type(value) in (int, float) and math.isfinite(value)
                                  and (value >= 0 if key == 'position' else value > 0)) else None
    # Old sensors omit context on heartbeats. Preserve it only for the same video.
    for field, key in (("playlist", "playlist_ytm_id"), ("albumBrowseId", "album_browse_id")):
        if field in msg:
            snapshot[key] = msg.get(field) or ""
        elif previous and previous.get("video_id") == snapshot["video_id"] and key in previous:
            snapshot[key] = previous[key]
    return snapshot


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


def build(ctx) -> APIRouter:
    router = APIRouter()
    bridge = ctx.bridge
    # Suspension runs in a worker. A new socket must not take ownership while
    # the previous owner's suspension is still changing the Radio controller.
    lifecycle_lock = asyncio.Lock()

    def prepare_radio(msg):
        playing = bridge.now_playing or {}
        if (not playing.get("video_id") or not playing.get("playlist_ytm_id")
                or playing.get("paused") or msg.get("muted") or msg.get("tabMuted")
                or msg.get("volume") == 0 or msg.get("metadataVerified") is False):
            return
        radio = getattr(ctx, "radio", None)
        if radio is not None:
            try:
                # Only schedule work here. Joining may need authenticated reads
                # whose replies must continue through this same receive loop.
                radio.prepare()
            except Exception:
                logger.debug("Radio reattachment deferred", exc_info=True)

    @router.get("/bridge/status")
    def bridge_status():
        radio = getattr(ctx, "radio", None)
        # The persisted session can already be playing before the radio panel has been opened
        # and its controller reattached. Use the same snapshot for attribution and the tab light.
        session = radio.bootstrap()["state"] if radio is not None else {}
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
                # Sensors can verify playback before album artwork becomes available.
                # Resolve the exact upload without borrowing the previous song's image.
                if not now_playing.get("thumbnail") and now_playing.get("video_id"):
                    thumbnail = store.tracks.thumbnail_for_video(now_playing["video_id"])
                    if thumbnail:
                        now_playing = {**now_playing, "thumbnail": thumbnail}
            collection = getattr(getattr(ctx, "store", None), "collection", None)
            resolve = getattr(collection, "playback_source", None)
            source = resolve(now_playing.get("playlist_ytm_id"), now_playing.get("video_id"),
                             now_playing.get("album_browse_id")) if resolve else None
            if source:
                url = (f"/playlist/{source['id']}" if source["kind"] == "playlist" else
                       "/album?" + urlencode({"browse": source["browse"]}))
                source = {"kind": source["kind"], "title": source["title"], "url": url, "external": False}
            if session.get("active") and session.get("playlist_id") == now_playing.get("playlist_ytm_id"):
                source = {"kind": "radio", "title": session["title"], "url": "/#radio", "external": False}
            now_playing = {**now_playing, "source": source,
                           "snapshot_age": max(0, time.monotonic() - seen_at) if seen_at is not None else 0}
        return {"connected": bridge.connected, "now_playing": now_playing,
                "sensor_health": getattr(bridge, "sensor_health", None),
                "radio": bool(session.get("active")),
                "radio_ready": bool(session.get("active") and not session.get("starting"))}

    @router.post("/play")
    async def play(request: Request):
        try:
            url = (await request.json()).get("url") or ""
        except Exception:
            url = ""
        if not url.startswith("https://music.youtube.com/"):
            return {"ok": False, "error": "url must be a music.youtube.com link"}
        return await activity_for(ctx).to_thread(_play, url)

    def _play(url):
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
            rating = await asyncio.to_thread(ratings.main_rating, ctx.store, video_id)
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
            loop = asyncio.get_running_loop()

            def mirror(identity_id):
                # The admitted worker owns the complete command even if its HTTP waiter leaves.
                # Keep its per-item turn until native reconciliation and exact-track skip settle.
                return asyncio.run_coroutine_threadsafe(
                    mirror_rating(action, video_id, like_status, identity_id), loop).result()

            result = await activity_for(ctx).to_thread(ctx.ops().set_rating, video_id, like_status,
                                                       after_confirmed=mirror)
            logger.info("rating confirmed: video=%s desired=%s", video_id, like_status)
        except Exception as exc:
            logger.warning("rating failed: video=%s desired=%s", video_id, like_status, exc_info=True)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "error": "", **{k: v for k, v in result.items() if k == "player_error"}}

    async def mirror_rating(action, video_id, like_status, identity_id):
        if like_status == "LIKE":
            await asyncio.to_thread(measurement.record_action, ctx.store, "save", ctx.now_fn(), video_id=video_id)
        # The API confirms the exact upload/account even if playback has moved on. When it
        # is still playing, also drive Music's native control so its selected thumb follows
        # the saved rating. Radio repairs its saved future before advancing.
        playing = bridge.now_playing or {}
        if bridge.connected and playing.get("video_id") == video_id:
            player_errors = []
            native_diagnostics = None
            try:
                identities = await asyncio.to_thread(ctx.store.identities.get_identities)
                identity = next(i for i in identities if i.id == identity_id)
                status, body = await bridge.request_control({
                    "type": "rate", "action": action, "videoId": video_id,
                    "likeStatus": like_status, "brandId": identity.brand_account_id or "",
                    "skip": False}, timeout=10)
                result = json.loads(body)
                native_diagnostics = result.get("diagnostics")
                if status != 200 or not result.get("ok"):
                    raise ValueError(result.get("error") or "Player command failed")
            except Exception as exc:
                logger.warning("rating saved but native thumb update failed: video=%s diagnostics=%s",
                               video_id, json.dumps(native_diagnostics, ensure_ascii=True)[:4000], exc_info=True)
                player_errors.append(str(exc))
            # A missing native rating control must not prevent the requested
            # skip after the exact song's dislike has already been saved.
            if like_status == "DISLIKE":
                try:
                    skipped = await skip_song(video_id)
                    if not skipped.get("ok"):
                        raise ValueError(skipped.get("error") or "The player did not advance")
                except Exception as exc:
                    logger.warning("rating saved but skip failed: video=%s", video_id, exc_info=True)
                    player_errors.append(str(exc))
            if player_errors:
                return {"ok": True, "error": "", "player_error": " ".join(player_errors)}
        return {"ok": True, "error": ""}

    async def skip_song(video_id):
        radio = getattr(ctx, "radio", None)
        if radio is not None:
            result = await activity_for(ctx).to_thread(radio.skip, video_id)
            if result is not None:
                return result
        status, body = await bridge.request_control({"type": "next", "videoId": video_id}, timeout=10)
        result = json.loads(body)
        if status != 200 or not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "The player did not advance"}
        return {"ok": True}

    @router.post("/now-playing/next")
    async def now_playing_next(request: Request):
        video_id = None
        try:
            video_id = (await request.json()).get("videoId")
            if not isinstance(video_id, str) or not video_id:
                return {"ok": False, "error": "displayed video ID is required"}
            result = await skip_song(video_id)
            if not result.get("ok"):
                logger.warning("skip not confirmed: video=%s reason=%s", video_id, result.get("error"))
            return result
        except Exception as exc:
            logger.warning("skip failed: video=%s", video_id, exc_info=True)
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

        async with lifecycle_lock:
            conn_id = bridge.connect(send, loop)
        ping_task = asyncio.create_task(keepalive())
        store = getattr(ctx, "store", None)
        try:
            if store is not None:
                await run_in_threadpool(store.settings.set_setting, "bridge_paired", "1")
            clear = getattr(ctx, "clear_all_auth_expired", None)
            if callable(clear):
                await run_in_threadpool(clear)
            logger.info("extension bridge connected")
            while True:
                msg = await ws.receive_json()
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
                    bridge.sensor_health = {**counts, "healthy": bool(msg.get("healthy")),
                        "recovering": bool(msg.get("recovering")), "error": str(msg.get("error") or "")[:500]}
                    versions = msg.get("versions")
                    if isinstance(versions, list):
                        bridge.sensor_health["versions"] = [
                            {"ok": bool(item.get("ok")), **{
                                key: value if type(value := item.get(key)) is int and 0 <= value < 2**53 else None
                                for key in ("tabId", "receiverRevision", "mainVersion", "mainRevision", "mainAgeMs")},
                             "capture": _capture_diagnostics(item.get("capture"))}
                            for item in versions[:8] if isinstance(item, dict)]
                    continue
                if kind == "now-heartbeat":
                    if msg.get("title"):
                        bridge.now_playing = _now_playing_snapshot(msg, bridge.now_playing)
                    if bridge.now_playing is not None:
                        bridge.now_playing_seen_at = time.monotonic()
                    if msg.get("title"):
                        prepare_radio(msg)
                    continue
                if kind == "play":
                    bridge.now_playing = _now_playing_snapshot(msg)
                    bridge.now_playing_seen_at = time.monotonic()
                    if (msg.get("paused") or msg.get("muted") or msg.get("tabMuted")
                            or msg.get("volume") == 0):
                        continue
                    prepare_radio(msg)
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
            async with lifecycle_lock:
                if bridge.disconnect(conn_id):
                    try:
                        radio = getattr(ctx, "radio", None)
                        if radio is not None:
                            suspend = getattr(radio, "suspend", None) or radio.stop
                            await run_in_threadpool(suspend)
                    except Exception:
                        logger.warning("failed to stop Radio Session on disconnect", exc_info=True)
                    logger.info("extension bridge disconnected")

    return router
