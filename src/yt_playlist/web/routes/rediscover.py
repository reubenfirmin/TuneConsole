"""Rediscover tab: stale/low-listen playlists, with dismiss/snooze/restore."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from yt_playlist import analysis


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.get("/discover")
    def discover(request: Request):
        now = now_fn()
        hidden = store.get_stale_dismissed(now)                       # [(ytm, until)]
        hidden_set = {ytm for ytm, _ in hidden}
        by_ytm = {p.ytm_playlist_id: p for p in store.get_playlists()}
        snoozed = [{"playlist": by_ytm[ytm], "until": until}
                   for ytm, until in hidden if ytm in by_ytm]
        snoozed.sort(key=lambda s: (s["until"] is None, s["until"] or 0))
        return templates.TemplateResponse(request, "discover.html", {
            "stale": analysis.find_stale(store, now, exclude_ytm=hidden_set)[:50],
            "snoozed": snoozed,
            "labels": {i.id: i.label for i in store.get_identities()},
            "flash": request.query_params.get("flash"),
        })

    @router.post("/rediscover/dismiss")
    async def rediscover_dismiss(request: Request):
        form = await request.form()
        store.dismiss_stale(form["ytm"], until=None)                  # hide forever
        return JSONResponse({"ok": True})

    @router.post("/rediscover/snooze")
    async def rediscover_snooze(request: Request):
        form = await request.form()
        days = float(form.get("days", 30))
        store.dismiss_stale(form["ytm"], until=now_fn() + days * 86400.0)
        return JSONResponse({"ok": True})

    @router.post("/rediscover/restore")
    async def rediscover_restore(request: Request):
        form = await request.form()
        store.restore_stale(form["ytm"])
        return RedirectResponse("/discover", status_code=303)

    return router
