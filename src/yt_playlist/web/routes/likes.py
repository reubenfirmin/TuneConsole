"""Toggle a song's membership in YouTube's Liked Music (the heart on a song row)."""
from fastapi import APIRouter, Form, Request


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
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("like toggle failed")
            return _toast(request, "YouTube wouldn’t update your Liked Music.")
        return templates.TemplateResponse(
            request, "_partials/liked_cell.html", {"video_id": video_id, "liked": res["liked"]})

    return router
