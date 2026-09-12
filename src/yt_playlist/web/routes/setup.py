"""Setup wizard: identities, provider configuration, and local import connections."""
import asyncio
import threading
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from yt_playlist.core.setup import BROWSER_CREDENTIAL_FILENAME
from yt_playlist.library.takeout import (TakeoutFormatError, import_takeout,
                                         seed_discovery_from_unmatched)
from yt_playlist.library.spotify_data import SpotifyDataFormatError, import_spotify_data
from yt_playlist.library.spotify_library import (SpotifyLibraryFormatError,
                                                  SPOTIFY_REPORT_SCHEMA,
                                                  find_album_candidates,
                                                  import_spotify_library,
                                                  load_spotify_library,
                                                  save_selected_album)
from yt_playlist.providers import enrichment, lastfm, spotify
from yt_playlist.web.spotify_import import (ACTIVE_STATUSES, JOB_KEY, current_job,
                                            estimate, save_job)


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, setup = ctx.store, ctx.templates, ctx.setup

    def _setup_context(request, *, rows, master_idx, error=None, status_code=200):
        return templates.TemplateResponse(request, "setup.html", {
            "rows": rows, "master_idx": master_idx, "error": error,
            "configured": (setup.configured if setup else True),
            "flash": request.query_params.get("flash"),
            "enrichment": enrichment.load_config(store),
            "lastfm_configured": lastfm.api_key(store) is not None,
            "spotify_library_job": current_job(ctx),
        }, status_code=status_code)

    @router.get("/setup")
    def setup_page(request: Request):
        idents = store.get_identities()
        if idents:
            rows = [{"label": i.label, "brand": i.brand_account_id or ""} for i in idents]
            master_idx = next((n for n, i in enumerate(idents) if i.is_master), 0)
        else:
            # Pre-fill a sensible default so a single-account user who just paired the extension can
            # click Save without being nagged to invent an identity. They can rename it or add more.
            rows, master_idx = [{"label": "main", "brand": ""}], 0
        return _setup_context(request, rows=rows, master_idx=master_idx)

    def _enrichment_panel(request):
        return templates.TemplateResponse(request, "_partials/enrichment_panel.html", {
            "enrichment": enrichment.load_config(store),
            "lastfm_configured": lastfm.api_key(store) is not None})

    @router.post("/setup/enrichment")
    async def setup_enrichment(request: Request):
        # Persist the provider order + enabled flags. Form: `order` (names in DOM order) and
        # `enabled` (the checked names). The JS onMove guard already prevents invalid orders; on the
        # off chance an invalid one arrives, save_config raises and we just re-render last-good state.
        form = await request.form()
        order = form.getlist("order")
        on = set(form.getlist("enabled"))
        try:
            enrichment.save_config(store, [{"name": n, "enabled": n in on} for n in order])
        except ValueError:
            pass
        return _enrichment_panel(request)

    @router.post("/setup")
    async def setup_submit(request: Request):
        if setup is None:
            raise HTTPException(status_code=404, detail="setup not available")
        form = await request.form()
        labels, brands = form.getlist("label"), form.getlist("brand_account_id")
        master = form.get("master")
        identities = []
        for idx, label in enumerate(labels):
            if not (label or "").strip():
                continue
            brand = brands[idx] if idx < len(brands) else ""
            identities.append({
                "label": label.strip(),
                "brand_account_id": (brand or "").strip() or None,
                "is_master": str(idx) == master,
                "credential_ref": BROWSER_CREDENTIAL_FILENAME})
        try:
            # The credential is the live extension pairing now, so no capture is passed here.
            setup.apply_setup(identities)
        except ValueError as e:
            rows = [{"label": l, "brand": b} for l, b in zip(labels, brands)] or [{"label": "", "brand": ""}]
            master_idx = int(master) if (master or "").isdigit() else 0
            return _setup_context(request, rows=rows, master_idx=master_idx,
                                  error=str(e), status_code=400)
        ctx.clear_all_auth_expired()                # success clears the stale-session banner (persisted)
        # Syncing is automatic in the background now (fires as soon as the extension is connected and
        # then periodically), so there is no button to point at. "Has synced before" tells a re-auth
        # apart from a first-time setup only to word the confirmation.
        has_synced = bool(store.get_setting("last_sync_at"))
        if has_synced:
            # Nothing for them to do: the background sync catches up. A transient toast, not a banner.
            return RedirectResponse(
                f"/?toast={quote('You’re authenticated again. Your library will refresh automatically.')}",
                status_code=303)
        n = len(identities)
        msg = (f"Saved {n} identit{'y' if n == 1 else 'ies'}. Keep a signed-in music.youtube.com tab "
               "open and your library will sync automatically.")
        return RedirectResponse(f"/?flash={quote(msg)}", status_code=303)

    @router.post("/import/spotify")
    async def import_spotify_route(request: Request):
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            return HTMLResponse('<p class="section-note">No file selected.</p>', status_code=400)
        try:
            report = import_spotify_data(store, await up.read())
        except SpotifyDataFormatError:
            return HTMLResponse(
                '<p class="section-note">Could not find Spotify Extended Streaming History. '
                'Upload the downloaded zip as-is, or one Streaming_History_Audio JSON file.</p>')
        if "error" in report:
            return HTMLResponse(f'<p class="section-note">{report["error"]}</p>')
        if report["plays_added"] or report["events_added"]:
            if ctx.rec_worker:
                ctx.rec_worker.trigger()
        if report["matched"]:
            store.set_setting("spotify_imported_at", str(ctx.now_fn()))
        min_plays = max(3, round(report["span_days"] / 365))
        seeded = seed_discovery_from_unmatched(
            store, report["unmatched_artists"], ctx.now_fn(), min_plays=min_plays)
        return templates.TemplateResponse(
            request, "_partials/spotify_result.html", {"report": report, "seeded": seeded},
            headers={"HX-Retarget": "#spotify-import-block", "HX-Reswap": "innerHTML"})

    # The versioned URL is intentional: setup.html is read from disk on every request, while
    # Python modules remain loaded for the lifetime of the server. If TuneConsole is updated while
    # open, a new form therefore gets a 404 from an old backend instead of silently producing an
    # obsolete, summary-only report. The unversioned route remains for API compatibility.
    @router.post("/import/spotify-library")
    @router.post("/import/spotify-library/v2")
    @router.post("/import/spotify-library/v3")
    @router.post("/import/spotify-library/v4")
    async def import_spotify_library_route(request: Request):
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            return HTMLResponse('<p class="section-note">No file selected.</p>', status_code=400)
        existing = current_job(ctx)
        if existing and existing.get("status") in ACTIVE_STATUSES:
            return templates.TemplateResponse(
                request, "_partials/spotify_library_status.html", {"job": existing},
                headers={"HX-Retarget": "#spotify-library-import-block", "HX-Reswap": "innerHTML"})
        identities = store.get_identities()
        identity = next((i for i in identities if i.is_master), identities[0] if identities else None)
        client = (ctx.client_provider() or {}).get(identity.id) if identity else None
        if identity is None or client is None:
            return HTMLResponse(
                '<p class="section-note">Connect the extension and configure an identity first.</p>')
        try:
            raw = await up.read()
            parsed = load_spotify_library(raw)
        except SpotifyLibraryFormatError:
            return HTMLResponse(
                '<p class="section-note">Could not find Spotify playlists or saved albums. '
                'Choose the <strong>Account Data</strong> zip containing Playlist JSON and '
                'YourLibrary JSON files—not Extended Streaming History or Technical Log Information.</p>')
        if not ctx.spotify_import_lock.acquire(blocking=False):
            job = current_job(ctx)
            return templates.TemplateResponse(
                request, "_partials/spotify_library_status.html", {"job": job},
                headers={"HX-Retarget": "#spotify-library-import-block", "HX-Reswap": "innerHTML"})

        job = {"status": "running", "started_at": ctx.now_fn(),
               "report_schema": SPOTIFY_REPORT_SCHEMA, **estimate(parsed)}
        save_job(store, job)

        def run_import():
            try:
                report = import_spotify_library(
                    store, raw, client, identity.id, ctx.now_fn())
                if report["playlists_imported"] or report["albums_imported"]:
                    store.set_setting("spotify_library_imported_at", str(ctx.now_fn()))
                save_job(store, {**job, "status": "done", "finished_at": ctx.now_fn(),
                                 "report": report})
            except Exception:  # noqa: BLE001 - partial progress is recorded for a safe retry
                ctx.logger.exception("Spotify library import failed")
                save_job(store, {**job, "status": "error", "finished_at": ctx.now_fn(),
                                 "error": "The Spotify library import stopped. Anything already "
                                          "created was recorded, so it is safe to retry."})
            finally:
                ctx.spotify_import_lock.release()

        threading.Thread(target=run_import, name="spotify-library-import", daemon=True).start()
        current = current_job(ctx)
        return templates.TemplateResponse(
            request, "_partials/spotify_library_status.html", {"job": current},
            headers={"HX-Retarget": "#spotify-library-import-block", "HX-Reswap": "innerHTML",
                     **({"HX-Redirect": "/#notices"} if current.get("status") == "done" else {})})

    @router.get("/import/spotify-library/status")
    def spotify_library_status(request: Request):
        job = current_job(ctx)
        if not job:
            return Response(status_code=204)
        return templates.TemplateResponse(
            request, "_partials/spotify_library_status.html", {"job": job},
            headers={"HX-Redirect": "/#notices"} if job.get("status") == "done" else None)

    @router.get("/import/spotify-library/notice")
    def spotify_library_notice(request: Request):
        job = current_job(ctx)
        if not job:
            return Response(status_code=204)
        return templates.TemplateResponse(
            request, "_partials/spotify_library_notice.html", {"job": job})

    @router.get("/import/spotify-library/report")
    def spotify_library_report(request: Request):
        job = current_job(ctx)
        if not job or job.get("status") != "done" or not isinstance(job.get("report"), dict):
            return RedirectResponse("/setup?tab=import", status_code=303)
        details = job["report"].get("details") or {}
        for index, item in enumerate(details.get("albums") or []):
            item["report_index"] = index
            if not item.get("spotify_url"):
                item["spotify_url"] = "https://open.spotify.com/search/" + quote(
                    f"{item.get('title', '')} {item.get('artist', '')}", safe="")
                item["spotify_link_is_search"] = True
        for item in details.get("unmatched_tracks") or []:
            if not item.get("spotify_url"):
                item["spotify_url"] = "https://open.spotify.com/search/" + quote(
                    f"{item.get('title', '')} {item.get('artist', '')}", safe="")
                item["spotify_link_is_search"] = True
        return templates.TemplateResponse(request, "spotify_library_report.html", {
            "job": job, "report": job["report"],
            "spotify_error": request.query_params.get("spotify_error"),
        })

    def _report_album(job, index):
        try:
            position = int(index)
            if position < 0:
                raise IndexError
            albums = job["report"]["details"]["albums"]
            item = albums[position]
        except (KeyError, IndexError, TypeError, ValueError):
            raise HTTPException(status_code=404, detail="album outcome not found") from None
        if item.get("status") != "unmatched":
            raise HTTPException(status_code=409, detail="album outcome is already resolved")
        return item

    def _spotify_client():
        identities = store.get_identities()
        identity = next((i for i in identities if i.is_master), identities[0] if identities else None)
        client = (ctx.client_provider() or {}).get(identity.id) if identity else None
        if client is None:
            raise HTTPException(status_code=409, detail="YouTube Music is not connected")
        return client

    @router.post("/import/spotify-library/find-album-candidates")
    async def spotify_library_find_candidates(request: Request):
        job = current_job(ctx)
        if not job or job.get("status") != "done":
            raise HTTPException(status_code=404, detail="report not found")
        form = await request.form()
        item = _report_album(job, form.get("album_index"))
        try:
            # Bridge-backed YTMusic calls wait for replies delivered by this app's
            # WebSocket handler. Running them on the event loop deadlocks that reply.
            item["candidates"] = await asyncio.to_thread(
                find_album_candidates, _spotify_client(), item)
            save_job(store, job)
        except Exception:  # noqa: BLE001 - a failed lookup leaves the report intact
            ctx.logger.exception("Spotify import candidate lookup failed")
            message = quote("YouTube Music could not search for candidates. Try again when connected.")
            return RedirectResponse(f"/import/spotify-library/report?spotify_error={message}", status_code=303)
        return RedirectResponse(
            f"/import/spotify-library/report#album-{form.get('album_index')}", status_code=303)

    @router.post("/import/spotify-library/resolve-album")
    async def spotify_library_resolve_album(request: Request):
        job = current_job(ctx)
        if not job or job.get("status") != "done":
            raise HTTPException(status_code=404, detail="report not found")
        form = await request.form()
        item = _report_album(job, form.get("album_index"))
        try:
            outcome = await asyncio.to_thread(
                save_selected_album, store, _spotify_client(), item,
                (form.get("browse_id") or "").strip())
        except (ValueError, HTTPException) as exc:
            message = quote(str(exc.detail if isinstance(exc, HTTPException) else exc))
            return RedirectResponse(f"/import/spotify-library/report?spotify_error={message}", status_code=303)
        except Exception:  # noqa: BLE001 - preserve the report and surface a usable failure
            ctx.logger.exception("Spotify import selected-album save failed")
            message = quote("YouTube Music could not save that album. Try again when connected.")
            return RedirectResponse(f"/import/spotify-library/report?spotify_error={message}", status_code=303)
        report = job["report"]
        item.update(outcome)
        item.pop("candidates", None)
        report["albums_unmatched"] = max(0, report["albums_unmatched"] - 1)
        report["albums_imported" if outcome["status"] == "imported" else "albums_skipped"] += 1
        save_job(store, {**job, "report": report})
        if outcome["status"] == "imported":
            store.set_setting("spotify_library_imported_at", str(ctx.now_fn()))
        return RedirectResponse("/import/spotify-library/report", status_code=303)

    @router.get("/import/spotify-library/spotify-thumbnail")
    def spotify_library_thumbnail(url: str):
        thumbnail = spotify.thumbnail(url)
        if not thumbnail:
            return Response(status_code=404)
        return RedirectResponse(thumbnail, status_code=307,
                                headers={"Cache-Control": "private, max-age=86400"})

    @router.post("/import/spotify-library/dismiss")
    def dismiss_spotify_library_notice():
        job = current_job(ctx)
        if not job or job.get("status") not in ACTIVE_STATUSES:
            if job:
                save_job(store, {**job, "dismissed": True})
            else:
                store.delete_setting(JOB_KEY)
        return Response(status_code=200)

    @router.post("/import/takeout")
    async def import_takeout_route(request: Request):
        # Google Takeout watch-history upload (#61). Everything is processed locally: the file
        # is parsed in this request and never leaves the machine. Unmatched artists with enough
        # plays seed the discovery pool automatically (the min-plays gate filters one-off noise).
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "read"):    # absent field, or a stray text value
            return HTMLResponse("<p class=\"section-note\">No file selected.</p>", status_code=400)
        raw = await up.read()
        try:
            report = import_takeout(store, raw)
        except TakeoutFormatError:
            # load_watch_history (called by import_takeout) now parses both the JSON and the
            # default HTML export, so this only fires for genuinely unusable input (a zip with
            # neither history file inside it, or plain garbage). JSON stays the recommended path
            # because it carries a real timestamp on every row.
            return HTMLResponse(
                "<p class=\"section-note\">Could not read this as a Takeout watch history export. "
                "JSON is recommended (it has the most complete timestamps), but the HTML export "
                "works too.</p>")
        if "error" in report:
            return HTMLResponse(f"<p class=\"section-note\">{report['error']}</p>")
        if report["plays_added"] or report["events_added"]:
            if ctx.rec_worker:
                ctx.rec_worker.trigger()
        if report["matched"] > 0:
            store.set_setting("takeout_imported_at", str(ctx.now_fn()))
            # Takeout is account-wide, so it can now prove which history rows never happened. Run the
            # phantom purge against the window this import just established (core/repair.py).
            from yt_playlist.core import repair
            repair.run_once(store)
        else:
            # A zero-match import (usually: library not synced yet) should not re-nag on the very
            # next Home render: snooze the nag 90 days; it returns as the re-import reminder.
            store.set_setting("takeout_nag_dismissed_at", str(ctx.now_fn()))
        # Seeding bar scales with export span: 3 plays in a decade is noise, 3 in a season is taste.
        min_plays = max(3, round(report["span_days"] / 365))
        n = seed_discovery_from_unmatched(store, report["unmatched_artists"], ctx.now_fn(),
                                          min_plays=min_plays)
        # Success replaces the whole import block (instructions + form) with a labeled result
        # card: HX-Retarget widens the swap target beyond the form's own error slot. Error
        # responses above keep the default #takeout-import-result target so the form stays
        # usable for a retry.
        return templates.TemplateResponse(
            request, "_partials/takeout_result.html", {"report": report, "seeded": n},
            headers={"HX-Retarget": "#takeout-import-block", "HX-Reswap": "innerHTML"})

    return router
