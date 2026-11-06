"""Toggle a song's membership in YouTube's Liked Music (the heart on a song row)."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import Response
from yt_playlist.core.action_errors import ActionInputError


def build(ctx) -> APIRouter:
    router = APIRouter()
    templates, logger = ctx.templates, ctx.logger

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    @router.post("/track/like")
    def track_like(request: Request, video_id: str = Form(...), on: str = Form("")):
        try:
            res = ctx.ops().set_liked(video_id, bool(on))
        except ActionInputError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("like toggle failed")
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        # The confirmed rating inbox records the save with its replay receipt.
        # A second measurement here would duplicate it and wait for maintenance.
        return templates.TemplateResponse(
            request, "_partials/liked_cell.html", {"video_id": video_id, "liked": res["liked"]})

    return router
