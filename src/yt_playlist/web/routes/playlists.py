"""Playlists tab: browse every playlist, sort, assign user groups, and run bulk actions."""
import asyncio
from yt_playlist.core.activity import activity_for
import json
import re
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from yt_playlist.providers import lastfm
from yt_playlist.rec import journeys, rec_params, recommend
from yt_playlist.repos.base import GENERATED_GROUP
from yt_playlist.library import executor
from yt_playlist.library.playlist_positions import pending_insertions
from yt_playlist.library.analysis import SYSTEM_PLAYLIST_IDS
from yt_playlist.library.live_plays import resolve_identity
from yt_playlist.web.jobs import start_playlist_enrichment


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates, logger = ctx.store, ctx.now_fn, ctx.templates, ctx.logger

    def _ids(form):
        return [int(x) for x in (form.get("ids", "") or "").split(",") if x.strip().isdigit()]

    def _refresh():
        # htmx sees this header and does a full page reload, exact parity with the old
        # location.reload(); the list re-renders from the server as it did before.
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    def _action_result(result):
        # Keep the result with the existing job registry so the refreshed URL can display it
        # repeatedly without losing an error or replaying an already completed mutation.
        job = ctx.jobs.create(kind="playlist_action_result")
        job.result, job.outcome, job.done = result, result["status"], True
        return JSONResponse(result, status_code=422 if job.outcome == "failed" else 200,
                            headers={"HX-Redirect": f"/playlists?result={job.id}",
                                     "X-Action-Outcome": job.outcome})

    def _copy_result(result, *, new=True):
        skipped = result.get("skipped", 0)
        status = "partial" if skipped else "complete"
        return _action_result({"status": status, "operation": "copy",
            "message": f"Copied {result['added']} track(s) to {result['title']}; {skipped} track(s) skipped.",
            "copy": result, "items": [],
            "guidance": (("Inspect the destination and copy only the missing songs. "
                          "Repeating Copy creates another playlist." if new else
                          "Inspect the destination and retry Copy into for missing songs; songs already present are skipped.")
                         if skipped else "")})

    def _is_generated(pid):
        pl = store.get_playlist(pid)
        return pl is not None and store.get_playlist_groups().get(pl.ytm_playlist_id) == GENERATED_GROUP

    def _record_alt_version_events(pid, old_video_id, tracks):
        # #95: choosing an alternate version off the alternates modal is the user saying "these two
        # are the same song to me" - future dedupe/preference evidence (see library/player_events.py
        # KINDS note). Server-written, one row per chosen version, only when after_video_id (the
        # anchor set exclusively by that modal's form) is present, so a plain suggestion-add never
        # fires it. Best-effort only: no identity configured yet means nothing to attribute the
        # event to, so skip silently rather than let it break the route's own response.
        try:
            ident = resolve_identity(store, "")
            if ident is None:
                return
            pl = store.get_playlist(pid)
            if pl is None:
                return
            now = now_fn()
            for t in tracks:
                new_video_id = t.get("videoId")
                if not new_video_id:
                    continue
                payload = json.dumps({"old": old_video_id, "new": new_video_id})
                store.record_player_event(ident, "alt_version", new_video_id, None, None,
                                          pl.ytm_playlist_id, payload, now)
        except Exception:  # noqa: BLE001  (analytics must never 500 a route whose add succeeded)
            logger.debug("alt_version event not recorded for playlist %s", pid, exc_info=True)

    def _refresh_ytm_view_id(ctx, ytm_id):
        # #95 variant taking the YTM id directly, for callers whose local row is already gone by
        # the time they fire (delete: the id must be captured before the row is removed).
        try:
            if ytm_id:
                ctx.bridge.send_control({"type": "refresh-view", "playlist": ytm_id})
        except Exception:  # noqa: BLE001
            logger.debug("refresh-view control not sent for %s", ytm_id, exc_info=True)

    def _refresh_ytm_view(ctx, store, pid):
        # #95: tell a connected extension to reload the YTM tab if it's sitting on this playlist, so
        # a server-side edit (rename, track add/remove/reorder) doesn't leave a stale view on screen.
        # Best-effort only: no extension connected raises, and that must never affect the route's
        # own response.
        try:
            pl = store.get_playlist(pid)
        except Exception:  # noqa: BLE001
            return
        if pl is not None:
            _refresh_ytm_view_id(ctx, pl.ytm_playlist_id)

    def _track_row(request, pid, video_id):
        # Re-render one track's <tr> (the shared swap unit for manual edits and enrich).
        gen = _is_generated(pid)
        for i, t in enumerate(store.playlist_tracks_detail(pid), start=1):
            if t["video_id"] == video_id:
                return templates.TemplateResponse(request, "_partials/track_row.html",
                    {"t": t, "idx": i, "is_generated": gen,
                     "mood_states": recommend.track_mood_states(store, now_fn()) if gen else {}})
        return Response(status_code=204)         # row no longer present, nothing to swap

    @router.get("/playlists")
    def playlists_page(request: Request):
        labels = {i.id: i.label for i in store.get_identities()}
        groups = store.get_playlist_groups()                 # ytm -> group name
        recipe_dates = store.get_recipe_created_ats()
        stats = store.get_playlist_listen_stats()            # pid -> (last_ts, count)
        thumbnails = store.get_playlist_thumbnails()         # cover or first song artwork
        hidden = store.get_hidden_playlists()                # ytm of playlists hidden from this tab
        now = now_fn()
        cutoff = now - 7 * 86400
        rows = []
        for p in store.get_playlists():
            if p.ytm_playlist_id in hidden:
                continue
            last, listens = stats.get(p.id, (None, 0))
            group = groups.get(p.ytm_playlist_id, "")
            generated_created = p.created_at
            if group == GENERATED_GROUP and generated_created is None:
                generated_created = recipe_dates.get(p.ytm_playlist_id)
                # Another install's generated playlists have no local creation timestamp.
                # Use their dated title for ordering only; keep actual creation metadata unknown.
                if generated_created is None:
                    dated = re.search(r" - ([A-Za-z]+ \d{1,2} \d{4})(?: #\d+)?$", p.title or "")
                    if dated:
                        try:
                            generated_created = datetime.strptime(dated[1], "%B %d %Y").timestamp()
                        except ValueError:
                            pass
            rows.append({
                "id": p.id, "ytm": p.ytm_playlist_id, "title": p.title,
                "identity": labels.get(p.identity_id, "?"), "thumbnail": thumbnails.get(p.id),
                "count": p.track_count, "kind": store.playlist_kind(p.id),
                "group": group,
                "last": last, "listens": listens,
                "created": p.created_at,
                "generated_created": generated_created,
                "is_new": (group != GENERATED_GROUP and p.ytm_playlist_id not in SYSTEM_PLAYLIST_IDS
                           and p.created_at is not None and cutoff <= p.created_at <= now),
            })
        group_names = sorted({g for g in groups.values() if g}, key=str.lower)
        # Generated playlists are pinned in their own card, so they do not make the
        # main table's grouping controls or Group column useful on their own.
        has_groups = any(r["group"] and r["group"] != GENERATED_GROUP for r in rows)
        result_id = request.query_params.get("result", "")
        result_job = ctx.jobs.get(int(result_id)) if result_id.isdigit() else None
        action_result = (result_job.result if result_job and result_job.kind == "playlist_action_result" else None)
        return templates.TemplateResponse(request, "playlists.html", {
            "rows": rows, "has_groups": has_groups, "group_names": group_names,
            "gc_days": rec_params.get_param(store, "generated_gc_days"),
            "flash": request.query_params.get("flash"),
            "flash_pl": request.query_params.get("flash_pl"),
            "action_result": action_result,
        })

    @router.get("/playlist/{pid}")
    def playlist_detail(request: Request, pid: int):
        pl = store.get_playlist(pid)
        if pl is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        labels = {i.id: i.label for i in store.get_identities()}
        tracks = store.playlist_tracks_detail(pid)
        from yt_playlist.rec import measurement
        measurement.record_action(store, "open", now_fn(), playlist_id=pid)
        is_generated = store.get_playlist_groups().get(pl.ytm_playlist_id) == GENERATED_GROUP
        recipe = store.get_recipe(pl.ytm_playlist_id) if is_generated else None
        return templates.TemplateResponse(request, "playlist.html", {
            "pl": pl, "tracks": tracks, "identity": labels.get(pl.identity_id, "?"),
            "show_identity": len(labels) > 1,
            "is_generated": is_generated,
            "facets": recommend.playlist_facets(store, pid) if is_generated else None,
            "mood_state": recommend.playlist_mood_state(store, pid, now_fn()) if is_generated else 0,
            "mood_states": recommend.track_mood_states(store, now_fn()) if is_generated else {},
            "recipe": recipe,
            # #48: a cluster-derived playlist whose canvas we saved can be reopened and regrown
            "has_cluster_canvas": bool(store.get_cluster_canvas(pl.ytm_playlist_id)) if is_generated else False,
            "journey_label": journeys.JOURNEY_LABELS.get((recipe or {}).get("journey")) if is_generated else None,
            "journey_desc": journeys.JOURNEY_DESCRIPTIONS.get((recipe or {}).get("journey")) if is_generated else None,
            "kind": store.playlist_kind(pid), "total_plays": sum(t["plays"] for t in tracks),
            # autosuggest = the editable whitelist plus whatever genres already exist in the library
            "genres": sorted(set(store.get_genre_whitelist()) | set(store.all_genres()), key=str.lower),
            "lastfm_configured": lastfm.api_key(store) is not None,
            "conflict_count": store.conflict_count_for_playlist(pid),  # drives the header conflict icon
            "active_job": ctx.jobs.find_active(pid),     # an in-progress enrichment to rejoin, if any
            "position_pending": len(pending_insertions(store, pid)),
        })

    @router.get("/playlist/{pid}/cells")
    def playlist_cells(pid: int):
        # Lightweight genre/year snapshot so the open playlist page can fill in cells as the background
        # enrichment worker resolves them (polled on the same loop that highlights the now-playing row).
        if store.get_playlist(pid) is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        tracks = store.playlist_tracks_detail(pid)
        cells = [{"video_id": t["video_id"], "genre": t.get("genre") or "", "year": t.get("year") or ""}
                 for t in tracks]
        pending = sum(1 for t in tracks if not (t.get("genre") and t.get("year")))
        return JSONResponse({"cells": cells, "pending": pending})

    @router.get("/playlist/{pid}/share.txt")
    def playlist_share(pid: int):
        """Download the playlist as a plain .txt (one song URL per line) for easy sharing."""
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
        ctx_data = {"results": [], "source_video_id": video_id}
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
        # each selected alternate posts as a "track" field carrying its full track JSON; the optional
        # after_video_id anchors the insert just below that existing track (else they append at the end)
        form = await request.form()
        tracks = []
        for raw in form.getlist("track"):
            try:
                tracks.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
        if not tracks:
            return _toast(request, "select at least one version to add")
        after_video_id = form.get("after_video_id") or None
        from_suggestion = form.get("suggestion") == "1" and len(tracks) == 1 and not after_video_id
        before = {t["video_id"] for t in store.playlist_tracks_detail(pid)}
        try:
            await activity_for(ctx).to_thread(ctx.ops().add_tracks, pid, tracks, after_video_id)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("add-tracks failed for playlist %s", pid)
            return _toast(request, "YouTube returned an unexpected response")
        _refresh_ytm_view(ctx, store, pid)
        if after_video_id:
            _record_alt_version_events(pid, after_video_id, tracks)
        from yt_playlist.rec import measurement
        present = {t["video_id"] for t in store.playlist_tracks_detail(pid)}
        for track in tracks:
            video_id = track.get("videoId")
            if video_id in present and video_id not in before:
                measurement.record_action(store, "save", now_fn(), video_id=video_id,
                                          generated=_is_generated(pid))
        if from_suggestion:
            video_id = tracks[0].get("videoId")
            details = store.playlist_tracks_detail(pid)
            added = next((t for t in reversed(details) if t["video_id"] == video_id), None)
            if added is not None:
                if not added.get("genre"):
                    store.requeue_enrichment([added["track_id"]])
                    if ctx.enrich_worker:
                        ctx.enrich_worker.trigger()
                gen = _is_generated(pid)
                return templates.TemplateResponse(request, "_partials/suggestion_added.html", {
                    "t": added, "idx": len(details), "is_generated": gen,
                    "mood_states": recommend.track_mood_states(store, now_fn()) if gen else {},
                })
        return _refresh()                             # reload so the new tracks drop into the table

    @router.post("/playlist/{pid}/retry-placement")
    async def playlist_retry_placement(pid: int, request: Request):
        try:
            await activity_for(ctx).to_thread(ctx.ops().retry_track_placement, pid)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("retrying track placement failed for playlist %s", pid)
            return _toast(request, "Couldn’t retry placement on YouTube. Try again after reconnecting.")
        _refresh_ytm_view(ctx, store, pid)
        return _refresh()

    @router.post("/playlist/{pid}/enrich")
    def playlist_enrich(pid: int):
        """Kick off the enrichment waterfall: every enabled provider, in configured order, over this
        playlist's not-yet-complete tracks. Logs each provider response and records disagreements."""
        if store.get_playlist(pid) is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        job = start_playlist_enrichment(ctx, pid)
        return JSONResponse({"job_id": job.id})

    @router.get("/playlist/{pid}/conflicts")
    def playlist_conflicts(request: Request, pid: int):
        """The conflict-resolver modal: unresolved provider disagreements for this playlist's tracks."""
        return templates.TemplateResponse(request, "_partials/conflicts_modal.html", {
            "conflicts": store.unresolved_conflicts_for_playlist(pid),
            "resolve_url": f"/playlist/{pid}/conflicts/resolve",
            "count_left": store.conflict_count_for_playlist(pid)})

    @router.post("/playlist/{pid}/conflicts/resolve")
    async def playlist_resolve_conflicts(request: Request, pid: int):
        """Apply the user's picks. Each `<track_id>:<field>` form key carries the chosen value;
        it overwrites the canonical column and marks that conflict resolved."""
        form = await request.form()
        for key, value in form.multi_items():
            if ":" not in key or not value:
                continue
            tid, _, field = key.partition(":")
            if tid.isdigit():
                try:
                    store.resolve_conflict(int(tid), field, value)
                except ValueError:
                    pass
        return templates.TemplateResponse(request, "_partials/conflicts_modal.html", {
            "conflicts": store.unresolved_conflicts_for_playlist(pid),
            "resolve_url": f"/playlist/{pid}/conflicts/resolve",
            "count_left": store.conflict_count_for_playlist(pid)})

    @router.get("/playlist/enrich/events/{job_id}")
    async def playlist_enrich_events(request: Request, job_id: int):
        job = ctx.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such enrichment job")

        # Events are invalidations, including replay after reconnect. Render the complete row
        # from current state at delivery, so old events cannot undo later edits or user choices.
        if job.album_browse is not None:
            row_tmpl = templates.env.get_template("_partials/album_track_row.html")
        else:
            row_tmpl = templates.env.get_template("_partials/track_row.html")

        def _with_row(ev):
            vid = ev.get("video_id")
            if ev.get("type") != "track":
                return ev
            rows = (store.album_tracks_detail(job.album_browse) if job.album_browse is not None else
                    store.playlist_tracks_detail(job.playlist_id) if job.playlist_id is not None else [])
            gen_flag = _is_generated(job.playlist_id) if job.playlist_id is not None else False
            for index, track in enumerate(rows, start=1):
                if track["video_id"] == vid:
                    mood_states = recommend.track_mood_states(store, now_fn()) if gen_flag else {}
                    return {**ev, "genre": track["genre"], "year": track["year"],
                            "row_html": row_tmpl.render(t=track, idx=index, is_generated=gen_flag,
                                                        mood_states=mood_states)}
            return {k: v for k, v in ev.items() if k != "row_html"}  # removed tracks stay removed

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
            await activity_for(ctx).to_thread(ctx.ops().rename, pid, title)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("rename of playlist %s failed", pid)
            return _toast(request, "YouTube returned an unexpected response")
        _refresh_ytm_view(ctx, store, pid)
        return templates.TemplateResponse(request, "_partials/playlist_head.html",
                                          {"pl": store.get_playlist(pid)})

    @router.post("/settings/lastfm-key")
    async def set_lastfm_key(request: Request):
        store.set_setting("lastfm_api_key", ((await request.form()).get("key") or "").strip())
        if ctx.enrich_worker:
            ctx.enrich_worker.trigger()
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
        ctx.rec_worker.trigger_mode_refresh()
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
        ctx.rec_worker.trigger_mode_refresh()
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/track-title")
    async def playlist_set_track_title(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        title = (form.get("title") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        tid = store.track_ids_for_videos([vid]).get(vid)
        if tid is None:
            return _toast(request, "track not found")
        store.set_track_title(tid, title)
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/track-artist")
    async def playlist_set_track_artist(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        artist = (form.get("artist") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        tid = store.track_ids_for_videos([vid]).get(vid)
        if tid is None:
            return _toast(request, "track not found")
        store.set_track_artist(tid, artist)
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/track-reset")
    async def playlist_reset_track_field(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        field = (form.get("field") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        if field not in ("title", "artist"):
            return _toast(request, "bad field")
        tid = store.track_ids_for_videos([vid]).get(vid)
        if tid is None:
            return _toast(request, "track not found")
        if field == "title":
            store.reset_track_title(tid)
        else:
            store.reset_track_artist(tid)
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/remove-track")
    async def playlist_remove_track(pid: int, request: Request):
        vid = ((await request.form()).get("video_id") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            await activity_for(ctx).to_thread(ctx.ops().remove_track, pid, vid)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("remove-track failed for playlist %s", pid)
            return _toast(request, "YouTube returned an unexpected response")
        _refresh_ytm_view(ctx, store, pid)
        return HTMLResponse("")                       # htmx swaps empty -> the row is removed

    @router.post("/playlist/{pid}/reorder")
    async def playlist_reorder(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        before = (form.get("before_video_id") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            await activity_for(ctx).to_thread(ctx.ops().reorder_track, pid, vid, before)
        except Exception:  # noqa: BLE001  (the DOM already moved; reload to resync the true order)
            logger.exception("reorder failed for playlist %s", pid)
            return _refresh()
        _refresh_ytm_view(ctx, store, pid)
        return Response(status_code=204)              # success: order persisted, nothing to swap

    @router.post("/playlist/{pid}/promote")
    def playlist_promote(pid: int):
        """Promote a Generated playlist into the library: move it out of the quarantine group so it
        counts as a real playlist and starts shaping the taste model (graduation by user intent).

        The quarantine marker lives in the playlist's YouTube description (executor), because that is
        the only thing your machines share - so promoting rewrites it there too. Without that, this
        machine releases the playlist and every other one keeps quarantining it, and even this one
        re-quarantines it on the next sync (clearing a group deletes the row, leaving no local trace
        of the decision). Best-effort: if the write fails, the local promotion still stands."""
        pl = store.get_playlist(pid)
        if pl is None:
            return _refresh()
        was_generated = store.get_playlist_groups().get(pl.ytm_playlist_id) == "Generated"
        store.set_playlist_group(pl.ytm_playlist_id, "")
        if was_generated:
            from yt_playlist.rec import measurement
            measurement.record_action(store, "promotion", now_fn(), playlist_id=pid,
                                      event_id=f"promote:{pl.ytm_playlist_id}")
        client = (ctx.client_provider() or {}).get(pl.identity_id)
        if client is not None:
            try:
                client.edit_playlist(pl.ytm_playlist_id,
                                     description=executor.PROMOTED_DESCRIPTION)
            except Exception as e:  # noqa: BLE001 - the promotion is local truth either way
                logger.warning("couldn't clear the generated marker on %s: %s",
                               pl.ytm_playlist_id, e)
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
        try:
            result = await activity_for(ctx).to_thread(ctx.ops().copy, ids, form.get("name", ""))
        except Exception as exc:
            logger.exception("copy of %s failed", ids)
            return _action_result({"status": "failed", "operation": "copy",
                "message": "Copy could not be completed.", "items": [{"id": pid,
                    "title": pl.title if (pl := store.get_playlist(pid)) else f"Playlist #{pid}",
                    "status": "failed", "detail": str(exc) or type(exc).__name__} for pid in ids],
                "guidance": "Check YouTube for a newly created playlist before retrying; the response may have been lost."})
        return _copy_result(result)

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
            result = await activity_for(ctx).to_thread(ctx.ops().copy_into, ids, target)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001  (errors surface on the reloaded page, as before)
            logger.exception("copy-into %s -> %s failed", ids, target)
            return _toast(request, "Copy failed. See the log.")
        _refresh_ytm_view(ctx, store, target)   # #95: the TARGET playlist changed on YouTube
        return _copy_result({**result, "new_ytm": result["target_ytm"]}, new=False)

    @router.post("/playlists/delete")
    async def playlists_delete(request: Request):
        ops = ctx.ops()
        form = await request.form()
        items = []
        for pid in dict.fromkeys(_ids(form)):
            pl = store.get_playlist(pid)
            if pl is None:
                items.append({"id": pid, "title": f"Playlist #{pid}", "status": "absent",
                              "detail": "Already absent from the local library; no delete sent."})
                continue
            if pl.ytm_playlist_id in SYSTEM_PLAYLIST_IDS:
                # undeletable system playlists (Liked Music, Episodes for Later) -> just hide locally
                store.hide_playlist(pl.ytm_playlist_id)
                items.append({"id": pid, "title": pl.title, "status": "hidden",
                              "detail": "Hidden from TuneConsole; system playlist kept on YouTube."})
                continue
            try:
                await activity_for(ctx).to_thread(ops.delete, pid)
            except Exception as exc:
                logger.exception("playlists delete of %s failed", pl.ytm_playlist_id)
                items.append({"id": pid, "title": pl.title, "status": "failed",
                              "detail": str(exc) or type(exc).__name__})
            else:
                items.append({"id": pid, "title": pl.title, "status": "deleted", "detail": "Deleted."})
                # #95: a YTM tab sitting on the deleted playlist should show it's gone, not a stale
                # tracklist. The local row is removed by the delete, hence the id-taking variant.
                _refresh_ytm_view_id(ctx, pl.ytm_playlist_id)
        # The home card's pending-cleanup count/icons come from a cached summary; recompute it now
        # so deleted playlists stop showing as pending cleanups until the next worker rebuild (#73).
        try:
            await activity_for(ctx).to_thread(recommend.refresh_cleanup, store, now_fn())
        except Exception:
            logger.exception("cleanup refresh after playlist deletion failed")
        failed = sum(item["status"] == "failed" for item in items)
        status = "partial" if failed and failed < len(items) else "failed" if failed or not items else "complete"
        return _action_result({"status": status, "operation": "delete", "items": items,
            "message": f"Playlist deletion: {len(items) - failed} completed or already absent; {failed} failed.",
            "guidance": ("Check YouTube before retrying failed items; a request may have completed remotely. "
                         "Completed items are listed separately and are no longer selected." if failed else "")})

    return router
