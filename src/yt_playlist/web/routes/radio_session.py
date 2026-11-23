"""Home Radio and saved-playlist session endpoints."""
import asyncio
from contextlib import ExitStack
from functools import wraps
import logging
import time

from fastapi import APIRouter, Body, HTTPException, Request

from yt_playlist.rec import access
from yt_playlist.web.context import Ctx
from yt_playlist.web.artist_links import radio_navigation

logger = logging.getLogger(__name__)


def build(ctx):
    router = APIRouter()
    radio = ctx.radio

    def present(state):
        if not state.get('directions'):
            return state
        with ctx.store.read_view() as reader:
            return radio_navigation(state, reader.artists)

    async def present_command(command, *args, **kwargs):
        @wraps(command)
        def run():
            return present(command(*args, **kwargs))

        return await radio.work.to_thread(run)

    async def admitted_start(ownership, command, data, intent, launch):
        # Keep the launch registered while the worker drains even if the
        # requester disconnects after submitting it.
        with ownership:
            return await present_command(command, data, intent, launch)

    @router.get("/radio/status")
    def status():
        radio.prepare()
        return present(radio.state())

    @router.get("/radio/seeds")
    async def seeds():
        return await radio.work.to_thread(radio.seeds)

    @router.get("/radio/playlist")
    def playlist(request: Request):
        state = radio.state()
        tracks = radio.history_tracks(state)
        return ctx.templates.TemplateResponse(request, "_partials/radio_playlist.html", {
            "session": state, "tracks": tracks,
        })

    @router.post('/radio/resume')
    async def resume(data: dict = Body(...)):
        fields = ('playlist_id', 'video_id', 'episode_id')
        if any(not isinstance(data.get(key), str) or not data[key] for key in fields):
            return {'ok': False}
        intent = Ctx.radio_commands(ctx).admit_resume()
        return await radio.work.to_thread(radio.resume_playback,
            data['playlist_id'], data['video_id'], data['episode_id'], intent=intent)

    @router.post("/radio/start")
    async def start(request: Request):
        try:
            data = await request.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "Choose a library song to start radio."}
        if any(data.get(key) is not None and not isinstance(data[key], str) for key in ("video_id", "local_date", "menu_id", "launch_id")):
            return {"ok": False, "reason": "Invalid Radio Session request."}
        if data.get('launch_id') is not None and not 1 <= len(data['launch_id']) <= 64:
            return {"ok": False, "reason": "Invalid Radio Session request."}
        playlist_id = data.get("playlist_id")
        if playlist_id is not None and (type(playlist_id) is not int or not 0 < playlist_id < 2**63 or data.get("video_id")):
            return {"ok": False, "reason": "Choose a playlist to start radio."}
        try:
            commands = Ctx.radio_commands(ctx)
            with ExitStack() as ownership:
                intent, launch = ownership.enter_context(commands.admit_start(data))
                if launch.trace and not launch.replay:
                    clicked = data['clicked_at_ms']
                    if 0 < clicked < 2**53:
                        logger.info('Radio launch timing: launch=%r stage=request clicked_at_ms=%d click_to_request_ms=%d',
                                    launch.launch_id, clicked, time.time_ns() // 1_000_000 - clicked)
                task = asyncio.create_task(admitted_start(ownership.pop_all(), commands.start, data, intent, launch))
                return await asyncio.shield(task)
        except access.AccessDenied as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

    @router.post("/radio/stop")
    def stop(data: dict | None = Body(None)):
        launch_id = (data or {}).get('launch_id')
        if launch_id is not None and (not isinstance(launch_id, str) or not 1 <= len(launch_id) <= 64):
            return {'ok': False}
        return present(radio.stop(launch_id=launch_id))

    @router.post("/radio/select")
    async def select(request: Request):
        try:
            data = await request.json()
            if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("proposal_id", "menu_id")):
                raise ValueError("Choose one of the current radio directions.")
            video_id = data.get('video_id')
            if 'video_id' in data and (not isinstance(video_id, str) or not video_id.strip()):
                raise ValueError("Choose a current song from the stream.")
            intent = Ctx.radio_commands(ctx).admit_select(video_id)
            return await present_command(radio.select, data["proposal_id"], data["menu_id"], video_id, intent=intent)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

    @router.post("/radio/tune")
    async def tune(request: Request):
        try:
            commands = Ctx.radio_commands(ctx)
            values, serial = commands.admit_tuning(await request.json())
        except (ValueError, TypeError):
            logger.info("Radio tuning refused: reason=invalid_controls")
            return {"ok": False, "tune_reason": "invalid_controls"}
        logger.info("Radio tuning requested: serial=%d values=%s client=%r", serial, values,
                    request.headers.get('x-radio-tuning-client', 'unknown')[:64])

        return await present_command(commands.tune, values, serial)

    @router.post("/radio/seen")
    async def seen(request: Request):
        try:
            data = await request.json()
            if not isinstance(data, dict) or not isinstance(data.get("menu_id"), str):
                return {"ok": False}
            return await present_command(radio.seen, data["menu_id"])
        except ValueError:
            return {"ok": False}

    return router
