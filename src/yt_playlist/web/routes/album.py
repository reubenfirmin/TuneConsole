"""Album landing page — cover, track table, save/open, "create a playlist from this album", and
(for saved albums folded into the library) genre/year enrichment with the same live flow as playlists."""
import asyncio
import re
import threading
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from yt_playlist import discogs, lastfm, musicbrainz
from yt_playlist.rec_dao import RecDao
from yt_playlist.thumbnails import best_thumb


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates
    ENRICH_SOURCES = {"musicbrainz": musicbrainz.enrich_playlist, "lastfm": lastfm.enrich_playlist,
                      "discogs": discogs.enrich_playlist}

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    @router.get("/album")
    def album_page(request: Request):
        browse_id = (request.query_params.get("browse") or "").strip()
        album = None
        try:
            client = next(iter((ctx.client_provider() or {}).values()), None)
            if client and browse_id:
                a = client.get_album(browse_id)
                album = {
                    "title": a.get("title"),
                    "artist": ", ".join(x.get("name", "") for x in (a.get("artists") or [])),
                    "year": a.get("year"),
                    "thumbnail": best_thumb(a.get("thumbnails")),
                    "tracks": [{"title": t.get("title"), "video_id": t.get("videoId"),
                                "artist": ", ".join(x.get("name", "") for x in (t.get("artists") or [])),
                                "duration": t.get("duration")} for t in (a.get("tracks") or [])],
                }
        except Exception:  # noqa: BLE001 - no client / network / parse all degrade gracefully
            ctx.logger.info("album fetch failed for %r (non-fatal)", browse_id)
        saved = browse_id in RecDao(store).saved_album_ids()
        # The editable "folded-in library" table is only for SAVED albums (whose full track list has
        # been materialized for enrichment). For an unsaved album, regular sync may still have stamped
        # one incidental track with this album_browse_id (because it's in one of your playlists) — that
        # partial subset must NOT shadow the full live-fetched album, so only read it when saved.
        tracks = store.album_tracks_detail(browse_id) if (browse_id and saved) else []
        # Fold a saved album's tracks into the library ON DEMAND (using the tracks we just fetched
        # live), so enrichment is available the moment you open it — no waiting for a full sync.
        if saved and not tracks and album and album.get("tracks"):
            for t in album["tracks"]:
                if t.get("video_id") and t.get("title"):
                    store.upsert_track(t["video_id"], t["title"], t.get("artist") or album.get("artist") or "",
                                       album.get("title") or "", None, album_browse_id=browse_id,
                                       thumbnail=album.get("thumbnail"))
            tracks = store.album_tracks_detail(browse_id)
        if tracks and not (album and album.get("title")):   # live fetch missing/empty but it's in our library
            meta = next((a for a in store.get_saved_albums() if a["browse"] == browse_id), {})
            album = {"title": meta.get("title") or tracks[0]["album"] or "Album",
                     "artist": meta.get("artist") or tracks[0]["artist"], "year": meta.get("year"),
                     "thumbnail": meta.get("thumbnail") or tracks[0]["thumbnail"], "tracks": tracks}
        return templates.TemplateResponse(request, "album.html", {
            "album": album, "browse_id": browse_id, "tracks": tracks, "saved": saved,
            "gaps": sum(1 for t in tracks if not t["genre"]),
            "genres": sorted(set(store.get_genre_whitelist()) | set(store.all_genres()), key=str.lower),
            "lastfm_configured": lastfm.api_key(store) is not None,
            # arrived via a home "Enrich" CTA — tint the enrich icons CTA-green so it's clear what to click
            "enrich_hint": request.query_params.get("enrich") == "1"})

    @router.get("/album/{browse}/share.txt")
    def album_share(browse: str):
        """Download the album as a plain .txt — one song URL per line — for easy sharing. Uses the
        folded-in library tracks when saved, else the live album fetch (so unsaved albums share too)."""
        title, vids = None, []
        # Only trust the library track list for SAVED albums (whose full track list is materialized).
        # For an unsaved album, album_tracks_detail may hold a single incidental track stamped with
        # this browse_id by playlist sync — sharing that would drop the rest of the album, so fetch live.
        if browse in RecDao(store).saved_album_ids():
            tracks = store.album_tracks_detail(browse)
            vids = [t["video_id"] for t in tracks if t.get("video_id")]
            meta = next((a for a in store.get_saved_albums() if a["browse"] == browse), {})
            title = meta.get("title") or (tracks[0]["album"] if tracks else None)
        if not vids:
            try:
                client = next(iter((ctx.client_provider() or {}).values()), None)
                a = client.get_album(browse) if client else {}
                title = title or a.get("title")
                vids = [t.get("videoId") for t in (a.get("tracks") or []) if t.get("videoId")]
            except Exception:  # noqa: BLE001 - no client / network all degrade to a 404
                ctx.logger.info("album share fetch failed for %r (non-fatal)", browse)
        if not vids:
            raise HTTPException(status_code=404, detail="album not found")
        lines = [f"https://music.youtube.com/watch?v={v}" for v in vids]
        safe = re.sub(r'[\\/:*?"<>|]', "_", title or "album").strip() or "album"
        ascii_name = safe.encode("ascii", "ignore").decode() or "album"
        return PlainTextResponse("\n".join(lines) + "\n", headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}.txt"; '
                                   f"filename*=UTF-8''{quote(safe)}.txt"})

    def _album_row(request, browse, vid):
        for i, t in enumerate(store.album_tracks_detail(browse), start=1):
            if t["video_id"] == vid:
                return templates.TemplateResponse(request, "_partials/album_track_row.html", {"t": t, "idx": i})
        return Response(status_code=204)

    @router.post("/album/{browse}/track-genre")
    async def album_set_track_genre(browse: str, request: Request):
        form = await request.form()
        vid, genre = (form.get("video_id") or "").strip(), (form.get("genre") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        tid = store.track_ids_for_videos([vid]).get(vid)
        if tid is None:
            return _toast(request, "track not found")
        store.set_track_genre(tid, genre)
        return _album_row(request, browse, vid)

    @router.post("/album/{browse}/track-year")
    async def album_set_track_year(browse: str, request: Request):
        form = await request.form()
        vid, year = (form.get("video_id") or "").strip(), (form.get("year") or "").strip()
        if not vid:
            return _toast(request, "no track given")
        tid = store.track_ids_for_videos([vid]).get(vid)
        if tid is None:
            return _toast(request, "track not found")
        store.set_track_year(tid, year)
        return _album_row(request, browse, vid)

    @router.post("/album/create-playlist")
    async def album_create_playlist(request: Request):
        form = await request.form()
        browse_id = (form.get("browse_id") or "").strip()
        name = (form.get("name") or "").strip()
        if not browse_id:
            return _toast(request, "No album to create from.")
        try:
            res = await asyncio.to_thread(ctx.ops().create_playlist_from_album, browse_id, name)
        except ValueError as e:
            return _toast(request, str(e))
        except Exception:  # noqa: BLE001
            ctx.logger.exception("create playlist from album %r failed", browse_id)
            return _toast(request, "Couldn't create the playlist — see the log.")
        return Response(status_code=200, headers={"HX-Redirect": f"/playlist/{res['db_pid']}"})

    @router.post("/album/{browse}/enrich/{source}")
    def album_enrich(browse: str, source: str):
        """Kick off a background enrichment job over a saved album's folded-in tracks — the same
        runners and SSE stream as playlist enrichment, scoped by album_browse instead of a playlist."""
        runner = ENRICH_SOURCES.get(source)
        if runner is None:
            raise HTTPException(status_code=404, detail="unknown enrichment source")
        job = ctx.jobs.create()
        job.album_browse = browse
        job.source = source
        pending = store.album_tracks_to_enrich(browse)

        def run():
            try:
                runner(store, None, on_progress=job.events.append, pending=pending)
            except Exception as e:  # noqa: BLE001
                detail = str(e) or type(e).__name__
                job.error = detail
                job.events.append({"type": "err", "text": f"enrichment failed: {detail}"})
            finally:
                job.done = True

        threading.Thread(target=run, daemon=True).start()
        return JSONResponse({"job_id": job.id})

    return router
