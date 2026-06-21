"""Destructive cleanup ops: the N-way merge editor, dupe deletes, keep-one, delete-empty."""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from yt_playlist.merge_order import track_positions


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    @router.get("/merge")
    def merge_editor(request: Request):
        # N-way track-level merge editor for a set of playlists (?ids=1,2,3).
        raw = request.query_params.get("ids", "")
        ids = [int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]
        pls = [p for p in (store.get_playlist(i) for i in ids) if p is not None]
        if len(pls) < 2:
            raise HTTPException(status_code=404, detail="need at least two existing playlists")
        idmap = {i.id: i.label for i in store.get_identities()}
        members = [{"id": p.id, "letter": chr(65 + n), "ident": idmap.get(p.identity_id, "?"),
                    "title": p.title, "ytm": p.ytm_playlist_id} for n, p in enumerate(pls)]
        tracks = {}
        seqs = [[] for _ in pls]                          # each playlist's track order, for order-preserving merge
        for mi, p in enumerate(pls):
            for pos, (k, v, t, ar, d, av) in enumerate(store.get_playlist_tracks_with_meta(p.id)):
                tid = ("v:" + v) if v else ("k:" + k)
                seqs[mi].append(tid)
                row = tracks.get(tid)
                if row is None:
                    row = {"tid": tid, "title": t, "artist": ar, "video_id": v,
                           "available": av, "duration": d, "present": [False] * len(pls),
                           "pos": [None] * len(pls)}
                    tracks[tid] = row
                elif row["duration"] is None and d is not None:
                    row["duration"] = d                 # fill from whichever copy has it
                row["present"][mi] = True
                row["pos"][mi] = pos + 1                 # 1-based index within this playlist (for display)
        positions = track_positions(seqs)            # avg normalized position → weaves shared & unique
        lens = [len(sq) for sq in seqs]
        for row in tracks.values():
            row["order"] = positions.get(row["tid"], 1.0)
            # normalized position (0..1) within each playlist, or None if absent — lets the editor
            # place a track by one chosen playlist's position instead of the average.
            row["npos"] = [((row["pos"][mi] - 1) / (lens[mi] - 1) if lens[mi] > 1 else 0.0)
                           if row["pos"][mi] is not None else None
                           for mi in range(len(pls))]
        track_list = sorted(tracks.values(), key=lambda r: (r["order"], (r["title"] or "").lower()))
        # where to land after Apply — only allow local paths (default: the Cleanup dashboard)
        ret = request.query_params.get("return", "/cleanup")
        if not (ret.startswith("/") and not ret.startswith("//")):
            ret = "/cleanup"
        return templates.TemplateResponse(request, "editor.html",
                                          {"members": members, "tracks": track_list, "return_to": ret})

    @router.post("/merge/apply")
    def merge_apply(ids: str = Form(...), result: str = Form(""), keep: str = Form("all")):
        # Apply the N-way editor: set the kept playlist(s) to exactly `result`.
        pid_list = [int(x) for x in ids.split(",") if x]
        vids = [v for v in result.split(",") if v]
        try:
            s = ctx.ops().apply_merge(pid_list, vids, keep)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception:  # noqa: BLE001
            logger.exception("merge/apply failed")
            return JSONResponse({"ok": False, "error": "YouTube returned an unexpected response."})
        parts = []
        if s["added"]:
            parts.append(f"{s['added']} added")
        if s["removed"]:
            parts.append(f"{s['removed']} removed")
        if s.get("skipped"):
            parts.append(f"{s['skipped']} skipped (unavailable)")
        if s["deleted"]:
            parts.append("deleted " + ", ".join(f"“{t}”" for t in s["deleted"]))
        detail = (" — " + ", ".join(parts)) if parts else ""
        msg = f"Merged into “{s['kept_title']}”{detail}."
        return JSONResponse({"ok": True, "message": msg, "playlist": s["kept_ytm"]})

    @router.post("/dupe/delete")
    def dupe_delete(source: int = Form(...), target: int = Form(...)):
        # One-shot delete for the dupes table: plan + remote-verified delete in a single call,
        # returning JSON so the row can be removed without a page reload. The remote verify in
        # execute_planned makes rapid-fire deletes safe (refuses if the kept copy lacks a track).
        try:
            title = ctx.ops().delete_dupe(source, target)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception:  # noqa: BLE001
            logger.exception("inline dupe delete failed")
            return JSONResponse({"ok": False, "error": "YouTube returned an unexpected response."})
        return JSONResponse({"ok": True, "deleted": title})

    @router.post("/dupe/keep-one")
    def dupe_keep_one(keep: int = Form(...)):
        # Collapse a cluster of identical playlists to one: delete every other copy with the same
        # track set, each remote-verified against the keeper. One click resolves the whole group.
        try:
            deleted, errors = ctx.ops().keep_one(keep)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": not errors, "deleted": deleted, "errors": errors})

    @router.post("/playlist/delete-empty")
    def delete_empty(request: Request, playlist: int = Form(...)):
        try:
            ctx.ops().delete_empty(playlist)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("delete-empty failed")
            return _toast(request, "YouTube returned an unexpected response.")
        return HTMLResponse("")   # htmx fades + removes the row

    @router.get("/dupe/{a}/{b}")
    def dupe_detail(a: int, b: int):
        # back-compat: the pairwise compare is just the 2-playlist case of the N-way editor
        ctx.playlist_by_id(a), ctx.playlist_by_id(b)
        return RedirectResponse(f"/merge?ids={a},{b}", status_code=307)

    return router
