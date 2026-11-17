"""Playlists tab: browse every playlist, sort, assign user groups, and run bulk actions."""
from yt_playlist.core.activity import activity_for
from yt_playlist.core.action_errors import ActionInputError
import json
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from yt_playlist.rec import journeys
from yt_playlist.library.playlist_mutation import PlaylistWriteUnconfirmed
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.web.playlist_heading import title_context


def build(ctx) -> APIRouter:
    router = APIRouter()
    templates, logger = ctx.templates, ctx.logger

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
        result["result_id"] = job.id
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

    def _track_row(request, pid, video_id):
        from yt_playlist.web.context import Ctx
        row = Ctx.playlist_queries(ctx).row(video_id, pid=pid)
        if row:
            return templates.TemplateResponse(request, "_partials/track_row.html", row,
                headers={"HX-Trigger": "playlist-metadata-changed"})
        return Response(status_code=204)         # row no longer present, nothing to swap

    @router.get("/playlists")
    def playlists_page(request: Request):
        from yt_playlist.web.context import Ctx
        return templates.TemplateResponse(request, "playlists.html", {
            **Ctx.playlist_queries(ctx).listing(request.query_params.get('result', '')),
            "flash": request.query_params.get("flash"),
            "flash_pl": request.query_params.get("flash_pl"),
        })

    @router.get("/playlist/{pid}")
    def playlist_detail(request: Request, pid: int):
        from yt_playlist.web.context import Ctx
        data = Ctx.playlist_queries(ctx).detail(pid)
        if data is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        pl, is_generated, build_recipe = data['pl'], data['is_generated'], data.pop('build_recipe')
        recipe = data['recipe']
        return templates.TemplateResponse(request, "playlist.html", {
            **data,
            **title_context(pl.title, is_generated=is_generated, recipe=build_recipe, tracks=data['tracks']),
            "journey_label": journeys.JOURNEY_LABELS.get((recipe or {}).get("journey")) if is_generated else None,
            "journey_desc": journeys.JOURNEY_DESCRIPTIONS.get((recipe or {}).get("journey")) if is_generated else None,
        })

    @router.get("/playlist/{pid}/cells")
    def playlist_cells(pid: int):
        # Lightweight genre/year snapshot so the open playlist page can fill in cells as the background
        # enrichment worker resolves them (polled on the same loop that highlights the now-playing row).
        from yt_playlist.web.context import Ctx
        data = Ctx.playlist_queries(ctx).cells(pid)
        if data is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        return JSONResponse(data)

    @router.get('/playlist/{pid}/snapshot-report')
    def playlist_snapshot_report(pid: int):
        """Read-only support report scoped to a saved playlist's configured account."""
        from yt_playlist.web.context import Ctx
        try:
            result = Ctx.playlist_queries(ctx).snapshot(pid=pid)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if result is None:
            raise HTTPException(404, 'Playlist not found')
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @router.get('/liked-music/{account_id}/snapshot-report')
    def liked_music_snapshot_report(account_id: int):
        """Inspect an account even when its partial Liked Music read was never cached."""
        from yt_playlist.web.context import Ctx
        try:
            result = Ctx.playlist_queries(ctx).snapshot(account=account_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @router.get("/playlist/{pid}/feedback-facets")
    def playlist_feedback_facets(request: Request, pid: int):
        from yt_playlist.web.context import Ctx
        data = Ctx.playlist_queries(ctx).feedback(pid)
        if data is None:
            raise HTTPException(status_code=404, detail="generated playlist not found")
        return templates.TemplateResponse(request, "_partials/feedback_facets.html", data)

    @router.get("/playlist/{pid}/share.txt")
    def playlist_share(pid: int):
        """Download the playlist as a plain .txt (one song URL per line) for easy sharing."""
        from yt_playlist.web.context import Ctx
        pl, tracks = Ctx.playlist_queries(ctx).share(pid)
        if pl is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        lines = [f"https://music.youtube.com/watch?v={t['video_id']}"
                 for t in tracks if t.get("video_id")]
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
        except ActionInputError as e:
            return JSONResponse({'validation': e.code}, status_code=422)
        except Exception:  # noqa: BLE001
            logger.exception("alternate search failed for %s in playlist %s", video_id, pid)
            return Response(status_code=503)
        return templates.TemplateResponse(request, "_partials/alternates_results.html", ctx_data,
                                          headers={'X-Alternate-Results': 'true'})

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
        try:
            outcome = await activity_for(ctx).to_thread(ctx.library_actions().add_tracks, pid, tracks,
                                                       after_video_id, suggestion=from_suggestion)
        except (PlaylistWriteUnconfirmed, PlaylistAddUncertain):
            logger.exception("add-tracks unconfirmed for playlist %s", pid)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        except ActionInputError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("add-tracks failed for playlist %s", pid)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        if from_suggestion:
            details, added = outcome['details'], outcome['added']
            if added is not None:
                from yt_playlist.web.context import Ctx
                row = Ctx.playlist_queries(ctx).row(added['video_id'], pid=pid)
                return templates.TemplateResponse(request, "_partials/suggestion_added.html", row)
        return _refresh()                             # reload so the new tracks drop into the table

    @router.post("/playlist/{pid}/retry-placement")
    async def playlist_retry_placement(pid: int, request: Request):
        try:
            await activity_for(ctx).to_thread(ctx.library_actions().mutate, 'retry_track_placement', pid)
        except ActionInputError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("retrying track placement failed for playlist %s", pid)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        return _refresh()

    @router.post("/playlist/{pid}/enrich")
    def playlist_enrich(pid: int):
        """Kick off the enrichment waterfall: every enabled provider, in configured order, over this
        playlist's not-yet-complete tracks. Logs each provider response and records disagreements."""
        try:
            job = ctx.enrichment_commands().start(playlist_id=pid)
        except LookupError:
            raise HTTPException(status_code=404, detail="playlist not found") from None
        return JSONResponse({"job_id": job.id})

    @router.get("/playlist/{pid}/conflicts")
    def playlist_conflicts(request: Request, pid: int):
        """The conflict-resolver modal: unresolved provider disagreements for this playlist's tracks."""
        return templates.TemplateResponse(request, "_partials/conflicts_modal.html", {
            "resolve_url": f"/playlist/{pid}/conflicts/resolve",
            **ctx.enrichment_commands().conflicts(playlist_id=pid)})

    @router.post("/playlist/{pid}/conflicts/resolve")
    async def playlist_resolve_conflicts(request: Request, pid: int):
        """Apply the user's picks. Each `<track_id>:<field>` form key carries the chosen value;
        it overwrites the canonical column and marks that conflict resolved."""
        form = await request.form()
        ctx.enrichment_commands().resolve_many(form.multi_items())
        return templates.TemplateResponse(request, "_partials/conflicts_modal.html", {
            "resolve_url": f"/playlist/{pid}/conflicts/resolve",
            **ctx.enrichment_commands().conflicts(playlist_id=pid)})

    @router.get("/playlist/enrich/events/{job_id}")
    async def playlist_enrich_events(request: Request, job_id: int):
        from yt_playlist.web.job_events import enrichment_progress, stream_job
        job = ctx.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Enrichment job is missing or expired.")

        # Events are invalidations, including replay after reconnect. Render the complete row
        # from current state at delivery, so old events cannot undo later edits or user choices.
        if job.album_browse is not None:
            row_tmpl = templates.env.get_template("_partials/album_track_row.html")
        else:
            row_tmpl = templates.env.get_template("_partials/track_row.html")

        def _with_row(ev):
            vid = ev.get("video_id")
            progress = enrichment_progress(ev)
            if ev.get("type") != "track":
                return progress
            from yt_playlist.web.context import Ctx
            row = Ctx.playlist_queries(ctx).row(vid, pid=job.playlist_id, album=job.album_browse)
            if row:
                return {**progress, 'genre': row['t']['genre'], 'year': row['t']['year'],
                        'row_html': row_tmpl.render(**row)}
            return progress  # removed tracks stay removed

        return StreamingResponse(stream_job(job, request, transform=_with_row,
                                 terminal=lambda: {"type": "end", "error": bool(job.error)}),
                                 media_type="text/event-stream")

    @router.post("/playlist/{pid}/rename")
    async def playlist_rename(pid: int, request: Request):
        title = ((await request.form()).get("title") or "").strip()
        if not title:
            return _toast(request, "name can't be empty")
        try:
            await activity_for(ctx).to_thread(ctx.library_actions().mutate, 'rename', pid, title)
        except PlaylistWriteUnconfirmed:
            logger.exception("rename unconfirmed for playlist %s", pid)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        except ActionInputError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("rename of playlist %s failed", pid)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        from yt_playlist.web.context import Ctx
        heading = Ctx.playlist_queries(ctx).heading(pid)
        return templates.TemplateResponse(request, "_partials/playlist_head.html",
            {"pl": heading['pl'], **title_context(heading['pl'].title,
                is_generated=heading['is_generated'], recipe=heading['recipe'], tracks=heading['tracks'])})

    @router.post("/settings/lastfm-key")
    async def set_lastfm_key(request: Request):
        from yt_playlist.web.context import Ctx
        Ctx.enrichment_configuration(ctx).lastfm_key((await request.form()).get("key") or "")
        return Response(status_code=204)

    @router.post("/playlist/{pid}/track-genre")
    async def playlist_set_track_genre(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        genre = (form.get("genre") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            ctx.library_actions().edit_field(vid, 'genre', genre)
        except ValueError as exc:
            return _toast(request, str(exc))
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/track-year")
    async def playlist_set_track_year(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        year = (form.get("year") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            ctx.library_actions().edit_field(vid, 'year', year)
        except ValueError as exc:
            return _toast(request, str(exc))
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/track-title")
    async def playlist_set_track_title(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        title = (form.get("title") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            ctx.library_actions().edit_field(vid, 'title', title)
        except ValueError as exc:
            return _toast(request, str(exc))
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/track-artist")
    async def playlist_set_track_artist(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        artist = (form.get("artist") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            ctx.library_actions().edit_field(vid, 'artist', artist)
        except ValueError as exc:
            return _toast(request, str(exc))
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
        try:
            ctx.library_actions().edit_field(vid, field, reset=True)
        except ValueError as exc:
            return _toast(request, str(exc))
        return _track_row(request, pid, vid)

    @router.post("/playlist/{pid}/remove-track")
    async def playlist_remove_track(pid: int, request: Request):
        vid = ((await request.form()).get("video_id") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            await activity_for(ctx).to_thread(ctx.library_actions().mutate, 'remove_track', pid, vid)
        except PlaylistWriteUnconfirmed:
            logger.exception("remove-track unconfirmed for playlist %s", pid)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        except ActionInputError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("remove-track failed for playlist %s", pid)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        return HTMLResponse("")                       # htmx swaps empty -> the row is removed

    @router.post("/playlist/{pid}/reorder")
    async def playlist_reorder(pid: int, request: Request):
        form = await request.form()
        vid = (form.get("video_id") or "").strip()
        before = (form.get("before_video_id") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        try:
            await activity_for(ctx).to_thread(ctx.library_actions().mutate, 'reorder_track', pid, vid, before)
        except Exception:  # noqa: BLE001  (the DOM already moved; reload to resync the true order)
            logger.exception("reorder failed for playlist %s", pid)
            return _refresh()
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
        ctx.library_actions().promote(pid)
        return _refresh()

    @router.post("/playlists/group")
    async def playlists_group(request: Request):
        form = await request.form()
        name = form.get("name", "")
        ctx.library_actions().group(_ids(form), name)
        return _refresh()

    @router.post("/playlists/copy")
    async def playlists_copy(request: Request):
        form = await request.form()
        ids = _ids(form)
        name = form.get("name", "")
        try:
            result = await activity_for(ctx).to_thread(ctx.library_actions().copy, ids, name)
        except PlaylistAddUncertain as exc:
            logger.warning("copy of %s has unconfirmed additions", ids, exc_info=True)
            created = ctx.library_actions().unconfirmed(exc)
            return _action_result({"status": "partial", "operation": "copy",
                "message": f"Copied {len(exc.confirmed)} song(s); {len(exc.uncertain)} addition(s) not confirmed.",
                "items": [], "source_ids": ids, "copy_name": name,
                "copy": {"new_ytm": exc.playlist_id, "title": created.title if created else "Partial copy",
                         "added": len(exc.confirmed), "skipped": len(exc.skipped),
                         "uncertain": len(exc.uncertain)},
                "guidance": "The partial copy is saved and the source playlists were kept. "
                            "Review the destination and Action Log before recovering. "
                            "Repeating Copy creates another playlist."})
        except Exception:
            logger.exception("copy of %s failed", ids)
            titles = ctx.library_actions().titles(ids)
            return _action_result({"status": "failed", "operation": "copy",
                "message": "Copy not confirmed.", "source_ids": ids, "copy_name": name,
                "items": [{"id": pid,
                    "title": titles[pid],
                    "status": "failed", "detail": "Copy not confirmed."} for pid in ids],
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
            result = await activity_for(ctx).to_thread(ctx.library_actions().copy_into, ids, target)
        except (PlaylistWriteUnconfirmed, PlaylistAddUncertain):
            logger.exception("copy-into %s -> %s unconfirmed", ids, target)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        except ActionInputError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            logger.exception("copy-into %s -> %s failed", ids, target)
            return Response(status_code=503, headers={"HX-Reswap": "none"})
        return _copy_result({**result, "new_ytm": result["target_ytm"]}, new=False)

    @router.post("/playlists/delete")
    async def playlists_delete(request: Request):
        form = await request.form()
        result = await activity_for(ctx).to_thread(ctx.library_actions().delete, _ids(form))
        return _action_result(result)

    return router
