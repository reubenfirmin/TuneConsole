"""Move tab: copy/move a playlist between identities."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

    def _move_row(request, playlist_id, *, message=None, error=None):
        p = store.get_playlist(playlist_id)
        if p is None:
            return HTMLResponse("")   # the playlist is gone (deleted elsewhere) -> drop the row
        kinds = {p.ytm_playlist_id: store.playlist_kind(p.id)}
        return templates.TemplateResponse(request, "_partials/move_row.html",
                                          {"p": p, "kinds": kinds, "message": message, "error": error})

    @router.get("/move")
    def move_page(request: Request):
        idents = store.get_identities()
        labels = {i.id: i.label for i in idents}
        rows = sorted(store.get_playlists(), key=lambda p: (labels.get(p.identity_id, ""), p.title.lower()))
        kinds = {p.ytm_playlist_id: store.playlist_kind(p.id) for p in rows}
        return templates.TemplateResponse(request, "move.html", {
            "playlists": rows, "identities": idents, "labels": labels, "kinds": kinds,
            "flash": request.query_params.get("flash"),
        })

    @router.post("/move/run")
    def move_run(request: Request, playlist: int = Form(...), target_identity: int = Form(...),
                 copy_only: str = Form("")):
        try:
            res = ctx.ops().move(playlist, target_identity, copy_only=bool(copy_only))
        except ValueError as e:
            return _move_row(request, playlist, error=str(e))
        except Exception:  # noqa: BLE001
            logger.exception("move/copy failed")
            return _move_row(request, playlist, error="YouTube returned an unexpected response.")
        if res["deleted"]:
            return HTMLResponse("")   # source deleted -> htmx fades + removes the row
        extra = f", {res['unresolved']} couldn’t be matched" if res["unresolved"] else ""
        if res.get("delete_error"):  # copy succeeded but the original couldn't be deleted
            msg = (f"Copied “{res['title']}” ({res['added']} tracks{extra}). Couldn’t delete the original: "
                   f"YouTube refused (it may not be a deletable playlist). Remove it manually if you want.")
        elif not copy_only:  # move requested but some tracks couldn't be recreated -> kept the source
            msg = f"Copied “{res['title']}” ({res['added']} tracks{extra}). Source kept. Couldn’t recreate every track."
        else:
            msg = f"Copied “{res['title']}” ({res['added']} tracks{extra})."
        return _move_row(request, playlist, message=msg)

    return router
