"""Opt-in playlist radio POC at /radio-session-poc; no changes to existing radio."""
from fastapi import APIRouter, Request

from yt_playlist.web.radio_playlist_poc import PlaylistRadio


def build(ctx):
    router = APIRouter(prefix="/radio-session-poc")
    radio = PlaylistRadio(ctx)

    @router.get("")
    def page(request: Request):
        return ctx.templates.TemplateResponse(request, "radio_session_poc.html", {})

    @router.get("/status")
    def status():
        return radio.status()

    @router.post("/start")
    async def start():
        return await radio.work.to_thread(radio.start)

    @router.post("/stop")
    def stop():
        return radio.stop()

    return router
