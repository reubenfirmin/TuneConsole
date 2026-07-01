"""WebSocket endpoint the browser extension connects to.

Authentication is by ORIGIN, not a shared token: the browser stamps every extension-initiated
WebSocket handshake with `Origin: chrome-extension://<id>`, and a web page cannot forge that header.
So we accept the socket only when it comes from our pinned extension id, which makes pairing seamless
(install the extension and it connects, nothing to paste) while still rejecting any local web page
that tries to drive the bridge. No credential ever crosses this socket in either direction."""
import asyncio
import logging
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# The extension id is pinned by the `key` field in extension/manifest.json, so it is stable across
# machines and installs. Only this origin may open the bridge socket.
EXTENSION_ID = "edhplcadobipneepllhkkajckoammpnk"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"


def build(ctx) -> APIRouter:
    router = APIRouter()
    bridge = ctx.bridge

    @router.get("/bridge/status")
    def bridge_status():
        return {"connected": bridge.connected, "now_playing": bridge.now_playing}

    @router.post("/play")
    async def play(request: Request):
        # Play a YouTube Music URL by swapping the existing YTM tab (in the background) via the
        # extension, instead of opening a new tab. Any app play link routes through here.
        try:
            url = (await request.json()).get("url") or ""
        except Exception:  # noqa: BLE001
            url = ""
        if not url.startswith("https://music.youtube.com/"):
            return {"ok": False, "error": "url must be a music.youtube.com link"}
        try:
            bridge.send_control({"type": "navigate", "url": url})
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 - extension not connected; UI falls back to opening it
            return {"ok": False, "error": str(e)}

    @router.post("/now-playing/rate")
    async def now_playing_rate(request: Request):
        # Like/dislike the currently-playing track by asking the extension to drive YTM's own control.
        try:
            action = (await request.json()).get("action")
        except Exception:  # noqa: BLE001
            action = None
        if action not in ("like", "dislike"):
            return {"ok": False, "error": "action must be 'like' or 'dislike'"}
        try:
            bridge.send_control({"type": "rate", "action": action})
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 - extension not connected, surface it to the UI
            return {"ok": False, "error": str(e)}

    @router.websocket("/bridge/ws")
    async def bridge_ws(ws: WebSocket):
        # Origin is set by the browser and unspoofable by page scripts, so it is a sound gate.
        if ws.headers.get("origin") != EXTENSION_ORIGIN:
            logger.warning("bridge handshake rejected (origin %r)", ws.headers.get("origin"))
            await ws.close(code=1008)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        send_lock = asyncio.Lock()

        async def send(frame):
            # Serialize sends: the bridge request path and the keepalive pinger both write here.
            async with send_lock:
                await ws.send_json(frame)

        async def keepalive():
            # An MV3 service worker sleeps after ~30s idle, which drops this socket (and then every
            # backend write fails with "no extension connected"). Incoming message activity resets
            # that timer, so ping every 20s to keep the extension and the pipe alive.
            try:
                while True:
                    await asyncio.sleep(20)
                    async with send_lock:
                        await ws.send_json({"ping": 1})
            except Exception:  # noqa: BLE001 - a closing socket races the sleep; disconnect handles it
                pass

        conn_id = bridge.connect(send, loop)
        ping_task = asyncio.create_task(keepalive())
        # A live pairing is the credential now (see Runtime.credentials_present), so record it as
        # soon as our extension connects. Guard for a store being present since some tests build a
        # bare ctx without one.
        store = getattr(ctx, "store", None)
        if store is not None:
            store.set_setting("bridge_paired", "1")
        # A live extension means a live session, so clear any stale "not signed in" flags (they may
        # have been set earlier when the extension was merely disconnected). A genuine signed-out
        # state re-flags on the next sync attempt. Guard: some tests build a bare ctx.
        clear = getattr(ctx, "clear_all_auth_expired", None)
        if callable(clear):
            clear()
        logger.info("extension bridge connected")
        try:
            while True:
                msg = await ws.receive_json()
                # The extension can push unsolicited events (not replies to a request). A play
                # notification carries what is currently playing in the YouTube Music tab.
                if isinstance(msg, dict) and msg.get("type") == "play":
                    logger.info("Received play notification: %s by %s",
                                msg.get("title") or "?", msg.get("artist") or "?")
                    # Surface it for the Home now-playing line (polled via GET /bridge/status).
                    bridge.now_playing = {"title": msg.get("title"), "artist": msg.get("artist"),
                                          "thumbnail": msg.get("thumbnail"),
                                          "likeStatus": msg.get("likeStatus"),
                                          "video_id": msg.get("videoId")}
                    continue
                try:
                    req_id = int(msg["id"])
                    status = int(msg["status"])
                    body = msg["body"]
                except (KeyError, ValueError, TypeError):
                    logger.warning("malformed bridge frame, ignoring: %r", msg)
                    continue
                bridge.resolve(req_id, status, body)
        except WebSocketDisconnect:
            pass
        finally:
            ping_task.cancel()
            bridge.disconnect(conn_id)
            bridge.now_playing = None      # nothing is playing once the extension is gone
            logger.info("extension bridge disconnected")

    return router
