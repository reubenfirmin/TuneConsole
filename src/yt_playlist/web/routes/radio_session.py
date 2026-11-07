"""Home Radio and saved-playlist session endpoints."""
import logging

from fastapi import APIRouter, Body, HTTPException, Request

from yt_playlist.core.activity import cancellable_work, WorkClosing
from yt_playlist.rec import onboarding, radio_tuning
from yt_playlist.web.routes.home_access import guard

logger = logging.getLogger(__name__)


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
        state = radio.state()
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
        if any(data.get(key) is not None and not isinstance(data[key], str) for key in ("video_id", "local_date", "menu_id", "launch_id")):
            return {"ok": False, "reason": "Invalid Radio Session request."}
        if data.get('launch_id') is not None and not 1 <= len(data['launch_id']) <= 64:
            return {"ok": False, "reason": "Invalid Radio Session request."}
        playlist_id = data.get("playlist_id")
        if playlist_id is not None and (type(playlist_id) is not int or not 0 < playlist_id < 2**63 or data.get("video_id")):
            return {"ok": False, "reason": "Choose a playlist to start radio."}
        try:
            with radio.launch_request(data.get('launch_id')) as cancelled:
                if cancelled.is_set():
                    return {'ok': False, 'reason': 'Stopped during start.'}
                intent = ctx.bridge.begin_playback_intent()

                def launch():
                    # Preflight is disposable; admitted remote writes keep their
                    # existing confirmation and reconciliation protocol.
                    try:
                        with cancellable_work(cancelled.is_set):
                            if cancelled.is_set():
                                raise WorkClosing('Radio launch stopped')
                            bootstrap = radio.bootstrap()
                            starter = (not onboarding.setup_completed(ctx.store)
                                       and ctx.store.settings.get_setting('onboard_listening_format') == 'radio'
                                       and playlist_id is None and data.get('menu_id') == bootstrap.get('seedMenu')
                                       and any(track.get('video_id') == data.get('video_id') for track in bootstrap['seeds']))
                            if not starter and not bootstrap['state'].get('active'):
                                guard(ctx, 'radio', radio=bootstrap)(request)
                    except WorkClosing:
                        return {'ok': False, 'reason': 'Stopped during start.'}
                    return radio.start(data.get("video_id"), data.get("local_date"), data.get("menu_id"),
                                       playlist_id=playlist_id, intent=intent, cancelled=cancelled)

                return await radio.work.to_thread(launch)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

    @router.post("/radio/stop")
    def stop(data: dict | None = Body(None)):
        launch_id = (data or {}).get('launch_id')
        if launch_id is not None and (not isinstance(launch_id, str) or not 1 <= len(launch_id) <= 64):
            return {'ok': False}
        return radio.stop(launch_id=launch_id)

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
            values = radio_tuning.controls(await request.json())
        except (ValueError, TypeError):
            logger.info("Radio tuning refused: reason=invalid_controls")
            return {"ok": False, "tune_reason": "invalid_controls"}
        serial = radio.reserve_tuning()
        logger.info("Radio tuning requested: serial=%d values=%s client=%r", serial, values,
                    request.headers.get('x-radio-tuning-client', 'unknown')[:64])

        def apply_tuning():
            try:
                # Use the access projection directly. Building the entire Home
                # journey here adds unrelated preparation to an explicit Apply.
                if values['energy'] != .5:
                    guard(ctx, 'vibes')(request)
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                logger.info("Radio tuning refused: reason=energy_locked values=%s", values)
                return {"ok": False, "tune_reason": "energy_locked"}
            logger.info("Radio tuning admitted: values=%s", values)
            return radio.tune(values, serial=serial)

        return await radio.work.to_thread(apply_tuning)

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
