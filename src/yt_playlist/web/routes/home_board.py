"""Prepared Home feed, mix rotation and asynchronous pool-job routes."""
import json

from fastapi import APIRouter, Depends, Request, Response

from yt_playlist.rec import home_cards, home_prepared, rec_params
from yt_playlist.rec.home_cards import _epoch
from yt_playlist.web.context import request_form
from yt_playlist.web.routes.home_onboarding import sample_fragment


def build(ctx, views):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates
    recommendations = views.recommendations

    @router.get("/home/feed")
    def home_feed(request: Request):
        return templates.TemplateResponse(request, "_partials/home_feed.html",
                                          views.feed_context(request, now_fn()))

    @router.post("/home/refresh-card/{card}")
    def home_refresh_card(request: Request, card: str):
        if card in ("onboard_library", "onboard_radio"):
            return sample_fragment(ctx, request, card)
        if card not in (*home_cards._CARD_LABELS, "temporal"):
            return Response(status_code=404)
        now = now_fn()
        cap = max(1, rec_params.get_param(store, "erosion_view_cap"))
        store.rec_ledger.refresh_card(card, cap, now)
        store.rec_ledger.refresh_card("cards", cap, now)
        result = home_prepared.cards(store, views.prepared(now), now, recommendations,
                                     views.schedule, record=False)
        proto = next((p for p in result.protos if p["lane"] == card), None)
        if proto is None:
            return Response(status_code=204)
        recommendations.record_modes([proto], result.epoch, now)
        recommendations.record_offer([proto], now, epoch=result.epoch)
        response = templates.TemplateResponse(request, "_partials/generated_playlist.html",
                                               {"p": proto, "regenerating": True})
        recommendations.audit([proto], now, request.url.path)
        return response

    def expired_pool_fragment(request):
        # Polls need a terminal successful fragment after registry retention expires.
        return templates.TemplateResponse(
            request, "_partials/pool_expired.html", {},
            status_code=200 if request.headers.get("HX-Request") == "true" else 404)

    @router.post("/home/fresh-pool")
    def home_fresh_pool(request: Request, form=Depends(request_form)):
        parameters = {key: sorted(set(form.getlist(key))) for key in ("include_genres", "avoid_genres")}
        available = {o["family"] for o in (views.prepared(now_fn()) or {}).get("genre_options", [])}
        if any(len(form.getlist(key)) > 100 or any(g not in available for g in values)
               for key, values in parameters.items()):
            return Response(status_code=400)
        return views.pool_fragment(request, recommendations.start_pool("fresh_pool", parameters, []))

    @router.get("/home/fresh-pool/{job_id}")
    def home_fresh_pool_status(request: Request, job_id: int):
        job = ctx.jobs.get(job_id)
        if job is None or job.kind != "fresh_pool":
            return expired_pool_fragment(request)
        return views.pool_fragment(request, job)

    @router.post("/home/library-pool")
    def home_library_pool(request: Request, form=Depends(request_form)):
        try:
            parameters = {key: sorted(set(form.getlist(key))) for key in ("include_genres", "avoid_genres")}
            parameters.update(lane=form.get("lane"), epoch=int(form.get("epoch", "-1")))
            excluded = json.loads(form.get("exclude_keys") or "[]")
        except (TypeError, ValueError):
            return Response(status_code=400)
        available = {o["family"] for o in (views.prepared(now_fn()) or {}).get("genre_options", [])}
        if (parameters["lane"] not in ("wheelhouse", "explore", "comfort", "temporal")
                or parameters["epoch"] != _epoch(store, "cards")
                or any(len(form.getlist(key)) > 100 or any(g not in available for g in parameters[key])
                       for key in ("include_genres", "avoid_genres"))
                or not isinstance(excluded, list) or len(excluded) > 200
                or any(not isinstance(k, str) or len(k) > 1000 for k in excluded)):
            return Response(status_code=400)
        return views.pool_fragment(request, recommendations.start_pool("library_pool", parameters, excluded))

    @router.get("/home/library-pool/{job_id}")
    def home_library_pool_status(request: Request, job_id: int):
        job = ctx.jobs.get(job_id)
        if job is None or job.kind != "library_pool":
            return expired_pool_fragment(request)
        return views.pool_fragment(request, job)

    @router.get("/home/cards")
    def home_cards_fragment(request: Request):
        return views.cards_safe(request, now_fn())

    @router.post("/home/refresh-cards")
    def home_refresh_cards(request: Request):
        now = now_fn()
        store.rec_ledger.refresh_card("cards", max(1, rec_params.get_param(store, "erosion_view_cap")), now)
        try:
            return views.cards_fragment(request, now, regenerating=True)
        except Exception:  # noqa: BLE001 - preserve the GET's safe-fragment contract
            ctx.logger.exception("home cards refresh render failed")
            return templates.TemplateResponse(request, "_partials/mode_cards.html", {"protos": []})

    return router
