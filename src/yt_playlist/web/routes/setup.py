"""Setup wizard: render the config form and save identities. Credential is the live extension
pairing (see the Pairing tab), so there is no capture to paste or verify here anymore."""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from yt_playlist.core.setup import BROWSER_CREDENTIAL_FILENAME
from yt_playlist.library.takeout import (TakeoutFormatError, import_takeout,
                                         seed_discovery_from_unmatched)
from yt_playlist.providers import enrichment, lastfm


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
