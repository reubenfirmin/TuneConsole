import json
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.controllers.merge_drafts import ordered as _ordered, liked_id as _liked_id
from yt_playlist.library.playlist_mutation import PlaylistWriteUnconfirmed
from yt_playlist.rec.actions import refresh_cleanup


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

    def _outcome(request, control, error):
        return templates.TemplateResponse(
            request, "_partials/action_outcome.html", {"control": control, "message": str(error)},
            status_code=422, headers={"HX-Reswap": "none"})

    def _refresh():
        # htmx does a full page reload; dependent sections (overlaps that referenced a
        # deleted copy) recompute, exactly as the old location.reload() did.
        return HTMLResponse("", headers={"HX-Refresh": "true"})

    def _ids(raw):
        return [int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]

    def _members_tracks(ids):
        return ctx.merge_drafts().members_tracks(ids)

    def _draft(ids, members, *, return_to=None):
        return ctx.merge_drafts().draft(ids, members, return_to=return_to)

    def _view(request, ids, members, tracks, draft):
        draft_params = json.loads(draft.params_json)
        draft_params["excluded"] = set(draft_params["excluded"])
        rows = [{**t, "included": t["tid"] not in draft_params["excluded"],
                 "picked": draft_params["pick"].get(t["tid"]), "present_count": sum(t["present"])}
                for t in _ordered(tracks, draft_params)]
        return_label = {"/cleanup": "Back to cleanup", "/playlists": "Back to playlists",
                        "/": "Back to dashboard"}.get(urlsplit(draft_params["return_to"]).path, "Back")
        return {"request": request, "members": members, "rows": rows, "liked_id": _liked_id(members),
                "return_label": return_label,
                "count": sum(1 for t in tracks if t["tid"] not in draft_params["excluded"]),
                "total": len(tracks), "draft": draft_params, "ids_csv": ",".join(str(i) for i in ids)}

    @router.get("/merge")
    def merge_editor(request: Request):
        # N-way track-level merge editor for a set of playlists (?ids=1,2,3).
        ids = _ids(request.query_params.get("ids", ""))
        mt = _members_tracks(ids)
        if mt is None:
            raise HTTPException(status_code=404, detail="need at least two existing playlists")
        members, tracks = mt
        ret = request.query_params.get("return", "/cleanup")
        if not (ret.startswith("/") and not ret.startswith("//")):
            ret = "/cleanup"
        draft = _draft(ids, members, return_to=ret)
        return templates.TemplateResponse(request, "editor.html", _view(request, ids, members, tracks, draft))

    @router.post("/merge/update")
    async def merge_update(request: Request):
        # Mutate one field of the draft and re-render the editor body.
        ids = _ids(request.query_params.get("ids", ""))
        mt = _members_tracks(ids)
        if mt is None:
            raise HTTPException(status_code=404, detail="merge no longer available")
        members, tracks = mt
        draft = _draft(ids, members)
        form = await request.form()
        draft = ctx.merge_drafts().update(draft, members, tracks, form.get('field', ''), form.get('value', ''))
        return templates.TemplateResponse(request, "_partials/merge_body.html",
                                          _view(request, ids, members, tracks, draft))

    @router.post("/merge/apply")
    def merge_apply(request: Request):
        # Apply the N-way editor: set the kept playlist(s) to the draft's included tracks, in order.
        ids = _ids(request.query_params.get("ids", ""))
        try:
            s, draft_params = ctx.merge_drafts().apply(ids)
        except PlaylistWriteUnconfirmed:
            logger.exception("merge/apply unconfirmed")
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        except ActionInputError as exc:
            return _outcome(request, 'merge-outcome', exc)
        except Exception:  # noqa: BLE001
            logger.exception("merge/apply failed")
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        parts = []
        if s["added"]:
            parts.append(f"{s['added']} added")
        if s["removed"]:
            parts.append(f"{s['removed']} removed")
        if s.get("skipped"):
            parts.append(f"{s['skipped']} skipped (unavailable)")
        if s["deleted"]:
            parts.append("deleted " + ", ".join(f"“{t}”" for t in s["deleted"]))
        detail = (", " + ", ".join(parts)) if parts else ""
        msg = f"Merged into “{s['kept_title']}”{detail}."
        ret = urlsplit(draft_params["return_to"])
        query = (ret.query + "&" if ret.query else "") + f"flash={quote(msg)}&flash_pl={quote(s['kept_ytm'])}"
        url = urlunsplit((ret.scheme, ret.netloc, ret.path, query, ret.fragment))
        return Response(status_code=200, headers={"HX-Redirect": url})

    @router.post("/dupe/delete")
    def dupe_delete(source: int = Form(...), target: int = Form(...)):
        # One-shot delete for the dupes table: plan + remote-verified delete in a single call,
        # returning JSON so the row can be removed without a page reload. The remote verify in
        # execute_planned makes rapid-fire deletes safe (refuses if the kept copy lacks a track).
        try:
            title = ctx.ops().delete_dupe(source, target)
        except ActionInputError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except Exception:  # noqa: BLE001
            logger.exception("inline dupe delete failed")
            return JSONResponse({"ok": False}, status_code=503)
        # no page reload on this path, so keep the home card's cached pending-cleanup icons honest
        refresh_cleanup(store, now_fn())
        return JSONResponse({"ok": True, "deleted": title})

    @router.post("/dupe/keep-one")
    def dupe_keep_one(request: Request, keep: int = Form(...)):
        # Collapse a cluster of identical playlists to one: delete every other copy with the same
        # track set, each remote-verified against the keeper. One click resolves the whole group.
        try:
            _deleted, errors = ctx.ops().keep_one(keep)
        except ActionInputError as exc:
            return _outcome(request, f'keep-outcome-{keep}', exc)
        except Exception:  # noqa: BLE001
            logger.exception("keep-one %s failed", keep)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        if errors:
            logger.warning("keep-one %s incomplete: %s", keep, errors)
            if all(isinstance(error, ActionInputError) for error in errors):
                return _outcome(request, f'keep-outcome-{keep}', errors[0])
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        return _refresh()

    @router.post("/playlist/delete-empty")
    def delete_empty(request: Request, playlist: int = Form(...)):
        try:
            ctx.ops().delete_empty(playlist)
        except ActionInputError as exc:
            return _outcome(request, f'empty-outcome-{playlist}', exc)
        except Exception:  # noqa: BLE001
            logger.exception("delete-empty failed")
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        # no page reload on this path, so keep the home card's cached pending-cleanup icons honest
        refresh_cleanup(store, now_fn())
        return HTMLResponse("")   # htmx fades + removes the row

    @router.post("/playlist/forget-empty")
    def forget_empty(request: Request, playlist: int = Form(...)):
        try:
            ctx.ops().forget_empty(playlist)
        except ActionInputError as exc:
            return _outcome(request, f'empty-outcome-{playlist}', exc)
        except Exception:  # noqa: BLE001
            logger.exception("forget-empty failed")
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        refresh_cleanup(store, now_fn())
        return HTMLResponse("")

    @router.get("/dupe/{a}/{b}")
    def dupe_detail(a: int, b: int):
        # back-compat: the pairwise compare is just the 2-playlist case of the N-way editor
        if not ctx.merge_drafts().available((a, b)):
            raise HTTPException(status_code=404, detail="playlist not found")
        return RedirectResponse(f"/merge?ids={a},{b}&return=/cleanup%23overlaps", status_code=307)

    return router
