"""User-initiated desktop updates and a graceful handoff to the installed version."""
import asyncio
import os
import signal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from yt_playlist.core.update_lifecycle import PendingRestart


async def _stop_server():
    # Let the restart response reach the browser before uvicorn closes its listener.
    await asyncio.sleep(0.3)
    os.kill(os.getpid(), signal.SIGTERM)


def build(ctx):
    router = APIRouter()
    ctx.pending_restart = pending = PendingRestart(ctx, _stop_server)

    def updater_for(request):
        updater = ctx.updater
        if updater is None:
            raise HTTPException(404, "In-app updates are available in packaged desktop installations.")
        # These operations install code and stop the server. Require the exact origin,
        # including the port, in addition to the application's general loopback guard.
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                source, target = urlsplit(origin), urlsplit(str(request.url))
                if (source.scheme, source.netloc) != (target.scheme, target.netloc):
                    raise HTTPException(403, "cross-origin request blocked")
        return updater

    def response(data, status_code=200):
        return JSONResponse(data, status_code=status_code, headers={"Cache-Control": "no-store"})

    @router.get("/app-update/status")
    async def status(request: Request):
        return response(pending.snapshot(await updater_for(request).status()))

    @router.post("/app-update/install")
    async def install(request: Request):
        updater = updater_for(request)
        if pending.phase is not None:
            return response(pending.snapshot(updater.snapshot()), 202)
        pending.message = ""
        return response(await updater.install())

    @router.post("/app-update/restart")
    async def restart(request: Request):
        updater = updater_for(request)
        if pending.phase is not None or updater.phase == "restarting":
            return response(pending.snapshot(updater.snapshot()), 202)
        if updater.phase != "ready":
            raise HTTPException(409, "Install the update before restarting.")
        try:
            host = request.url.hostname
            port = request.url.port or (request.scope.get("server") or (None, 8765))[1]
            if host not in ("127.0.0.1", "localhost") or not 0 < port < 65536:
                raise ValueError("Restart needs the local app address.")
            pending.request(host, port)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return response(pending.snapshot(updater.snapshot()), 202)

    @router.post("/app-update/restart/cancel")
    async def cancel_restart(request: Request):
        updater = updater_for(request)
        if not await pending.cancel():
            return response(pending.snapshot(updater.snapshot()), 409)
        return response(pending.snapshot(updater.snapshot()))

    return router
