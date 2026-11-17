"""Copy or move selected playlists between identities."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from yt_playlist.web.context import Ctx


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, logger = ctx.store, ctx.templates, ctx.logger

    def _panel_context(*, source=None, target=None, results=None, error=None):
        facts = Ctx.transfer_commands(ctx).panel(source, target)
        results = results or []
        counts = {state: sum(r["state"] == state for r in results)
                  for state in ("moved", "copied", "error")}
        summary = []
        for state, verb in (("moved", "Moved"), ("copied", "Copied")):
            n = counts[state]
            if n:
                summary.append(f"{verb} {n} playlist{'s' if n != 1 else ''}")
        if counts["error"]:
            summary.append(f"{counts['error']} failed")
        return {
            **facts, "results": results, "error": error,
            "summary": ". ".join(summary) + ("." if summary else ""),
            "has_issues": any(r["warning"] for r in results),
        }

    def _panel(request, **kwargs):
        return templates.TemplateResponse(request, "_partials/move_panel.html", _panel_context(**kwargs))

    def _move_row(request, playlist_id, *, message=None, error=None):
        p = Ctx.transfer_commands(ctx).row(playlist_id)
        if p is None:
            return HTMLResponse("")   # the playlist is gone (deleted elsewhere) -> drop the row
        return templates.TemplateResponse(request, "_partials/move_row.html",
                                          {"p": p, "message": message, "error": error})

    @router.get("/move")
    def move_page(request: Request):
        return templates.TemplateResponse(request, "move.html", {
            **_panel_context(),
            "flash": request.query_params.get("flash"),
        })

    @router.post("/move/run")
    def move_run(request: Request, playlist: int = Form(...), target_identity: int = Form(...),
                 copy_only: str = Form("")):
        result = Ctx.transfer_commands(ctx).transfer(playlist, target_identity, bool(copy_only))
        if result["state"] == "moved":
            return HTMLResponse("")
        key = "error" if result["state"] == "error" else "message"
        return _move_row(request, playlist, **{key: result["message"]})

    @router.post("/move/bulk")
    def move_bulk(request: Request, ids: str = Form(...), source_identity: int = Form(...),
                  target_identity: int = Form(...), copy_only: str = Form("")):
        def rejected(message):
            return _panel(request, source=source_identity, target=target_identity, error=message)

        try:
            results = Ctx.transfer_commands(ctx).bulk(ids, source_identity, target_identity, bool(copy_only))
        except ValueError as exc:
            return rejected(str(exc))
        return _panel(request, source=source_identity, target=target_identity, results=results)

    return router
