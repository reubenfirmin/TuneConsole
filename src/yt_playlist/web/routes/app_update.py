"""User-initiated desktop updates and a graceful handoff to the installed version."""
import asyncio
import os
import signal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from yt_playlist.library.update_composition import desktop


async def _stop_server():
    # Let the restart response reach the browser before uvicorn closes its listener.
    await asyncio.sleep(0.3)
    os.kill(os.getpid(), signal.SIGTERM)


def build(ctx, *, commands=None):
    router = APIRouter()
    commands = commands if commands is not None else desktop(ctx, _stop_server)

    def updater_for(request):
        if not commands.available:
            raise HTTPException(404, "In-app updates are available in packaged desktop installations.")
        # These operations install code and stop the server. Require the exact origin,
        # including the port, in addition to the application's general loopback guard.
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                source, target = urlsplit(origin), urlsplit(str(request.url))
                if (source.scheme, source.netloc) != (target.scheme, target.netloc):
                    raise HTTPException(403, "cross-origin request blocked")
        return commands

    def response(data, status_code=200):
        return JSONResponse(data, status_code=status_code, headers={"Cache-Control": "no-store"})

    @router.get("/app-update/status")
    async def status(request: Request):
        return response(await updater_for(request).status())

    @router.post("/app-update/install")
    async def install(request: Request):
        data, status_code = await updater_for(request).install()
        return response(data, status_code)

    @router.post("/app-update/restart")
    async def restart(request: Request):
        command = updater_for(request)
        try:
            host = request.url.hostname
            port = request.url.port or (request.scope.get("server") or (None, 8765))[1]
            data, status_code = command.restart(host, port)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return response(data, status_code)

    @router.post("/app-update/restart/cancel")
    async def cancel_restart(request: Request):
        data, status_code = await updater_for(request).cancel()
        return response(data, status_code)

    return router
