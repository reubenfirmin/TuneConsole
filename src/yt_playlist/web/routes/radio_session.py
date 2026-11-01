"""Home Radio and saved-playlist session endpoints."""
from fastapi import APIRouter, Request

from yt_playlist.rec import onboarding
from yt_playlist.web.routes.home_access import guard, state as growth_state


def build(ctx):
    router = APIRouter()
    radio = ctx.radio

    @router.get("/radio/status")
    def status():
        radio.prepare()
        return radio.state()

    @router.get("/radio/seeds")
    async def seeds():
        return await radio.work.to_thread(radio.seeds)

    @router.get("/radio/playlist")
    def playlist(request: Request):
        state = radio.status()
        tracks = radio.history_tracks(state)
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
        playlist_id = data.get("playlist_id")
        if playlist_id is not None and (type(playlist_id) is not int or not 0 < playlist_id < 2**63 or data.get("video_id")):
            return {"ok": False, "reason": "Choose a playlist to start radio."}
        try:
            intent = ctx.bridge.begin_playback_intent()

            def launch():
                # Bootstrap and access checks read the library too. Keep them in
                # admitted work so a slow read cannot stop HTTP or bridge replies.
                # The small first set still requires its current eligible offer.
                bootstrap = radio.bootstrap()
                starter = (not onboarding.setup_completed(ctx.store)
                           and ctx.store.settings.get_setting('onboard_listening_format') == 'radio'
                           and playlist_id is None and data.get('menu_id') == bootstrap.get('seedMenu')
                           and any(track.get('video_id') == data.get('video_id') for track in bootstrap['seeds']))
                if not starter and not bootstrap['state'].get('active'):
                    guard(ctx, 'radio', radio=bootstrap)(request)
                return radio.start(data.get("video_id"), data.get("local_date"), data.get("menu_id"),
                                   playlist_id=playlist_id, intent=intent)

            return await radio.work.to_thread(launch)
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
            video_id = data.get('video_id')
            if 'video_id' in data and (not isinstance(video_id, str) or not video_id.strip()):
                raise ValueError("Choose a current song from the stream.")
            intent = ctx.bridge.begin_playback_intent() if video_id else None
            return await radio.work.to_thread(radio.select, data["proposal_id"], data["menu_id"], video_id,
                                              intent=intent)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

    @router.post("/radio/tune")
    async def tune(request: Request):
        try:
            values = await request.json()

            def apply_tuning():
                if isinstance(values, dict) and values.get('energy', .5) != .5:
                    if not growth_state(ctx, request)['vibes_ready']:
                        guard(ctx, 'vibes')(request)
                return radio.tune(values)

            return await radio.work.to_thread(apply_tuning)
        except (ValueError, TypeError):
            return {"ok": False}

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
