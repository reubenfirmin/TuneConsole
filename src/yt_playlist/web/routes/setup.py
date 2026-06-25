"""Setup wizard: render the config form, live-check the capture, and save identities."""
import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from yt_playlist.core.setup import BROWSER_CREDENTIAL_FILENAME
from yt_playlist.providers import enrichment, lastfm


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, setup = ctx.store, ctx.templates, ctx.setup

    def _setup_context(request, *, rows, master_idx, error=None, status_code=200):
        return templates.TemplateResponse(request, "setup.html", {
            "rows": rows, "master_idx": master_idx, "error": error,
            "configured": (setup.configured if setup else True),
            "has_credentials": bool(setup and setup.credentials_present),
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
            rows, master_idx = [{"label": "", "brand": ""}], 0
        return _setup_context(request, rows=rows, master_idx=master_idx)

    @router.post("/setup/check")
    async def setup_check(request: Request):
        # Live-verify the capture (network) and report who's signed in, for the green checkmark.
        # Returns the result fragment AND fires an `setup-checked` HX-Trigger so the page's Alpine
        # can auto-fill the master label and (on re-auth) submit immediately.
        if setup is None:
            raise HTTPException(status_code=404, detail="setup not available")
        form = await request.form()
        try:
            account = setup.check_auth(form.get("headers", "") or "")
        except ValueError as e:
            resp = templates.TemplateResponse(request, "_partials/setup_check.html", {"error": str(e)})
            resp.headers["HX-Trigger"] = json.dumps({"setup-checked": {"ok": False}})
            return resp
        resp = templates.TemplateResponse(request, "_partials/setup_check.html", {"account": account})
        resp.headers["HX-Trigger"] = json.dumps({"setup-checked": {"ok": True, "account": account}})
        return resp

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

    @router.post("/setup/signout")
    def setup_signout(request: Request):
        # Sign out = delete the locally-saved sign-in (cookies). Identity config is kept so
        # re-signing in just means pasting a fresh capture.
        if setup is None:
            raise HTTPException(status_code=404, detail="setup not available")
        setup.sign_out()
        ctx.auth_expired.clear()
        return RedirectResponse(f"/setup?flash={quote('Signed out — your saved sign-in was deleted.')}",
                                status_code=303)

    @router.post("/setup")
    async def setup_submit(request: Request):
        if setup is None:
            raise HTTPException(status_code=404, detail="setup not available")
        form = await request.form()
        capture = form.get("headers", "") or ""
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
            setup.apply_setup(capture, identities)
        except ValueError as e:
            rows = [{"label": l, "brand": b} for l, b in zip(labels, brands)] or [{"label": "", "brand": ""}]
            master_idx = int(master) if (master or "").isdigit() else 0
            return _setup_context(request, rows=rows, master_idx=master_idx,
                                  error=str(e), status_code=400)
        ctx.auth_expired.clear()                    # success clears the stale-session banner (in place)
        # Tailor the confirmation to context. "Has synced before" (the same signal that turns the
        # syncbar's green "Sync now" CTA into a neutral "Full sync") tells a re-auth apart from a
        # first-time setup — so we never point at a "Sync now" button that isn't on the page.
        has_synced = bool(store.get_setting("last_sync_at"))
        auto_sync = store.get_setting("auto_sync_plays") == "1"
        if has_synced and auto_sync:
            # Nothing for them to do — auto-sync will catch up. A transient toast, not a banner.
            return RedirectResponse(f"/?toast={quote('You’re authenticated again.')}", status_code=303)
        if has_synced:
            # Re-authed but auto-sync is off, so they need to act — keep a persistent banner.
            msg = "You’re authenticated again. Click “Full sync” to catch up — or turn on “Sync plays” to keep it automatic."
        else:
            n = len(identities)
            msg = f"Saved {n} identit{'y' if n == 1 else 'ies'}. Click “Sync now” to pull your playlists."
        return RedirectResponse(f"/?flash={quote(msg)}", status_code=303)

    return router
