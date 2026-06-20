"""Move tab: copy/move a playlist between identities."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from yt_playlist.executor import copy_or_move_playlist


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

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
    def move_run(playlist: int = Form(...), target_identity: int = Form(...), copy_only: str = Form("")):
        clients = ctx.client_provider()
        pl = store.get_playlist(playlist)
        if pl is None:
            return JSONResponse({"ok": False, "error": "playlist no longer exists"})
        src_client = clients.get(pl.identity_id)
        tgt_client = clients.get(target_identity)
        if src_client is None or tgt_client is None:
            return JSONResponse({"ok": False, "error": "missing client for an identity"})
        try:
            res = copy_or_move_playlist(store, playlist, target_identity, src_client, tgt_client,
                                        now_fn(), delete_source=not bool(copy_only))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception:  # noqa: BLE001
            logger.exception("move/copy failed")
            return JSONResponse({"ok": False, "error": "YouTube returned an unexpected response."})
        extra = f", {res['unresolved']} couldn’t be matched" if res["unresolved"] else ""
        if res["deleted"]:
            msg = f"Moved “{res['title']}” ({res['added']} tracks{extra}). Source deleted (backup saved)."
        elif res.get("delete_error"):  # copy succeeded but the original couldn't be deleted
            msg = (f"Copied “{res['title']}” ({res['added']} tracks{extra}). Couldn’t delete the original "
                   f"— YouTube refused (it may not be a deletable playlist). Remove it manually if you want.")
        elif not copy_only:  # move requested but some tracks couldn't be recreated -> kept the source
            msg = f"Copied “{res['title']}” ({res['added']} tracks{extra}). Source kept — couldn’t recreate every track."
        else:
            msg = f"Copied “{res['title']}” ({res['added']} tracks{extra})."
        return JSONResponse({"ok": True, "message": msg, "deleted": res["deleted"]})

    return router
