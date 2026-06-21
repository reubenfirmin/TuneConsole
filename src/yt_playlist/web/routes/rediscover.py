"""Rediscover tab: stale/low-listen playlists, with dismiss/snooze/restore."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from yt_playlist import analysis


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    def _discover_context(request):
        now = now_fn()
        hidden = store.get_stale_dismissed(now)                       # [(ytm, until)]
        hidden_set = {ytm for ytm, _ in hidden}
        by_ytm = {p.ytm_playlist_id: p for p in store.get_playlists()}
        snoozed = [{"playlist": by_ytm[ytm], "until": until}
                   for ytm, until in hidden if ytm in by_ytm]
        snoozed.sort(key=lambda s: (s["until"] is None, s["until"] or 0))
        stale = analysis.find_stale(store, now, exclude_ytm=hidden_set)[:50]
        shown = [s.playlist.id for s in stale] + [s["playlist"].id for s in snoozed]
        return {
            "stale": stale,
            "snoozed": snoozed,
            "labels": {i.id: i.label for i in store.get_identities()},
            "kinds": {pid: store.playlist_kind(pid) for pid in shown},
            "flash": request.query_params.get("flash"),
        }

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    @router.get("/discover")
    def discover(request: Request):
        return templates.TemplateResponse(request, "discover.html", _discover_context(request))

    @router.post("/rediscover/dismiss")
    async def rediscover_dismiss(request: Request):
        form = await request.form()
        ytm = form.get("ytm")
        if not ytm:
            return _toast(request, "Nothing to dismiss.")
        store.dismiss_stale(ytm, until=None)                          # hide forever
        return HTMLResponse("")                                       # htmx fades + removes the row

    @router.post("/rediscover/snooze")
    async def rediscover_snooze(request: Request):
        form = await request.form()
        ytm = form.get("ytm")
        if not ytm:
            return _toast(request, "Nothing to snooze.")
        days = float(form.get("days", 30))
        store.dismiss_stale(ytm, until=now_fn() + days * 86400.0)
        return HTMLResponse("")

    @router.post("/rediscover/restore")
    async def rediscover_restore(request: Request):
        form = await request.form()
        store.restore_stale(form["ytm"])
        return RedirectResponse("/discover", status_code=303)

    return router
