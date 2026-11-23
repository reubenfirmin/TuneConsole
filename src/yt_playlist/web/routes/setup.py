"""Setup wizard: identities, provider configuration, and local import connections."""
import logging

from yt_playlist.core.activity import activity_for
from yt_playlist.controllers.account_configuration import AccountConfiguration, IdentityMissing, LastIdentity
from yt_playlist.controllers.enrichment_configuration import EnrichmentConfiguration
from yt_playlist.controllers.description_configuration import DescriptionConfiguration
from yt_playlist.controllers.identity_checks import IdentityChecks
from urllib.parse import quote
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from yt_playlist.library.takeout import TakeoutFormatError
from yt_playlist.library.spotify_data import SpotifyDataFormatError
from yt_playlist.library.spotify_library import SpotifyLibraryFormatError
from yt_playlist.library.spotify_transfer_composition import commands as transfer_commands
from yt_playlist.controllers.spotify_transfer import TransferRejected
from yt_playlist.providers import enrichment, lastfm, spotify
from yt_playlist.rec import album_descriptions
from yt_playlist.web.identity_checks import build as identity_check_routes
from yt_playlist.web.history_imports import HistoryImports
from yt_playlist.library.spotify_transfer_policy import ACTIVE_STATUSES


def build(ctx, *, identity_checks=None) -> APIRouter:
    router = APIRouter()
    store, templates, setup = ctx.store, ctx.templates, ctx.setup
    transfer = transfer_commands(ctx)
    history_imports = HistoryImports(ctx)
    accounts = AccountConfiguration(store.accounts, setup, ctx.clear_auth_expired, lambda: ctx.auth_expired)
    identity_checks = identity_checks if identity_checks is not None else IdentityChecks(
        store.accounts, lambda: ctx.clients(), ctx.clear_auth_expired, logging.getLogger(__name__))
    router.include_router(identity_check_routes(identity_checks, lambda: list(ctx.auth_expired.values())))
    enrichment_config = EnrichmentConfiguration(store.settings, store.genres,
        wake=lambda: ctx.enrich_worker.trigger() if ctx.enrich_worker else None)
    description_config = DescriptionConfiguration(store.settings, store.album_notes,
        lambda now: album_descriptions.settings(store, now), now=ctx.now_fn,
        refresh=lambda: ctx.rec_worker.trigger_home_refresh() if ctx.rec_worker else None)

    def command(target, *args, **kwargs):
        try:
            return target(*args, **kwargs)
        except TransferRejected as exc:
            status = {'busy': 409, 'expired': 410, 'invalid': 400, 'missing': 404,
                      'resolved': 409, 'unavailable': 409}[exc.reason]
            raise HTTPException(status_code=status, detail=exc.detail) from exc

    def _saved_identities():
        return [{"id": i.id, "label": i.label, "brand": i.brand_account_id or "",
                 "is_master": i.is_master} for i in store.accounts.get_accounts()]

    def _setup_context(request, *, rows, master_idx, error=None, status_code=200):
        from yt_playlist.rec import home_preferences
        home_format = store.settings.get_setting('home_format')
        if home_format not in {*home_preferences.PANELS, 'both'}:
            home_format = home_preferences.default_format(store)
        return templates.TemplateResponse(request, "setup.html", {
            "rows": rows, "master_idx": master_idx, "error": error,
            "saved_identities": _saved_identities(),
            "configured": (setup.configured if setup else True),
            "home_format": home_format,
            "flash": request.query_params.get("flash"),
            "enrichment": enrichment_config.query(),
            "lastfm_configured": lastfm.api_key(store) is not None,
            "deepseek": album_descriptions.settings(store, ctx.now_fn()),
            "spotify_library_job": transfer.reports.current(),
            "spotify_history": store.history.spotify_history_stats(),
            "spotify_legacy_unattributed": store.settings.get_setting('spotify_legacy_discovery_unattributed') == '1',
        }, status_code=status_code)

    @router.get("/setup")
    def setup_page(request: Request):
        idents = store.accounts.get_accounts()
        if idents:
            rows = [{"id": i.id, "label": i.label, "brand": i.brand_account_id or ""} for i in idents]
            master_idx = next((n for n, i in enumerate(idents) if i.is_master), 0)
        else:
            # Pre-fill a sensible default for a single-account user. They can rename it or add more.
            rows, master_idx = [{"id": None, "label": "main", "brand": ""}], 0
        return _setup_context(request, rows=rows, master_idx=master_idx)

    @router.post("/setup/identities/{account_id}/remove")
    def setup_remove_identity(account_id: int):
        if setup is None:
            raise HTTPException(status_code=404, detail="setup not available")
        try:
            result = accounts.remove(account_id)
        except IdentityMissing as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LastIdentity as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        label = result.pop('label')
        return {**result, 'message': f'Removed identity "{label}".'}

    def _enrichment_panel(request):
        return templates.TemplateResponse(request, "_partials/enrichment_panel.html", {
            "enrichment": enrichment_config.query(),
            "lastfm_configured": lastfm.api_key(store) is not None,
            'deepseek': album_descriptions.settings(store, ctx.now_fn())})

    @router.post("/setup/enrichment")
    async def setup_enrichment(request: Request):
        # Persist the provider order + enabled flags. Form: `order` (names in DOM order) and
        # `enabled` (the checked names). The JS onMove guard already prevents invalid orders; on the
        # off chance an invalid one arrives, save_config raises and we just re-render last-good state.
        form = await request.form()
        order = form.getlist("order")
        on = set(form.getlist("enabled"))
        try:
            enrichment_config.save([{"name": n, "enabled": n in on} for n in order])
        except ValueError:
            pass
        return _enrichment_panel(request)

    @router.post('/settings/deepseek-key')
    async def deepseek_key(request: Request):
        key = str((await request.form()).get('key') or '').strip()
        try:
            description_config.save(key=key, key_only=True)
        except ValueError:
            return Response(status_code=400)
        return Response(status_code=204)

    @router.post('/settings/deepseek')
    async def deepseek_settings(request: Request):
        form = await request.form()
        error, message = None, None
        try:
            disconnect = form.get('action') == 'disconnect'
            configuration = description_config.save(key=str(form.get('key') or ''),
                daily_limit=form.get('daily_limit', ''), enabled=form.get('enabled') == 'on',
                disconnect=disconnect)
            message = ('Generation paused and the saved key removed. Existing descriptions are kept.'
                       if disconnect else 'Album description settings saved.')
        except ValueError as exc:
            error = str(exc)
            configuration = description_config.query(ctx.now_fn())
        return templates.TemplateResponse(request, '_partials/album_description_settings.html', {
            'deepseek': configuration,
            'description_error': error, 'description_message': message}, status_code=200)

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
                "is_master": str(idx) == master})
        try:
            for row in identities:
                row["id"] = int(row["id"]) if row["id"] else None
            # The credential is the live extension pairing now, so no capture is passed here.
            accounts.save(identities)
        except ValueError as e:
            if wants_json:
                return JSONResponse({"error": str(e)}, status_code=400)
            active_ids = {str(i.id): i.id for i in store.accounts.get_accounts()}
            rows = [{"id": active_ids.get(identity_ids[idx]) if idx < len(identity_ids) else None,
                     "label": label, "brand": brands[idx] if idx < len(brands) else ""}
                    for idx, label in enumerate(labels)] or [{"id": None, "label": "", "brand": ""}]
            master_idx = int(master) if (master or "").isdigit() else 0
            return _setup_context(request, rows=rows, master_idx=master_idx,
                                  error=str(e), status_code=400)
        # Saving definitions does not authenticate them. The Identities tab checks live access and
        # clears expired-session flags only for identities whose checks actually succeed.
        if wants_json:
            return {"identities": _saved_identities(), "auth_labels": list(ctx.auth_expired.values())}
        n = len(identities)
        msg = f"Saved {n} identit{'y' if n == 1 else 'ies'}."
        return RedirectResponse(f"/setup?tab=identities&flash={quote(msg)}", status_code=303)

    @router.post("/import/spotify")
    @router.post("/import/spotify/v2")
    async def import_spotify_route(request: Request):
        async with request.form() as form:
            up = form.get("file")
            if up is None or not hasattr(up, "read"):
                return HTMLResponse('<p class="section-note">No file selected.</p>', status_code=400)
            raw = await up.read()
        try:
            result = await history_imports.run('spotify', raw)
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
        async with request.form() as form:
            up = form.get("file")
            if up is None or not hasattr(up, "read"):
                return HTMLResponse('<p class="section-note">No file selected.</p>', status_code=400)
            existing = transfer.reports.current()
            if existing and existing.get("status") in ACTIVE_STATUSES:
                return templates.TemplateResponse(
                    request, "_partials/spotify_library_status.html", {"job": existing},
                    headers={"HX-Retarget": "#spotify-library-import-block", "HX-Reswap": "innerHTML"})
            identities = store.accounts.get_accounts()
            if not identities:
                return HTMLResponse(
                    '<p class="section-note">Connect the extension and configure an identity first.</p>')
            raw = await up.read()
        try:
            selection = await activity_for(ctx).to_thread(transfer.select, raw)
        except SpotifyLibraryFormatError:
            return HTMLResponse(
                '<p class="section-note">Could not find Spotify playlists or saved albums. '
                'Choose the <strong>Account Data</strong> zip containing Playlist JSON and '
                'YourLibrary JSON files—not Extended Streaming History or Technical Log Information.</p>')
        return templates.TemplateResponse(
            request, "_partials/spotify_library_select.html", {
                **selection,
            }, headers={"HX-Retarget": "#spotify-library-import-block", "HX-Reswap": "innerHTML"})

    @router.post("/import/spotify-library/prepare")
    async def spotify_library_prepare(request: Request):
        form = await request.form()
        job = command(transfer.prepare, form.get('selection_token'), form.get('identity_id'),
                      form.getlist('playlist_index'), form.get('include_albums') == 'on')
        return templates.TemplateResponse(
            request, "_partials/spotify_library_status.html", {"job": job},
            headers={"HX-Retarget": "#spotify-library-import-block", "HX-Reswap": "innerHTML"})

    @router.get("/import/spotify-library/preview/{token}")
    def spotify_library_preview(request: Request, token: str):
        return templates.TemplateResponse(request, "spotify_library_preview.html",
                                          command(transfer.preview, token))

    @router.post("/import/spotify-library/confirm")
    async def spotify_library_confirm(request: Request):
        form = await request.form()
        token = form.get("preview_token")
        snapshot = command(transfer.snapshot, token, 'ready')
        actions = [(form.get(f'album_action_{i}', 'save'), form.get(f'album_choice_{i}'))
                   for i in range(len(snapshot['prepared']['albums']))]
        command(transfer.confirm, token, actions)
        return RedirectResponse("/#notices", status_code=303)

    @router.post("/import/spotify-library/cancel")
    async def spotify_library_cancel(request: Request):
        form = await request.form()
        token = form.get("preview_token")
        command(transfer.cancel, token)
        return RedirectResponse("/setup?tab=import", status_code=303)

    @router.get("/import/spotify-library/status")
    def spotify_library_status(request: Request):
        job = transfer.reports.current()
        if not job:
            return Response(status_code=204)
        redirect = (f"/import/spotify-library/preview/{job['preview_token']}" if job["status"] == "preview"
                    else "/#notices" if job["status"] == "done" else None)
        return templates.TemplateResponse(
            request, "_partials/spotify_library_status.html", {"job": job},
            headers={"HX-Redirect": redirect} if redirect else None)

    @router.get("/import/spotify-library/notice")
    def spotify_library_notice(request: Request):
        job = transfer.reports.current()
        if not job:
            return Response(status_code=204)
        return templates.TemplateResponse(
            request, "_partials/spotify_library_notice.html", {"job": job})

    @router.get("/import/spotify-library/report")
    def spotify_library_report(request: Request):
        job = transfer.reports.current()
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

    @router.post("/import/spotify-library/find-album-candidates")
    async def spotify_library_find_candidates(request: Request):
        form = await request.form()
        album_index = form.get('album_index')

        error = await activity_for(ctx).to_thread(command, transfer.find, album_index)
        target = (f'/import/spotify-library/report?spotify_error={quote(error)}' if error else
                  f'/import/spotify-library/report#album-{album_index}')
        return RedirectResponse(target, status_code=303)

    @router.post("/import/spotify-library/resolve-album")
    async def spotify_library_resolve_album(request: Request):
        form = await request.form()
        album_index, browse_id = form.get('album_index'), (form.get('browse_id') or '').strip()

        error = await activity_for(ctx).to_thread(command, transfer.resolve, album_index, browse_id)
        target = f'/import/spotify-library/report?spotify_error={quote(error)}' if error else '/import/spotify-library/report'
        return RedirectResponse(target, status_code=303)

    @router.get("/import/spotify-library/spotify-thumbnail")
    def spotify_library_thumbnail(url: str):
        with spotify.access(store):
            thumbnail = spotify.thumbnail(url)
        if not thumbnail:
            return Response(status_code=404)
        return RedirectResponse(thumbnail, status_code=307,
                                headers={"Cache-Control": "private, max-age=86400"})

    @router.post("/import/spotify-library/dismiss")
    def dismiss_spotify_library_notice():
        command(transfer.dismiss)
        return Response(status_code=200)

    @router.post("/import/spotify-library/delete-report")
    def delete_spotify_library_report():
        command(transfer.delete)
        return RedirectResponse("/setup?tab=import", status_code=303)

    @router.post("/import/takeout")
    async def import_takeout_route(request: Request):
        # Google Takeout watch-history upload (#61). Everything is processed locally: the file
        # is parsed in this request and never leaves the machine. Unmatched artists with enough
        # plays seed the discovery pool automatically (the min-plays gate filters one-off noise).
        async with request.form() as form:
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
