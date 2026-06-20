"""Destructive cleanup ops: the N-way merge editor, dupe deletes, keep-one, delete-empty."""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from yt_playlist import analysis
from yt_playlist.executor import (
    MergePlan, apply_result, delete_empty_playlist, execute_planned, store_plan)
from yt_playlist.merge_order import track_positions


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

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
        return templates.TemplateResponse(request, "editor.html",
                                          {"members": members, "tracks": track_list})

    @router.post("/merge/apply")
    def merge_apply(ids: str = Form(...), result: str = Form(""), keep: str = Form("all")):
        # Apply the N-way editor: set the kept playlist(s) to exactly `result`.
        clients = ctx.client_provider()
        pid_list = [int(x) for x in ids.split(",") if x]
        vids = [v for v in result.split(",") if v]
        try:
            s = apply_result(store, clients, pid_list, vids, keep, now_fn())
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
        if source == target:
            return JSONResponse({"ok": False, "error": "source and target must differ"})
        clients = ctx.client_provider()
        src, tgt = store.get_playlist(source), store.get_playlist(target)
        if src is None or tgt is None:
            return JSONResponse({"ok": False, "error": "playlist no longer exists (already deleted?)"})
        try:
            aid = store_plan(store, MergePlan(source, target, [], []), "delete",
                             src.ytm_playlist_id, now_fn())
            execute_planned(store, aid, clients, now_fn())
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception:  # noqa: BLE001
            logger.exception("inline dupe delete failed")
            return JSONResponse({"ok": False, "error": "YouTube returned an unexpected response."})
        return JSONResponse({"ok": True, "deleted": src.title})

    @router.post("/dupe/keep-one")
    def dupe_keep_one(keep: int = Form(...)):
        # Collapse a cluster of identical playlists to one: delete every other copy with the same
        # track set, each remote-verified against the keeper. One click resolves the whole group.
        clients = ctx.client_provider()
        kept = store.get_playlist(keep)
        if kept is None:
            return JSONResponse({"ok": False, "error": "kept playlist no longer exists"})
        keep_keys = store.get_playlist_track_keys(keep)
        # never treat undeletable system playlists (Liked Music, etc.) as deletable siblings
        siblings = [p for p in store.get_playlists()
                    if p.id != keep and p.ytm_playlist_id not in analysis.SYSTEM_PLAYLIST_IDS
                    and store.get_playlist_track_keys(p.id) == keep_keys]
        deleted, errors = 0, []
        for sib in siblings:
            try:
                aid = store_plan(store, MergePlan(sib.id, keep, [], []), "delete",
                                 sib.ytm_playlist_id, now_fn())
                execute_planned(store, aid, clients, now_fn())
                deleted += 1
            except ValueError as e:
                errors.append(str(e))
            except Exception:  # noqa: BLE001
                logger.exception("keep-one delete of %s failed", sib.ytm_playlist_id)
                errors.append(f"{sib.ytm_playlist_id}: unexpected error")
        return JSONResponse({"ok": not errors, "deleted": deleted, "errors": errors})

    @router.post("/playlist/delete-empty")
    def delete_empty(playlist: int = Form(...)):
        clients = ctx.client_provider()
        pl = store.get_playlist(playlist)
        if pl is None:
            return JSONResponse({"ok": False, "error": "playlist no longer exists"})
        client = clients.get(pl.identity_id)
        if client is None:
            return JSONResponse({"ok": False, "error": "no client for that identity"})
        try:
            delete_empty_playlist(store, playlist, client, now_fn())
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception:  # noqa: BLE001
            logger.exception("delete-empty failed")
            return JSONResponse({"ok": False, "error": "YouTube returned an unexpected response."})
        return JSONResponse({"ok": True})

    @router.get("/dupe/{a}/{b}")
    def dupe_detail(a: int, b: int):
        # back-compat: the pairwise compare is just the 2-playlist case of the N-way editor
        ctx.playlist_by_id(a), ctx.playlist_by_id(b)
        return RedirectResponse(f"/merge?ids={a},{b}", status_code=307)

    return router
