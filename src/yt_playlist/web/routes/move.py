"""Copy or move selected playlists between identities."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, logger = ctx.store, ctx.templates, ctx.logger

    def _panel_context(*, source=None, target=None, results=None, error=None):
        idents = store.get_identities()
        labels = {i.id: i.label for i in idents}
        rows = sorted((p for p in store.get_playlists() if p.identity_id in labels),
                      key=lambda p: (labels[p.identity_id], p.title.lower()))
        if source not in labels:
            source = idents[0].id if idents else None
        if target not in labels:
            target = next((i.id for i in idents if i.id != source), source)
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
            "playlists": rows, "identities": idents, "labels": labels,
            "selection_rows": [{"id": p.id, "identity_id": p.identity_id} for p in rows],
            "source": source, "target": target, "results": results, "error": error,
            "summary": ". ".join(summary) + ("." if summary else ""),
            "has_issues": any(r["warning"] for r in results),
        }

    def _panel(request, **kwargs):
        return templates.TemplateResponse(request, "_partials/move_panel.html", _panel_context(**kwargs))

    def _move_row(request, playlist_id, *, message=None, error=None):
        p = store.get_playlist(playlist_id)
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

    def _refresh_deleted_source(ytm_id):
        # #95: a move deletes the source playlist on YouTube; a YTM tab sitting on it should show
        # it's gone, not a stale tracklist. Best-effort only (no extension connected raises), and
        # never affects the route's own response. The copy destination is a brand-new playlist no
        # tab can already be viewing, so only the deleted source warrants a refresh.
        try:
            if ytm_id:
                ctx.bridge.send_control({"type": "refresh-view", "playlist": ytm_id})
        except Exception:  # noqa: BLE001
            logger.debug("refresh-view control not sent for %s", ytm_id, exc_info=True)

    def _transfer(playlist, target_identity, copy_only):
        src = store.get_playlist(playlist)
        result = {"title": src.title if src else "Unavailable playlist", "warning": False}
        try:
            res = ctx.ops().move(playlist, target_identity, copy_only=bool(copy_only))
        except ValueError as e:
            return {**result, "state": "error", "message": str(e), "warning": True}
        except Exception:  # noqa: BLE001
            logger.exception("move/copy failed")
            return {**result, "state": "error", "message": "YouTube returned an unexpected response.",
                    "warning": True}
        tracks = f"{res['added']} track{'s' if res['added'] != 1 else ''}"
        if res["deleted"]:
            _refresh_deleted_source(src.ytm_playlist_id)
            return {**result, "state": "moved", "message": f"Moved {tracks}."}
        extra = f"; {res['unresolved']} couldn’t be matched" if res["unresolved"] else ""
        msg = f"Copied {tracks}{extra}."
        if res.get("delete_error"):  # copy succeeded but the original couldn't be deleted
            msg += " Source kept: YouTube couldn’t delete the original."
        elif not copy_only:  # move requested but some tracks couldn't be recreated -> kept the source
            msg += " Source kept because some tracks couldn’t be recreated."
        return {**result, "state": "copied", "message": msg,
                "warning": bool(res["unresolved"] or res.get("delete_error"))}

    @router.post("/move/run")
    def move_run(request: Request, playlist: int = Form(...), target_identity: int = Form(...),
                 copy_only: str = Form("")):
        result = _transfer(playlist, target_identity, bool(copy_only))
        if result["state"] == "moved":
            return HTMLResponse("")
        key = "error" if result["state"] == "error" else "message"
        return _move_row(request, playlist, **{key: result["message"]})

    @router.post("/move/bulk")
    def move_bulk(request: Request, ids: str = Form(...), source_identity: int = Form(...),
                  target_identity: int = Form(...), copy_only: str = Form("")):
        def rejected(message):
            return _panel(request, source=source_identity, target=target_identity, error=message)

        active = {i.id for i in store.get_identities()}
        if source_identity not in active or target_identity not in active:
            return rejected("An identity has been removed. Choose identities again.")
        if source_identity == target_identity:
            return rejected("Pick two different identities.")
        try:
            playlist_ids = list(dict.fromkeys(int(pid) for pid in ids.split(",") if pid.strip()))
        except ValueError:
            return rejected("The playlist selection is invalid. Select playlists again.")
        if not playlist_ids:
            return rejected("Select at least one playlist.")
        # Validate the entire selection before making any remote changes, including stale tabs.
        for pid in playlist_ids:
            playlist = store.get_playlist(pid)
            if playlist is None or playlist.identity_id != source_identity:
                return rejected("The playlist selection has changed. Select playlists again.")
        results = [_transfer(pid, target_identity, bool(copy_only)) for pid in playlist_ids]
        return _panel(request, source=source_identity, target=target_identity, results=results)

    return router
