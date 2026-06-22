"""Destructive cleanup ops: the N-way merge editor, dupe deletes, keep-one, delete-empty."""
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from yt_playlist.merge_order import track_positions


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    def _refresh():
        # htmx does a full page reload; dependent sections (overlaps that referenced a
        # deleted copy) recompute, exactly as the old location.reload() did.
        return HTMLResponse("", headers={"HX-Refresh": "true"})

    # In-memory merge drafts keyed by the sorted member-id signature. Survives a browser refresh
    # (same server process); cleared on Apply. The editing state lives here, not in the client.
    drafts = {}

    def _ids(raw):
        return [int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]

    def _members_tracks(ids):
        """Build members + de-duped track rows for a set of playlist ids (None if <2 exist)."""
        pls = [p for p in (store.get_playlist(i) for i in ids) if p is not None]
        if len(pls) < 2:
            return None
        idmap = {i.id: i.label for i in store.get_identities()}
        members = [{"id": p.id, "letter": chr(65 + n), "ident": idmap.get(p.identity_id, "?"),
                    "title": p.title, "ytm": p.ytm_playlist_id} for n, p in enumerate(pls)]
        tracks = {}
        seqs = [[] for _ in pls]
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
                    row["duration"] = d
                row["present"][mi] = True
                row["pos"][mi] = pos + 1
        positions = track_positions(seqs)
        lens = [len(sq) for sq in seqs]
        for row in tracks.values():
            row["order"] = positions.get(row["tid"], 1.0)
            row["npos"] = [((row["pos"][mi] - 1) / (lens[mi] - 1) if lens[mi] > 1 else 0.0)
                           if row["pos"][mi] is not None else None
                           for mi in range(len(pls))]
        return members, sorted(tracks.values(), key=lambda r: (r["order"], (r["title"] or "").lower()))

    def _eff_pos(t, pick):
        p = pick.get(t["tid"])
        npos = t["npos"]
        if p is not None and p < len(npos) and npos[p] is not None:
            return npos[p]
        vals = [v for v in npos if v is not None]
        return sum(vals) / len(vals) if vals else 1.0

    def _ordered(tracks, draft):
        by_title = lambda t: (t["title"] or "").lower()
        if draft["sort"] == "playlist":
            out = sorted(tracks, key=lambda t: (_eff_pos(t, draft["pick"]), by_title(t)))
        else:
            out = sorted(tracks, key=by_title)
        if draft["mode"] == "ducks":   # shared (present in >=2) first, odd ducks last (stable within)
            out = sorted(out, key=lambda t: 0 if sum(t["present"]) >= 2 else 1)
        return out

    def _liked_id(members):
        # Liked Music can't be deleted, so a merge that includes it can only keep Liked (delete the
        # others) or keep all — the per-other "keep" options are dropped. Returns LM's member id or None.
        return next((m["id"] for m in members if m["ytm"] == "LM"), None)

    def _draft(ids, members, *, return_to=None):
        sig = tuple(ids)   # keep the entry order so member letters/colors stay stable across refresh
        d = drafts.get(sig)
        if d is None:
            liked = _liked_id(members)                    # merge with Liked always lands in Liked
            d = {"excluded": set(), "pick": {}, "sort": "playlist", "mode": "interleaved",
                 "keep": str(liked if liked is not None else members[0]["id"]),
                 "return_to": return_to or "/cleanup"}
            drafts[sig] = d
        elif return_to:
            d["return_to"] = return_to
        return d

    def _view(request, ids, members, tracks, draft):
        rows = [{**t, "included": t["tid"] not in draft["excluded"],
                 "picked": draft["pick"].get(t["tid"]), "present_count": sum(t["present"])}
                for t in _ordered(tracks, draft)]
        return {"request": request, "members": members, "rows": rows, "liked_id": _liked_id(members),
                "count": sum(1 for t in tracks if t["tid"] not in draft["excluded"]),
                "total": len(tracks), "draft": draft, "ids_csv": ",".join(str(i) for i in ids)}

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
        field, value = form.get("field", ""), form.get("value", "")
        valid = {t["tid"] for t in tracks}
        if field == "toggle" and value in valid:
            draft["excluded"] ^= {value}
        elif field == "setall":
            draft["excluded"] = set() if value == "1" else set(valid)
        elif field == "sort" and value in ("alpha", "playlist"):
            draft["sort"] = value
        elif field == "mode" and value in ("interleaved", "ducks"):
            draft["mode"] = value
        elif field == "keep":
            liked = _liked_id(members)   # with Liked in the merge, only "keep Liked" or "all" are valid
            if liked is None or value in (str(liked), "all"):
                draft["keep"] = value
        elif field == "pick" and ":" in value:
            tid, _, idx = value.rpartition(":")
            if tid in valid and idx.isdigit():
                i = int(idx)
                if draft["pick"].get(tid) == i:
                    draft["pick"].pop(tid, None)        # toggle off
                else:
                    draft["pick"][tid] = i
        return templates.TemplateResponse(request, "_partials/merge_body.html",
                                          _view(request, ids, members, tracks, draft))

    @router.post("/merge/apply")
    def merge_apply(request: Request):
        # Apply the N-way editor: set the kept playlist(s) to the draft's included tracks, in order.
        ids = _ids(request.query_params.get("ids", ""))
        mt = _members_tracks(ids)
        if mt is None:
            return _toast(request, "Merge no longer available.")
        members, tracks = mt
        draft = _draft(ids, members)
        vids = [t["video_id"] for t in _ordered(tracks, draft)
                if t["tid"] not in draft["excluded"] and t["video_id"]]
        member_ids = [m["id"] for m in members]   # the existing playlists only (not stale ?ids entries)
        try:
            s = ctx.ops().apply_merge(member_ids, vids, draft["keep"])
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("merge/apply failed")
            return _toast(request, "YouTube returned an unexpected response.")
        drafts.pop(tuple(ids), None)                    # consume the draft
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
        sep = "&" if "?" in draft["return_to"] else "?"
        url = f"{draft['return_to']}{sep}flash={quote(msg)}&flash_pl={quote(s['kept_ytm'])}"
        return Response(status_code=200, headers={"HX-Redirect": url})

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
    def dupe_keep_one(request: Request, keep: int = Form(...)):
        # Collapse a cluster of identical playlists to one: delete every other copy with the same
        # track set, each remote-verified against the keeper. One click resolves the whole group.
        try:
            _deleted, errors = ctx.ops().keep_one(keep)
        except ValueError as e:
            return _toast(request, str(e))
        if errors:
            return _toast(request, "Couldn’t delete some copies: " + " · ".join(errors))
        return _refresh()

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
