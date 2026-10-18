"""Optional read-only Google authorization for actual playlist creation dates."""
import asyncio
import secrets
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from yt_playlist.library.sync import sync_playlist_dates
from yt_playlist.providers.playlist_dates import PlaylistDatesError

BASE = "/setup/playlist-dates"
COOKIE = "playlist_dates_browser"


def build(ctx):
    router = APIRouter()

    def provider():
        if ctx.playlist_dates is None:
            raise HTTPException(404, "playlist date connection unavailable")
        return ctx.playlist_dates

    def back(message):
        response = RedirectResponse(BASE + "?message=" + quote(message), status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.get(BASE)
    def page(request: Request):
        status = provider().status()
        playlists = [p for p in ctx.store.get_playlists() if p.ytm_playlist_id.startswith("PL")]
        return ctx.templates.TemplateResponse(request, "playlist_dates.html", {
            "connection": status, "known": sum(p.created_at is not None for p in playlists),
            "total": len(playlists), "message": request.query_params.get("message"),
        }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @router.post(BASE + "/connect")
    async def connect(request: Request):
        # Never construct a Google redirect pointing at a caller-supplied remote host.
        if request.url.hostname not in ("localhost", "127.0.0.1"):
            raise HTTPException(400, "Open TuneConsole on localhost to connect Google.")
        form = await request.form()
        upload = form.get("file")
        raw = await upload.read(32_769) if upload and hasattr(upload, "read") else b""
        if len(raw) > 32_768:
            return back("That file is too large. Choose the Google Desktop app client JSON.")
        nonce = secrets.token_urlsafe(32)
        redirect = str(request.url.replace(path=BASE + "/callback", query=""))
        try:
            url = await asyncio.to_thread(provider().begin, raw, redirect, nonce)
        except PlaylistDatesError as e:
            return back(str(e))
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(COOKIE, nonce, max_age=600, httponly=True, samesite="lax", path=BASE)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.get(BASE + "/callback")
    async def callback(request: Request):
        try:
            await asyncio.to_thread(provider().finish, request.query_params.get("state", ""),
                                    request.query_params.get("code", ""), request.cookies.get(COOKIE, ""))
            report = await asyncio.to_thread(sync_playlist_dates, ctx.store, provider())
            message = (report["errors"][0] if report["errors"] else
                       f"Connected. Updated creation dates for {report['updated']} playlists.")
        except PlaylistDatesError as e:
            message = str(e)
        except OSError:
            message = "Could not save this connection locally. Check that your configuration folder is writable."
        response = back(message)
        response.delete_cookie(COOKIE, path=BASE)
        return response

    @router.post(BASE + "/refresh")
    async def refresh():
        report = await asyncio.to_thread(sync_playlist_dates, ctx.store, provider())
        return back(report["errors"][0] if report["errors"] else
                    f"Updated creation dates for {report['updated']} playlists.")

    @router.post(BASE + "/disconnect")
    async def disconnect(request: Request):
        form = await request.form()
        await asyncio.to_thread(provider().disconnect, str(form.get("account", "")))
        return back("Disconnected. Your saved playlist dates are kept.")

    return router
