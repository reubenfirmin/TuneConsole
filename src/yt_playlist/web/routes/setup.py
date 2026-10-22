"""Setup wizard: identities, provider configuration, and local import connections."""
from yt_playlist.core.activity import activity_for
from yt_playlist.core.accounts import client_for
from urllib.parse import quote
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from yt_playlist.core.setup import BROWSER_CREDENTIAL_FILENAME
from yt_playlist.library.takeout import TakeoutFormatError
from yt_playlist.library.spotify_data import SpotifyDataFormatError
from yt_playlist.library.spotify_library import (SpotifyLibraryFormatError,
                                                  SPOTIFY_REPORT_SCHEMA,
                                                  find_album_candidates,
                                                  import_spotify_library,
                                                  load_spotify_library,
                                                  prepare_spotify_library,
                                                  save_selected_album)
from yt_playlist.providers import enrichment, lastfm, spotify
from yt_playlist.web.identity_checks import check_identities
from yt_playlist.web.history_imports import HistoryImports
from yt_playlist.web.spotify_import import (ACTIVE_STATUSES, JOB_KEY, TransferPreviews, current_job,
                                            estimate, save_job)


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, setup = ctx.store, ctx.templates, ctx.setup
    previews = TransferPreviews()
    history_imports = HistoryImports(ctx)

    def _saved_identities():
        return [{"id": i.id, "label": i.label, "brand": i.brand_account_id or "",
                 "is_master": i.is_master} for i in store.identities.get_identities()]

    def _setup_context(request, *, rows, master_idx, error=None, status_code=200):
        return templates.TemplateResponse(request, "setup.html", {
            "rows": rows, "master_idx": master_idx, "error": error,
            "saved_identities": _saved_identities(),
            "configured": (setup.configured if setup else True),
            "flash": request.query_params.get("flash"),
            "enrichment": enrichment.load_config(store),
            "lastfm_configured": lastfm.api_key(store) is not None,
            "spotify_library_job": current_job(ctx),
            "spotify_history": store.history.spotify_history_stats(),
            "spotify_legacy_unattributed": store.settings.get_setting('spotify_legacy_discovery_unattributed') == '1',
        }, status_code=status_code)

    @router.get("/setup")
    def setup_page(request: Request):
        idents = store.identities.get_identities()
        if idents:
            rows = [{"id": i.id, "label": i.label, "brand": i.brand_account_id or ""} for i in idents]
            master_idx = next((n for n, i in enumerate(idents) if i.is_master), 0)
        else:
            # Pre-fill a sensible default for a single-account user. They can rename it or add more.
            rows, master_idx = [{"id": None, "label": "main", "brand": ""}], 0
        return _setup_context(request, rows=rows, master_idx=master_idx)

    @router.get("/setup/identities/check")
    def setup_identity_checks(identity_id: int | None = None):
        # A sync route runs in a worker thread: bridge calls must leave the WebSocket loop free.
        return {"checks": check_identities(ctx, identity_id), "auth_labels": list(ctx.auth_expired.values())}

    @router.post("/setup/identities/{identity_id}/remove")
    def setup_remove_identity(identity_id: int):
        if setup is None:
            raise HTTPException(status_code=404, detail="setup not available")
        identities = store.identities.get_identities()
        removed = next((i for i in identities if i.id == identity_id), None)
        if removed is None:
            raise HTTPException(status_code=404, detail="Identity no longer exists.")
        remaining = [i for i in identities if i.id != identity_id]
        if not remaining:
            raise HTTPException(status_code=409, detail="Keep at least one identity.")
        main = next((i for i in remaining if i.is_master), remaining[0])
        try:
            setup.apply_setup([{"id": i.id, "label": i.label, "credential_ref": i.credential_ref,
                                "brand_account_id": i.brand_account_id, "is_master": i.id == main.id}
                               for i in remaining])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.clear_auth_expired(identity_id)
        return {"message": f'Removed identity "{removed.label}".', "main_identity_id": main.id,
                "identity_count": len(remaining), "auth_labels": list(ctx.auth_expired.values())}

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
        else:
            if ctx.enrich_worker:
                ctx.enrich_worker.trigger()
        return _enrichment_panel(request)

    @router.post("/setup")
    async def setup_submit(request: Request):
        if setup is None:
            raise HTTPException(status_code=404, detail="setup not available")
        wants_json = "application/json" in request.headers.get("accept", "")
        form = await request.form()
        labels, brands = form.getlist("label"), form.getlist("brand_account_id")
        identity_ids = form.getlist("identity_id")
        master = form.get("master")
        identities = []
        for idx, label in enumerate(labels):
            brand = brands[idx] if idx < len(brands) else ""
            if not (label or "").strip() and not (brand or "").strip():
                continue
            identities.append({
                "id": identity_ids[idx] if idx < len(identity_ids) else "",
                "label": label.strip(),
                "brand_account_id": (brand or "").strip() or None,
                "is_master": str(idx) == master,
                "credential_ref": BROWSER_CREDENTIAL_FILENAME})
        try:
            for row in identities:
                row["id"] = int(row["id"]) if row["id"] else None
            # The credential is the live extension pairing now, so no capture is passed here.
            setup.apply_setup(identities)
        except ValueError as e:
            if wants_json:
                return JSONResponse({"error": str(e)}, status_code=400)
            active_ids = {str(i.id): i.id for i in store.identities.get_identities()}
            rows = [{"id": active_ids.get(identity_ids[idx]) if idx < len(identity_ids) else None,
                     "label": label, "brand": brands[idx] if idx < len(brands) else ""}
                    for idx, label in enumerate(labels)] or [{"id": None, "label": "", "brand": ""}]
            master_idx = int(master) if (master or "").isdigit() else 0
            return _setup_context(request, rows=rows, master_idx=master_idx,
                                  error=str(e), status_code=400)
        # Saving definitions does not authenticate them. The Identities tab checks live access and
        # clears expired-session flags only for identities whose checks actually succeed.
        active = {i.id for i in store.identities.get_identities()}
        for identity_id in list(ctx.auth_expired):
            if identity_id not in active:
                ctx.clear_auth_expired(identity_id)
        if wants_json:
            return {"identities": _saved_identities(), "auth_labels": list(ctx.auth_expired.values())}
        n = len(identities)
        msg = f"Saved {n} identit{'y' if n == 1 else 'ies'}."
        return RedirectResponse(f"/setup?tab=identities&flash={quote(msg)}", status_code=303)

    @router.post("/import/spotify")
    @router.post("/import/spotify/v2")
    async def import_spotify_route(request: Request):
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            return HTMLResponse('<p class="section-note">No file selected.</p>', status_code=400)
        try:
            result = await history_imports.run('spotify', await up.read())
            report, seeded = result['report'], result['seeded']
        except SpotifyDataFormatError:
            return HTMLResponse(
                '<p class="section-note">Could not find Spotify Extended Streaming History. '
                'Upload the downloaded zip as-is, or one Streaming_History_Audio JSON file.</p>')
        except Exception:
            return HTMLResponse('<p class="section-note" role="alert">History import failed. Try again.</p>')
        if "error" in report:
            return HTMLResponse(f'<p class="section-note">{escape(report["error"])}</p>')
        return templates.TemplateResponse(
            request, "_partials/spotify_result.html", {"report": report, "seeded": seeded,
                "spotify_history": store.history.spotify_history_stats(),
                "spotify_legacy_unattributed": store.settings.get_setting('spotify_legacy_discovery_unattributed') == '1'},
            headers={"HX-Retarget": "#spotify-import-block", "HX-Reswap": "innerHTML"})

    @router.post('/import/spotify/remove')
    async def remove_spotify_history(request: Request):
        try:
            report = await history_imports.remove_spotify()
        except Exception:
            return HTMLResponse('<p class="section-note" role="alert">History removal failed. Try again.</p>',
                                status_code=500)
        return templates.TemplateResponse(request, '_partials/spotify_removed.html', {
            'removed': report,
            'spotify_legacy_unattributed': store.settings.get_setting('spotify_legacy_discovery_unattributed') == '1'},
            headers={'HX-Retarget': '#spotify-import-block', 'HX-Reswap': 'innerHTML'})

    @router.get('/import/history/status')
    async def history_import_status(request: Request, source: str):
        if source not in ('takeout', 'spotify'):
            raise HTTPException(status_code=400, detail='Unknown history source')
        status = history_imports.status(source)
        if 'application/json' in request.headers.get('accept', ''):
            return JSONResponse(status)
        return HTMLResponse(f'<p class="section-note">{escape(status["message"])}</p>')

    # The versioned URL is intentional: setup.html is read from disk on every request, while
    # Python modules remain loaded for the lifetime of the server. If TuneConsole is updated while
    # open, a new form therefore gets a 404 from an old backend instead of silently producing an
    # obsolete, summary-only report. The unversioned route remains for API compatibility.
    @router.post("/import/spotify-library")
    @router.post("/import/spotify-library/v2")
    @router.post("/import/spotify-library/v3")
    @router.post("/import/spotify-library/v4")
    @router.post("/import/spotify-library/v5")
    @router.post("/import/spotify-library/v6")
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
        identities = store.identities.get_identities()
        if not identities:
            return HTMLResponse(
                '<p class="section-note">Connect the extension and configure an identity first.</p>')
        try:
            parsed = load_spotify_library(await up.read())
        except SpotifyLibraryFormatError:
            return HTMLResponse(
                '<p class="section-note">Could not find Spotify playlists or saved albums. '
                'Choose the <strong>Account Data</strong> zip containing Playlist JSON and '
                'YourLibrary JSON files—not Extended Streaming History or Technical Log Information.</p>')
        return templates.TemplateResponse(
            request, "_partials/spotify_library_select.html", {
                "parsed": parsed, "identities": identities,
                "selection_token": previews.put({"stage": "selection", "parsed": parsed}, ctx.now_fn()),
            }, headers={"HX-Retarget": "#spotify-library-import-block", "HX-Reswap": "innerHTML"})

    def _destination(value):
        try:
            identity_id = int(value)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Choose a destination identity") from None
        identity = next((i for i in store.identities.get_identities() if i.id == identity_id), None)
        try:
            _,client = client_for(store,ctx.client_provider(),identity_id=identity_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Connect the selected YouTube Music identity") from exc
        return identity, client

    def _snapshot(token, stage, *, consume=False):
        value = previews.get(token, ctx.now_fn(), consume=consume)
        if value is None or value["stage"] != stage:
            raise HTTPException(status_code=410, detail="Preview expired or already used. Upload the ZIP again.")
        return value

    @router.post("/import/spotify-library/prepare")
    async def spotify_library_prepare(request: Request):
        form = await request.form()
        snapshot = _snapshot(form.get("selection_token"), "selection")
        identity, client = _destination(form.get("identity_id"))
        parsed = snapshot["parsed"]
        try:
            indices = sorted({int(value) for value in form.getlist("playlist_index")})
            if any(index < 0 or index >= len(parsed["playlists"]) for index in indices):
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid playlist selection") from None
        selected = {"playlists": [parsed["playlists"][index] for index in indices],
                    "albums": parsed["albums"] if form.get("include_albums") == "on" else []}
        if not selected["playlists"] and not selected["albums"]:
            raise HTTPException(status_code=400, detail="Select at least one playlist or saved albums")
        if not ctx.spotify_import_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another Spotify import is running")
        try:
            _snapshot(form.get("selection_token"), "selection", consume=True)
        except HTTPException:
            ctx.spotify_import_lock.release()
            raise
        job = {"status": "running", "phase": "preview", "started_at": ctx.now_fn(),
               "identity_id": identity.id, "identity_label": identity.label,
               "report_schema": SPOTIFY_REPORT_SCHEMA, **estimate(selected)}
        save_job(store, job)

        def prepare():
            try:
                prepared = prepare_spotify_library(selected, client, store=store, identity_id=identity.id)
                token = previews.put({"stage": "ready", "prepared": prepared,
                                      "identity_id": identity.id}, ctx.now_fn())
                save_job(store, {**job, "status": "preview", "preview_token": token})
            except Exception:
                ctx.logger.exception("Spotify import preview failed")
                save_job(store, {**job, "status": "error", "error": "Could not prepare the preview. Upload the ZIP again."})
            finally:
                ctx.spotify_import_lock.release()

        activity_for(ctx).start_thread(prepare, name="spotify-library-preview")
        return templates.TemplateResponse(
            request, "_partials/spotify_library_status.html", {"job": current_job(ctx)},
            headers={"HX-Retarget": "#spotify-library-import-block", "HX-Reswap": "innerHTML"})

    @router.get("/import/spotify-library/preview/{token}")
    def spotify_library_preview(request: Request, token: str):
        snapshot = _snapshot(token, "ready")
        identity, _ = _destination(snapshot["identity_id"])
        return templates.TemplateResponse(request, "spotify_library_preview.html", {
            "prepared": snapshot["prepared"], "identity": identity, "preview_token": token})

    @router.post("/import/spotify-library/confirm")
    async def spotify_library_confirm(request: Request):
        form = await request.form()
        token = form.get("preview_token")
        snapshot = _snapshot(token, "ready")
        identity, client = _destination(snapshot["identity_id"])
        albums = []
        for index, source in enumerate(snapshot["prepared"]["albums"]):
            resolution = source["resolution"]
            action = form.get(f"album_action_{index}", "save")
            if action not in ("save", "playlist", "skip"):
                raise HTTPException(status_code=400, detail="Invalid album action")
            selected = form.get(f"album_choice_{index}")
            match = resolution["match"] if selected is None else next(
                (candidate for candidate in resolution["candidates"] if candidate["browse_id"] == selected), None)
            if selected and match is None:
                raise HTTPException(status_code=400, detail="Choose an album from this preview")
            if action == "playlist" and match is None:
                raise HTTPException(status_code=400, detail="Choose a release to create as a playlist")
            albums.append({**source, "action": action, "resolution": {**resolution, "match": match}})
        prepared = {**snapshot["prepared"], "albums": albums}
        if not ctx.spotify_import_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another Spotify import is running")
        try:
            snapshot = _snapshot(token, "ready", consume=True)
        except HTTPException:
            ctx.spotify_import_lock.release()
            raise
        job = {"status": "running", "phase": "import", "started_at": ctx.now_fn(),
               "identity_id": identity.id, "identity_label": identity.label,
               "report_schema": SPOTIFY_REPORT_SCHEMA, **estimate(prepared)}
        save_job(store, job)

        def run_import():
            try:
                report = import_spotify_library(store, None, client, identity.id, ctx.now_fn(),
                                                 prepared=prepared)
                if report["tracks_imported"] or report["albums_imported"]:
                    store.settings.set_setting("spotify_library_imported_at", str(ctx.now_fn()))
                save_job(store, {**job, "status": "done", "finished_at": ctx.now_fn(), "report": report})
            except Exception:
                ctx.logger.exception("Spotify library import failed")
                save_job(store, {**job, "status": "error", "finished_at": ctx.now_fn(),
                                 "error": "The import stopped. Upload the same ZIP to resume recorded transfers."})
            finally:
                ctx.spotify_import_lock.release()

        activity_for(ctx).start_thread(run_import, name="spotify-library-import")
        return RedirectResponse("/#notices", status_code=303)

    @router.post("/import/spotify-library/cancel")
    async def spotify_library_cancel(request: Request):
        form = await request.form()
        token = form.get("preview_token")
        _snapshot(token, "ready", consume=True)
        job = current_job(ctx)
        if job and job.get("preview_token") == token:
            store.settings.delete_setting(JOB_KEY)
        return RedirectResponse("/setup?tab=import", status_code=303)

    @router.get("/import/spotify-library/status")
    def spotify_library_status(request: Request):
        job = current_job(ctx)
        if not job:
            return Response(status_code=204)
        redirect = (f"/import/spotify-library/preview/{job['preview_token']}" if job["status"] == "preview"
                    else "/#notices" if job["status"] == "done" else None)
        return templates.TemplateResponse(
            request, "_partials/spotify_library_status.html", {"job": job},
            headers={"HX-Redirect": redirect} if redirect else None)

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

    def _spotify_destination():
        job = current_job(ctx)
        if job and job.get("identity_id") is not None:
            identity,remote = _destination(job["identity_id"])
            return identity.id,remote
        try:
            return client_for(store,ctx.client_provider())
        except ValueError as exc:
            raise HTTPException(status_code=409,detail=str(exc)) from exc

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
            item["candidates"] = await activity_for(ctx).to_thread(
                find_album_candidates, _spotify_destination()[1], item)
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
            identity_id,remote = _spotify_destination()
            outcome = await activity_for(ctx).to_thread(
                save_selected_album, store, remote, item,
                (form.get("browse_id") or "").strip(), identity_id=identity_id)
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
            store.settings.set_setting("spotify_library_imported_at", str(ctx.now_fn()))
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
                store.settings.delete_setting(JOB_KEY)
        return Response(status_code=200)

    @router.post("/import/spotify-library/delete-report")
    def delete_spotify_library_report():
        if not ctx.spotify_import_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Wait for the Spotify import to finish")
        try:
            store.settings.delete_setting(JOB_KEY)
        finally:
            ctx.spotify_import_lock.release()
        return RedirectResponse("/setup?tab=import", status_code=303)

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
            result = await history_imports.run('takeout', raw)
            report, n = result['report'], result['seeded']
        except TakeoutFormatError:
            # load_watch_history (called by import_takeout) now parses both the JSON and the
            # default HTML export, so this only fires for genuinely unusable input (a zip with
            # neither history file inside it, or plain garbage). JSON stays the recommended path
            # because it carries a real timestamp on every row.
            return HTMLResponse(
                "<p class=\"section-note\">Could not read this as a Takeout watch history export. "
                "JSON is recommended (it has the most complete timestamps), but the HTML export "
                "works too.</p>")
        except Exception:
            return HTMLResponse('<p class="section-note" role="alert">History import failed. Try again.</p>')
        if "error" in report:
            return HTMLResponse(f"<p class=\"section-note\">{escape(report['error'])}</p>")
        # Success replaces the whole import block (instructions + form) with a labeled result
        # card: HX-Retarget widens the swap target beyond the form's own error slot. Error
        # responses above keep the default #takeout-import-result target so the form stays
        # usable for a retry.
        return templates.TemplateResponse(
            request, "_partials/takeout_result.html", {"report": report, "seeded": n},
            headers={"HX-Retarget": "#takeout-import-block", "HX-Reswap": "innerHTML"})

    return router
