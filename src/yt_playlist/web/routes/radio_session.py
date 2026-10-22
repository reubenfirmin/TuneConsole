"""Home Radio and saved-playlist session endpoints."""
from fastapi import APIRouter, Request


def build(ctx):
    router = APIRouter()
    radio = ctx.radio

    @router.get("/radio/status")
    def status():
        radio.prepare()
        return radio.bootstrap()["state"]

    @router.get("/radio/seeds")
    async def seeds():
        return await radio.work.to_thread(radio.seeds)

    @router.get("/radio/playlist")
    def playlist(request: Request):
        state = radio.status()
        pid = state["local_id"]
        tracks = ctx.store.charts.playlist_tracks_detail(pid) if pid else []
        return ctx.templates.TemplateResponse(request, "_partials/radio_playlist.html", {
            "session": state, "tracks": tracks,
        })

    @router.post("/radio/start")
    async def start(request: Request):
        try:
            data = await request.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "Choose a library song to start radio."}
        if any(data.get(key) is not None and not isinstance(data[key], str) for key in ("video_id", "local_date", "menu_id")):
            return {"ok": False, "reason": "Invalid Radio Session request."}
        try:
            return await radio.work.to_thread(radio.start, data.get("video_id"), data.get("local_date"), data.get("menu_id"))
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

    @router.post("/radio/stop")
    def stop():
        return radio.stop()

    @router.post("/radio/select")
    async def select(request: Request):
        try:
            data = await request.json()
            if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("proposal_id", "menu_id")):
                raise ValueError("Choose one of the current radio directions.")
            return await radio.work.to_thread(radio.select, data["proposal_id"], data["menu_id"])
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

    @router.post("/radio/seen")
    async def seen(request: Request):
        try:
            data = await request.json()
            if not isinstance(data, dict) or not isinstance(data.get("menu_id"), str):
                return {"ok": False}
            return await radio.work.to_thread(radio.seen, data["menu_id"])
        except ValueError:
            return {"ok": False}

    return router
