"""User-initiated desktop updates and a graceful handoff to the installed version."""
import asyncio
import os
import signal
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from yt_playlist.core.update_lifecycle import acquire_restart_locks


async def _stop_server():
    # Let the restart response reach the browser before uvicorn closes its listener.
    await asyncio.sleep(0.3)
    os.kill(os.getpid(), signal.SIGTERM)


def build(ctx):
    router = APIRouter()

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
        return response(await updater_for(request).status())

    @router.post("/app-update/install")
    async def install(request: Request):
        return response(await updater_for(request).install())

    @router.post("/app-update/restart")
    async def restart(request: Request, background: BackgroundTasks):
        updater = updater_for(request)
        if updater.phase == "restarting":
            return response(updater.snapshot(), 202)
        if updater.phase != "ready":
            raise HTTPException(409, "Install the update before restarting.")
        acquired, message = acquire_restart_locks(ctx)
        if acquired is None:
            return response({**updater.snapshot(), "busy": True, "message": message}, 409)
        try:
            host = request.url.hostname
            port = request.url.port or (request.scope.get("server") or (None, 8765))[1]
            if host not in ("127.0.0.1", "localhost") or not 0 < port < 65536:
                raise ValueError("Restart needs the local app address.")
            result = await updater.restart(host, port)
        except Exception:
            for held in acquired:
                held.release()
            ctx.logger.warning("could not launch updated TuneConsole", exc_info=True)
            return response({**updater.snapshot(),
                             "message": "Couldn't restart TuneConsole. Please try again."}, 503)
        # Hold the locks until exit so no new background sync/import can start in the gap.
        background.add_task(_stop_server)
        return response(result, 202)

    return router
