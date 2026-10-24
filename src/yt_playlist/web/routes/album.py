"""Album landing page: cover, track table, save/open, "create a playlist from this album", and
(for saved albums folded into the library) genre/year enrichment with the same live flow as playlists."""
import json
import re
import uuid
from yt_playlist.core.activity import activity_for
from yt_playlist.core.accounts import catalog_client,main_client
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from yt_playlist.library.album_fetch import fetch_album, is_upload_album, shareable_album_tracks
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.providers import enrichment, lastfm, waterfall
from yt_playlist.rec import album_choices, album_notes, choice_evidence
from yt_playlist.util.duration import parse_duration
from yt_playlist.util.thumbnails import best_thumb, is_placeholder_art


_MOSAIC_TILES = 4


def cover_and_mosaic(album, tracks):
    """(cover, mosaic) for the album head. A personal compilation has no cover of its own, and
    YouTube hands back a generic grey disc rather than nothing, so fall back to a mosaic of the
    tracks' own covers: honest that it is a collection, where picking one track's art to stand for
    the whole thing would not be. Returns a single cover when the album really has one."""
    cover = (album or {}).get("thumbnail")
    if cover and not is_placeholder_art(cover):
        return cover, []
    seen = []
    for t in tracks or []:
        art = t.get("thumbnail")
        if art and not is_placeholder_art(art) and art not in seen:
            seen.append(art)
            if len(seen) == _MOSAIC_TILES:
                break
    if len(seen) == 1:            # one cover is not a mosaic, it is just the cover
        return seen[0], []
    return (None, seen) if seen else (cover, [])


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    def _live_album(browse_id):
        clients = ctx.client_provider()
        client = main_client(store, clients) if is_upload_album(browse_id) else catalog_client(clients)
        if not client or not browse_id:
            return None
        a = fetch_album(client, browse_id)
        if not a or not a.get('title'):
            return None
        remember_album(store, browse_id, a)
        return {
            'title': a.get('title'),
            'artist': ', '.join(x.get('name', '') for x in a.get('artists') or []),
            'audio_playlist_id': a.get('audioPlaylistId'),
            'year': a.get('year'), 'thumbnail': best_thumb(a.get('thumbnails')),
            'tracks': [{'title': t.get('title'), 'video_id': t.get('videoId'),
                        'artist': ', '.join(x.get('name', '') for x in t.get('artists') or []),
                        'duration': t.get('duration')} for t in a.get('tracks') or []]}

    @router.get('/album/inline')
    def album_inline(request: Request, browse: str, opportunity: str = ''):
        """Opening a record fetches its tracks without leaving the held Home menu."""
        return templates.TemplateResponse(request, '_partials/album_expansion.html',
            _album_context(browse, opportunity, inline=True))

    @router.get("/album")
    def album_page(request: Request):
        browse_id = (request.query_params.get("browse") or "").strip()
        opportunity = request.query_params.get("opportunity")
        if opportunity:
            choice_evidence.safe_respond(store, opportunity, "open", ctx.now_fn(), option_id=browse_id)
        return templates.TemplateResponse(request, "album.html", _album_context(browse_id, opportunity))

    def _album_context(browse_id, opportunity, *, inline=False):
        """One album, table and set of actions for the page and its Home expansion."""
        album = None
        try:
            album = _live_album(browse_id)
        except Exception:  # noqa: BLE001 - no client / network / parse all degrade gracefully
            ctx.logger.info("album fetch failed for %r (non-fatal)", browse_id, exc_info=True)
        saved = browse_id in store.collection.saved_album_ids()
        upload = is_upload_album(browse_id)
        # An album is "in your library" when you saved it, or when it IS your library: an upload
        # (privately owned release) is yours by definition, and can never appear in saved_albums
        # because saving means liking an audioPlaylistId, which an upload has none of.
        in_library = saved or upload
        # The editable "folded-in library" table is only for library albums (whose full track list has
        # been materialized for enrichment). For any other album, regular sync may still have stamped
        # one incidental track with this album_browse_id (because it's in one of your playlists). That
        # partial subset must NOT shadow the full live-fetched album. Count it for
        # the library-status note, but only display it for saved or uploaded albums.
        library_tracks = store.charts.album_tracks_detail(browse_id) if browse_id else []
        tracks = library_tracks if in_library else []
        # Fold a library album's tracks in ON DEMAND (using the tracks we just fetched live), so
        # enrichment is available the moment you open it, no waiting for a full sync. Fold whenever
        # the live album has more than we hold: an upload typically arrives as a PARTIAL subset (sync
        # stamps only the tracks that appear in a playlist), and a `not tracks` guard would skip the
        # fold entirely for it, leaving 3 of 17 rows on screen.
        if in_library and album and album.get("tracks") and len(tracks) < len(album["tracks"]):
            for t in album["tracks"]:
                if t.get("video_id") and t.get("title"):
                    store.tracks.upsert_track(t["video_id"], t["title"], t.get("artist") or album.get("artist") or "",
                                       album.get("title") or "", parse_duration(t.get("duration")),
                                       album_browse_id=browse_id, thumbnail=album.get("thumbnail"))
            tracks = store.charts.album_tracks_detail(browse_id)
        complete = store.collection.is_album_tracklist_complete(browse_id)
        fallback_tracks = library_tracks if complete else tracks if not inline else []
        if fallback_tracks and not (album and album.get("title")):
            meta = next((a for a in store.collection.get_saved_albums() if a["browse"] == browse_id), {})
            album = {"title": meta.get("title") or fallback_tracks[0]["album"] or "Album",
                     "artist": meta.get("artist") or fallback_tracks[0]["artist"], "year": meta.get("year"),
                     "thumbnail": meta.get("thumbnail") or fallback_tracks[0]["thumbnail"], "tracks": fallback_tracks}
        # Unsaved releases use the same row component. Merge known metadata by exact
        # upload while retaining the full fetched sequence and keeping library ownership intact.
        table_tracks = tracks if album else []
        if album and not in_library:
            known = {t['video_id']: t for t in library_tracks}
            table_tracks = [{
                **known.get(t.get('video_id'), {}),
                'video_id': t.get('video_id') or '', 'title': t.get('title') or '',
                'artist': t.get('artist') or album.get('artist') or '',
                'album': album['title'], 'album_browse': browse_id,
                'thumbnail': t.get('thumbnail') or album.get('thumbnail'),
                'duration': parse_duration(t.get('duration')),
                'genre': known.get(t.get('video_id'), {}).get('genre', ''),
                'year': known.get(t.get('video_id'), {}).get('year') or album.get('year') or '',
                'liked': known.get(t.get('video_id'), {}).get('liked', False),
                'plays': known.get(t.get('video_id'), {}).get('plays'),
            } for t in album.get('tracks') or []]
        cover, mosaic = cover_and_mosaic(album, tracks)
        editorial = {'browse_id': browse_id, 'title': (album or {}).get('title'),
                     'artist': (album or {}).get('artist')}
        if album and not inline:
            store.rec_proposals.request_album_notes([editorial], ctx.now_fn())
            if ctx.rec_worker:
                ctx.rec_worker.trigger_home_refresh()
        prepared = store.rec_proposals.get_proposals("home_prepared") or {}
        anchor = next((a for a in prepared.get("album_choices", []) if a["browse_id"] == browse_id), None)
        companion = []
        if not inline and anchor and anchor.get("companion"):
            from yt_playlist.rec.home_prepared import _eligible_protos
            proposals = _eligible_protos(store, [{"lane": "wheelhouse", "tracks": anchor["companion"]}], ctx.now_fn())
            companion = proposals[0]["tracks"] if proposals else []
        companion_recipe = None
        if companion:
            oid = None
            try:
                oid = choice_evidence.present(store, "mixes", [{"id": "companion", "tracks": companion,
                    "intent": "album_companion", "album_browse_id": browse_id}], ctx.now_fn(),
                    context=prepared.get("choice_context", {}), window=[browse_id, opportunity,
                        int(ctx.now_fn() // 86400)])
            except Exception:
                ctx.logger.warning("Companion mix measurement failed", exc_info=True)
            companion_recipe = {"lane": "album_companion", "anchor_album": browse_id,
                                "opportunity_id": oid, "option_id": "companion"}
        return {
            "album": album, "browse_id": browse_id, "tracks": tracks, "saved": saved,
            "table_tracks": table_tracks,
            "saved_owners": store.collection.saved_album_owners(browse_id).get(browse_id, []),
            "editorial": editorial, "notes": album_notes.read(store, editorial),
            "opportunity_id": opportunity,
            "album_feedback": album_choices.feedback_state(store).get(browse_id, {}).get("kind"),
            "companion": companion, "companion_recipe": companion_recipe,
            "is_upload": upload, "in_library": in_library,
            "library_track_count": len({t["track_id"] for t in library_tracks}),
            "cover": cover, "mosaic": mosaic,
            "genres": sorted(set(store.genres.get_genre_whitelist()) | set(store.genres.all_genres()), key=str.lower),
            "lastfm_configured": lastfm.api_key(store) is not None,
            "conflict_count": store.enrichment_reports.conflict_count_for_album(browse_id)}

    @router.post("/album/feedback")
    async def album_feedback(request: Request):
        form = await request.form()
        try:
            album_choices.set_feedback(store, (form.get("browse_id") or "").strip(),
                form.get("kind"), ctx.now_fn(), form.get("opportunity_id"))
        except ValueError as exc:
            return _toast(request, str(exc))
        if ctx.rec_worker:
            ctx.rec_worker.trigger_home_refresh()
        return Response(status_code=204)

    @router.post("/album/play")
    async def album_play(request: Request):
        form = await request.form()
        browse = (form.get("browse_id") or "").strip()
        opportunity = form.get("opportunity_id")
        action_id = (form.get('action_id') or uuid.uuid4().hex).strip()
        if not browse:
            return _toast(request, "Choose an album to play.")

        def start():
            if not ctx.bridge.connected:
                raise ValueError("Connect the TuneConsole extension to play this album.")
            clients = ctx.client_provider()
            client = main_client(store, clients) if is_upload_album(browse) else catalog_client(clients)
            album = fetch_album(client, browse) if client else {}
            playlist = album.get("audioPlaylistId")
            tracks = album.get("tracks") or []
            video = next((track.get("videoId") for track in tracks if track.get("videoId")), None)
            if not playlist or not video:
                raise ValueError("The complete album queue is unavailable. Open its details and try again.")
            remember_album(store, browse, album)
            if opportunity:
                choice_evidence.safe_respond(store, opportunity, "choice", ctx.now_fn(), option_id=browse,
                    action_id=action_id,
                    details={"playlist": playlist, "video_id": video,
                             "tracks": [{"video_id": track['videoId'],
                                         'key': store.tracks.identity_key_for_video(track['videoId'])}
                                        for track in tracks if track.get('videoId')]})
                store.settings.set_setting("listening_album_pending", json.dumps({"opportunity_id": opportunity,
                    "browse": browse, "playlist": playlist, "at": ctx.now_fn(), 'action_id': action_id,
                    'videos': [track['videoId'] for track in tracks if track.get('videoId')]}))
            ctx.bridge.send_control({"type": "navigate", "url":
                "https://music.youtube.com/watch?v=" + quote(video, safe="") + "&list=" + quote(playlist, safe="")})
        try:
            await activity_for(ctx).to_thread(start)
        except ValueError as exc:
            return _toast(request, str(exc))
        except Exception:
            ctx.logger.exception("Album playback failed: %s", browse)
            return _toast(request, "Couldn’t start the album. Check the extension connection and try again.")
        return Response(status_code=204)

    @router.get("/album/{browse}/share.txt")
    def album_share(browse: str):
        """Download a verified complete, ordered album, including offline cached snapshots."""
        title, vids = None, []
        cached = store.collection.cached_album_share(browse)
        if cached:
            title, vids = cached["title"], cached["video_ids"]
        if not vids:
            problem = "The full album could not be fetched. Reconnect to YouTube Music and try sharing again."
            try:
                clients = ctx.client_provider()
                client = main_client(store,clients) if is_upload_album(browse) else catalog_client(clients)
                a = fetch_album(client, browse) if client else {}
                tracks = shareable_album_tracks(a)
                title = a.get("title")
                vids = [track["videoId"] for track in tracks]
                remember_album(store, browse, a)
            except ValueError as exc:
                problem = str(exc)
            except Exception:
                ctx.logger.info("album share fetch failed for %r (non-fatal)", browse)
            if not vids:
                raise HTTPException(status_code=503, detail=problem)
        lines = [f"https://music.youtube.com/watch?v={v}" for v in vids]
        safe = re.sub(r'[\\/:*?"<>|]', "_", title or "album").strip() or "album"
        ascii_name = safe.encode("ascii", "ignore").decode() or "album"
        return PlainTextResponse("\n".join(lines) + "\n", headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}.txt"; '
                                   f"filename*=UTF-8''{quote(safe)}.txt"})

    def _album_row(request, browse, vid):
        for i, t in enumerate(store.charts.album_tracks_detail(browse), start=1):
            if t["video_id"] == vid:
                return templates.TemplateResponse(request, "_partials/album_track_row.html", {"t": t, "idx": i})
        return Response(status_code=204)

    @router.post("/album/{browse}/track-genre")
    async def album_set_track_genre(browse: str, request: Request):
        form = await request.form()
        vid, genre = (form.get("video_id") or "").strip(), (form.get("genre") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        tid = store.tracks.track_ids_for_videos([vid]).get(vid)
        if tid is None:
            return _toast(request, "track not found")
        store.tracks.set_track_genre(tid, genre)
        ctx.rec_worker.trigger_content_update(video_ids=[vid])
        return _album_row(request, browse, vid)

    @router.post("/album/{browse}/track-year")
    async def album_set_track_year(browse: str, request: Request):
        form = await request.form()
        vid, year = (form.get("video_id") or "").strip(), (form.get("year") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        tid = store.tracks.track_ids_for_videos([vid]).get(vid)
        if tid is None:
            return _toast(request, "track not found")
        store.tracks.set_track_year(tid, year)
        ctx.rec_worker.trigger_content_update(video_ids=[vid])
        return _album_row(request, browse, vid)

    @router.post("/album/create-playlist")
    async def album_create_playlist(request: Request):
        form = await request.form()
        browse_id = (form.get("browse_id") or "").strip()
        name = (form.get("name") or "").strip()
        if not browse_id:
            return _toast(request, "No album to create from.")
        try:
            res = await activity_for(ctx).to_thread(ctx.ops().create_playlist_from_album, browse_id, name)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            ctx.logger.exception("create playlist from album %r failed", browse_id)
            return _toast(request, "Couldn't create the playlist. See the log.")
        return Response(status_code=200, headers={"HX-Redirect": f"/playlist/{res['db_pid']}"})

    @router.post("/album/{browse}/enrich")
    def album_enrich(browse: str):
        """Run the enrichment waterfall over a saved album's folded-in tracks, same harness and SSE
        stream as playlist enrichment, scoped by album_browse instead of a playlist."""
        job = ctx.jobs.create()
        job.album_browse = browse
        job.source = "waterfall"
        pending = store.tracks.album_tracks_for_waterfall(browse)

        def run():
            try:
                waterfall.run_waterfall(store, pending, enrichment.load_config(store),
                                        on_progress=job.events.append)
            except Exception as e:  # noqa: BLE001
                detail = str(e) or type(e).__name__
                job.error = detail
                job.events.append({"type": "err", "text": f"enrichment failed: {detail}"})
            finally:
                job.done = True

        activity_for(ctx).start_thread(run)
        return JSONResponse({"job_id": job.id})

    @router.get("/album/{browse}/conflicts")
    def album_conflicts(request: Request, browse: str):
        return templates.TemplateResponse(request, "_partials/conflicts_modal.html", {
            "conflicts": store.enrichment_reports.unresolved_conflicts_for_album(browse),
            "resolve_url": f"/album/{browse}/conflicts/resolve",
            "count_left": store.enrichment_reports.conflict_count_for_album(browse)})

    @router.post("/album/{browse}/conflicts/resolve")
    async def album_resolve_conflicts(request: Request, browse: str):
        form = await request.form()
        for key, value in form.multi_items():
            if ":" not in key or not value:
                continue
            tid, _, field = key.partition(":")
            if tid.isdigit():
                try:
                    store.enrichment.resolve_conflict(int(tid), field, value)
                except ValueError:
                    pass
        return templates.TemplateResponse(request, "_partials/conflicts_modal.html", {
            "conflicts": store.enrichment_reports.unresolved_conflicts_for_album(browse),
            "resolve_url": f"/album/{browse}/conflicts/resolve",
            "count_left": store.enrichment_reports.conflict_count_for_album(browse)})

    return router
