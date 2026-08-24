"""Setup wizard: identities, provider configuration, and local import connections."""
import secrets
import threading
from urllib.parse import quote

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from yt_playlist.core.setup import BROWSER_CREDENTIAL_FILENAME
from yt_playlist.library.takeout import (TakeoutFormatError, import_takeout,
                                         seed_discovery_from_unmatched)
from yt_playlist.providers import enrichment, lastfm, spotify

_SPOTIFY_KEYS = ("spotify_client_id", "spotify_access_token", "spotify_refresh_token",
                 "spotify_access_expires_at", "spotify_profile_id", "spotify_profile_name")
_OAUTH_ATTEMPT_TTL_S = 600


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
            "spotify_connected": bool(store.get_setting("spotify_refresh_token")),
            "spotify_client_id": store.get_setting("spotify_client_id", ""),
            "spotify_profile_name": store.get_setting("spotify_profile_name", ""),
            "spotify_redirect_uri": _spotify_redirect_uri(request),
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

    def _spotify_redirect_uri(request):
        # Spotify permits loopback HTTP only with the literal 127.0.0.1 host (not localhost).
        port = request.url.port
        return f"http://127.0.0.1{f':{port}' if port else ''}/spotify/callback"

    @router.post("/spotify/connect")
    async def spotify_connect(request: Request):
        form = await request.form()
        client_id = (form.get("client_id") or "").strip()
        if not client_id or len(client_id) > 200 or any(ch.isspace() for ch in client_id):
            return RedirectResponse(
                f"/setup?tab=import&flash={quote('Enter a valid Spotify Client ID.')}", status_code=303)
        verifier, challenge = spotify.new_pkce()
        state = secrets.token_urlsafe(32)
        redirect_uri = _spotify_redirect_uri(request)
        # Clear expired attempts opportunistically, then retain only this short-lived verifier.
        now = ctx.now()
        for old in [k for k, v in ctx.spotify_oauth.items()
                    if now - v["created_at"] > _OAUTH_ATTEMPT_TTL_S]:
            ctx.spotify_oauth.pop(old, None)
        ctx.spotify_oauth[state] = {"verifier": verifier, "client_id": client_id,
                                    "redirect_uri": redirect_uri, "created_at": now}
        expiry = threading.Timer(_OAUTH_ATTEMPT_TTL_S, ctx.spotify_oauth.pop,
                                 args=(state, None))
        expiry.daemon = True
        expiry.start()
        return RedirectResponse(
            spotify.authorization_url(client_id, redirect_uri, state, challenge), status_code=303)

    @router.get("/spotify/callback")
    def spotify_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        attempt = ctx.spotify_oauth.pop(state, None)
        if error:
            return RedirectResponse(
                f"/setup?tab=import&flash={quote('Spotify connection was cancelled.')}", status_code=303)
        if (not attempt or not code
                or ctx.now() - attempt["created_at"] > _OAUTH_ATTEMPT_TTL_S):
            return RedirectResponse(
                f"/setup?tab=import&flash={quote('Spotify connection expired. Try again.')}", status_code=303)
        try:
            token = spotify.exchange_code(
                requests.Session(), attempt["client_id"], attempt["redirect_uri"],
                code, attempt["verifier"])
            access = token["access_token"]
            profile = spotify.SpotifyImportClient(access).profile()
        except (KeyError, spotify.SpotifyError, OSError):
            ctx.logger.warning("Spotify OAuth callback failed", exc_info=True)
            return RedirectResponse(
                f"/setup?tab=import&flash={quote('Spotify could not be connected. Try again.')}",
                status_code=303)
        store.set_setting("spotify_client_id", attempt["client_id"])
        store.set_setting("spotify_access_token", access)
        if token.get("refresh_token"):
            store.set_setting("spotify_refresh_token", token["refresh_token"])
        store.set_setting("spotify_access_expires_at", str(ctx.now() + int(token.get("expires_in", 3600))))
        store.set_setting("spotify_profile_id", profile.get("id") or "")
        store.set_setting("spotify_profile_name", profile.get("name") or "Spotify")
        return RedirectResponse(
            f"/setup?tab=import&flash={quote('Spotify connected for playlist import.')}", status_code=303)

    @router.post("/spotify/disconnect")
    def spotify_disconnect():
        for key in _SPOTIFY_KEYS:
            store.delete_setting(key)
        ctx.spotify_oauth.clear()
        return RedirectResponse(
            f"/setup?tab=import&flash={quote('Spotify disconnected and its local tokens deleted.')}",
            status_code=303)

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
