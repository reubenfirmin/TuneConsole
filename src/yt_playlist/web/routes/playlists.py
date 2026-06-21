"""Playlists tab: browse every playlist, sort, assign user groups, and run bulk actions."""
import asyncio
import json
import re
import threading
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from yt_playlist import discogs, lastfm, musicbrainz
from yt_playlist.analysis import SYSTEM_PLAYLIST_IDS


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

    def _ids(form):
        return [int(x) for x in (form.get("ids", "") or "").split(",") if x.strip().isdigit()]

    def _refresh():
        # htmx sees this header and does a full page reload — exact parity with the old
        # location.reload(); the list re-renders from the server as it did before.
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    def _track_row(request, pid, video_id):
        # Re-render one track's <tr> (the shared swap unit for manual edits and enrich).
        for i, t in enumerate(store.playlist_tracks_detail(pid), start=1):
            if t["video_id"] == video_id:
                return templates.TemplateResponse(request, "_partials/track_row.html", {"t": t, "idx": i})
        return Response(status_code=204)         # row no longer present — nothing to swap

    @router.get("/playlists")
    def playlists_page(request: Request):
        labels = {i.id: i.label for i in store.get_identities()}
        groups = store.get_playlist_groups()                 # ytm -> group name
        stats = store.get_playlist_listen_stats()            # pid -> (last_ts, count)
        hidden = store.get_hidden_playlists()                # ytm of playlists hidden from this tab
        rows = []
        for p in store.get_playlists():
            if p.ytm_playlist_id in hidden:
                continue
            last, listens = stats.get(p.id, (None, 0))
            rows.append({
                "id": p.id, "ytm": p.ytm_playlist_id, "title": p.title,
                "identity": labels.get(p.identity_id, "?"), "thumbnail": p.thumbnail,
                "count": p.track_count, "kind": store.playlist_kind(p.id),
                "group": groups.get(p.ytm_playlist_id, ""),
                "last": last, "listens": listens,
            })
        group_names = sorted({g for g in groups.values() if g}, key=str.lower)
        return templates.TemplateResponse(request, "playlists.html", {
            "rows": rows, "has_groups": bool(groups), "group_names": group_names,
            "flash": request.query_params.get("flash"),
            "flash_pl": request.query_params.get("flash_pl"),
        })

    @router.get("/playlist/{pid}")
    def playlist_detail(request: Request, pid: int):
        pl = store.get_playlist(pid)
        if pl is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        labels = {i.id: i.label for i in store.get_identities()}
        tracks = store.playlist_tracks_detail(pid)
        from yt_playlist.repos.rec_query import GENERATED_GROUP
        return templates.TemplateResponse(request, "playlist.html", {
            "pl": pl, "tracks": tracks, "identity": labels.get(pl.identity_id, "?"),
            "is_generated": store.get_playlist_groups().get(pl.ytm_playlist_id) == GENERATED_GROUP,
            "kind": store.playlist_kind(pid), "total_plays": sum(t["plays"] for t in tracks),
            # autosuggest = the editable whitelist plus whatever genres already exist in the library
            "genres": sorted(set(store.get_genre_whitelist()) | set(store.all_genres()), key=str.lower),
            "lastfm_configured": lastfm.api_key(store) is not None,
            "active_job": ctx.jobs.find_active(pid),     # an in-progress enrichment to rejoin, if any
        })

    @router.get("/playlist/{pid}/share.txt")
    def playlist_share(pid: int):
        """Download the playlist as a plain .txt — one song URL per line — for easy sharing."""
        pl = store.get_playlist(pid)
        if pl is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        lines = [f"https://music.youtube.com/watch?v={t['video_id']}"
                 for t in store.playlist_tracks_detail(pid) if t.get("video_id")]
        # ascii-safe filename for the legacy param, plus RFC 5987 filename* for the real title
        safe = re.sub(r'[\\/:*?"<>|]', "_", pl.title).strip() or "playlist"
        ascii_name = safe.encode("ascii", "ignore").decode() or "playlist"
        return PlainTextResponse("\n".join(lines) + ("\n" if lines else ""), headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}.txt"; '
                                   f"filename*=UTF-8''{quote(safe)}.txt",
        })

    @router.get("/playlist/{pid}/alternates")
    def playlist_alternates(request: Request, pid: int, video_id: str):
        """Search YouTube for alternate versions of a track; render the results list for the modal."""
        ctx_data = {"results": []}
        try:
            ctx_data["results"] = ctx.ops().find_alternates(pid, video_id)
        except ValueError as e:
            ctx_data["error"] = str(e)
        except Exception:  # noqa: BLE001
            logger.exception("alternate search failed for %s in playlist %s", video_id, pid)
            ctx_data["error"] = "YouTube returned an unexpected response"
        return templates.TemplateResponse(request, "_partials/alternates_results.html", ctx_data)

    @router.post("/playlist/{pid}/add-tracks")
    async def playlist_add_tracks(pid: int, request: Request):
        # each selected alternate posts as a "track" field carrying its full track JSON
        tracks = []
        for raw in (await request.form()).getlist("track"):
            try:
                tracks.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
        if not tracks:
            return _toast(request, "select at least one version to add")
        try:
            await asyncio.to_thread(ctx.ops().add_tracks, pid, tracks)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("add-tracks failed for playlist %s", pid)
            return _toast(request, "YouTube returned an unexpected response")
        return _refresh()                             # reload so the new tracks drop into the table

    ENRICH_SOURCES = {"musicbrainz": musicbrainz.enrich_playlist, "lastfm": lastfm.enrich_playlist,
                      "discogs": discogs.enrich_playlist}

    @router.post("/playlist/{pid}/enrich/{source}")
    def playlist_enrich(pid: int, source: str):
        """Kick off a background enrichment job (source: 'musicbrainz' = genre+year, 'lastfm' = genre)."""
        if store.get_playlist(pid) is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        runner = ENRICH_SOURCES.get(source)
        if runner is None:
            raise HTTPException(status_code=404, detail="unknown enrichment source")
        # Rejoin an already-running job for this playlist+source instead of starting a duplicate
        # (double-click, or the same playlist open in two tabs) — which would double the API load
        # and race writes. A different source is allowed to run alongside (separate rate gate).
        active = ctx.jobs.find_active(pid)
        if active is not None and active.source == source:
            return JSONResponse({"job_id": active.id})
        job = ctx.jobs.create()
        job.playlist_id = pid           # so a refreshed page can find + rejoin this job
        job.source = source

        def run():
            try:
                runner(store, pid, on_progress=job.events.append)
            except Exception as e:  # noqa: BLE001
                detail = str(e) or type(e).__name__
                job.error = detail
                job.events.append({"type": "err", "text": f"enrichment failed: {detail}"})
            finally:
                job.done = True

        threading.Thread(target=run, daemon=True).start()
        return JSONResponse({"job_id": job.id})

    @router.get("/playlist/enrich/events/{job_id}")
    async def playlist_enrich_events(request: Request, job_id: int):
        job = ctx.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such enrichment job")

        # Render each track's <tr> server-side so the live enrich update and a manual edit produce
        # identical cells (the page's applyRow just drops in row_html — no client HTML building).
        # Album jobs render the album row partial from the album's folded-in tracks instead.
        if job.album_browse is not None:
            row_tmpl = templates.env.get_template("_partials/album_track_row.html")
            rows = store.album_tracks_detail(job.album_browse)
        else:
            row_tmpl = templates.env.get_template("_partials/track_row.html")
            rows = store.playlist_tracks_detail(job.playlist_id) if job.playlist_id is not None else []
        base, idx_of = {}, {}
        for i, t in enumerate(rows, start=1):
            base[t["video_id"]] = t
            idx_of[t["video_id"]] = i

        def _with_row(ev):
            vid = ev.get("video_id")
            if ev.get("type") != "track" or vid not in base:
                return ev
            t = dict(base[vid])
            if "genre" in ev:
                t["genre"] = ev["genre"]
            if "year" in ev:
                t["year"] = ev["year"]
            return {**ev, "row_html": row_tmpl.render(t=t, idx=idx_of[vid])}

        async def gen():
            sent = 0
            while True:
                while sent < len(job.events):
                    yield f"data: {json.dumps(_with_row(job.events[sent]))}\n\n"
                    sent += 1
                if job.done:
                    yield f"data: {json.dumps({'type': 'end', 'error': job.error})}\n\n"
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @router.post("/playlist/{pid}/rename")
    async def playlist_rename(pid: int, request: Request):
        title = ((await request.form()).get("title") or "").strip()
        if not title:
            return _toast(request, "name can't be empty")
        try:
            await asyncio.to_thread(ctx.ops().rename, pid, title)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("rename of playlist %s failed", pid)
            return _toast(request, "YouTube returned an unexpected response")
        return templates.TemplateResponse(request, "_partials/playlist_head.html",
                                          {"pl": store.get_playlist(pid)})

    @router.post("/settings/lastfm-key")
    async def set_lastfm_key(request: Request):
        store.set_setting("lastfm_api_key", ((await request.form()).get("key") or "").strip())
        return Response(status_code=204)

    @router.post("/playlist/{pid}/track-genre")
    async def playlist_set_track_genre(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        genre = (form.get("genre") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        tid = store.track_ids_for_videos([vid]).get(vid)
        if tid is None:
            return _toast(request, "track not found")
        store.set_track_genre(tid, genre)
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/track-year")
    async def playlist_set_track_year(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        year = (form.get("year") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        tid = store.track_ids_for_videos([vid]).get(vid)
        if tid is None:
            return _toast(request, "track not found")
        store.set_track_year(tid, year)
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/remove-track")
    async def playlist_remove_track(pid: int, request: Request):
        vid = ((await request.form()).get("video_id") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            await asyncio.to_thread(ctx.ops().remove_track, pid, vid)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("remove-track failed for playlist %s", pid)
            return _toast(request, "YouTube returned an unexpected response")
        return HTMLResponse("")                       # htmx swaps empty -> the row is removed

    @router.post("/playlist/{pid}/reorder")
    async def playlist_reorder(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        before = (form.get("before_video_id") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            await asyncio.to_thread(ctx.ops().reorder_track, pid, vid, before)
        except Exception:  # noqa: BLE001  (the DOM already moved; reload to resync the true order)
            logger.exception("reorder failed for playlist %s", pid)
            return _refresh()
        return Response(status_code=204)              # success: order persisted, nothing to swap

    @router.post("/playlist/{pid}/promote")
    def playlist_promote(pid: int):
        """Promote a Generated playlist into the library: move it out of the quarantine group so it
        counts as a real playlist and starts shaping the taste model (graduation by user intent)."""
        pl = store.get_playlist(pid)
        if pl is not None:
            store.set_playlist_group(pl.ytm_playlist_id, "")
        return _refresh()

    @router.post("/playlists/group")
    async def playlists_group(request: Request):
        form = await request.form()
        name = form.get("name", "")
        for pid in _ids(form):
            pl = store.get_playlist(pid)
            if pl is not None:
                store.set_playlist_group(pl.ytm_playlist_id, name)
        return _refresh()

    @router.post("/playlists/copy")
    async def playlists_copy(request: Request):
        form = await request.form()
        ids = _ids(form)
        if ids:
            try:
                await asyncio.to_thread(ctx.ops().copy, ids, form.get("name", ""))
            except Exception:  # noqa: BLE001  (errors surface on the reloaded page, as before)
                logger.exception("copy of %s failed", ids)
        return _refresh()

    @router.post("/playlists/copy-into")
    async def playlists_copy_into(request: Request):
        form = await request.form()
        ids = _ids(form)
        try:
            target = int(form.get("target") or 0)
        except ValueError:
            target = 0
        if not ids or not target:
            return _toast(request, "Pick a destination playlist.")
        try:
            await asyncio.to_thread(ctx.ops().copy_into, ids, target)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001  (errors surface on the reloaded page, as before)
            logger.exception("copy-into %s -> %s failed", ids, target)
            return _toast(request, "Copy failed — see the log.")
        return _refresh()

    @router.post("/playlists/delete")
    async def playlists_delete(request: Request):
        ops = ctx.ops()
        form = await request.form()
        for pid in _ids(form):
            pl = store.get_playlist(pid)
            if pl is None:
                continue
            if pl.ytm_playlist_id in SYSTEM_PLAYLIST_IDS:
                # undeletable system playlists (Liked Music, Episodes for Later) -> just hide locally
                store.hide_playlist(pl.ytm_playlist_id)
                continue
            try:
                await asyncio.to_thread(ops.delete, pid)
            except Exception:  # noqa: BLE001  (errors surface on the reloaded page, as before)
                logger.exception("playlists delete of %s failed", pl.ytm_playlist_id)
        return _refresh()

    return router
