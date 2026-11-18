"""Temporary Home genre, vibe and fingerprint adjustments."""
import json

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from yt_playlist.providers import genres as genre_lib
from yt_playlist.rec import home_cache, home_prepared, home_vibe, mix_tuning, rec_params
from yt_playlist.rec.home_cards import _epoch
from yt_playlist.web.context import Ctx, form_float, request_form
from yt_playlist.web.routes.home_access import guard, vibe_selection


def build(ctx, views):
    router = APIRouter(dependencies=[Depends(guard(ctx, 'directions'))])
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates
    commands = Ctx.home_tuning(ctx, views.prepared)

    @router.post("/home/other-genres")
    def home_other_genres(request: Request, form=Depends(request_form)):
        """One-menu request based on the genres in this browser's currently displayed cards.
        Expires with the next rotation; no lean, permanent weight or dislike is written."""
        try:
            genres = json.loads(form.get("genres") or "[]")
        except (TypeError, ValueError):
            return Response(status_code=400)
        now = now_fn()
        try:
            commands.genre_request(genres, now, avoid=True)
        except ValueError:
            return Response(status_code=400)
        return views.cards_safe(request, now)

    @router.post("/home/mix-genres")
    def home_mix_genres(request: Request, form=Depends(request_form)):
        """Choose genre families for one menu, without teaching a lasting preference."""
        genres = form.getlist("genres")
        now = now_fn()
        try:
            commands.genre_request(genres, now)
        except ValueError:
            return Response(status_code=400)
        return views.cards_safe(request, now)

    @router.post("/home/vibe")
    def home_vibe_change(request: Request, form=Depends(request_form)):
        with vibe_selection(ctx, request) as now:
            vibe = form.get("vibe", "")
            if vibe and vibe not in home_vibe.PRESETS:
                return Response(status_code=400)
            accepted = ctx.vibe_requests.select(vibe, now)
            # Preserve the mounted board and player while the worker builds.
            return views.tuning_response(Response(headers={'HX-Reswap': 'none',
                'X-Home-Vibe-Request': accepted['id']}), now)

    @router.get('/home/vibe/status')
    def home_vibe_status():
        ctx.vibe_requests.resume()
        return JSONResponse(ctx.vibe_requests.state(), headers={'Cache-Control': 'no-store'})

    @router.post("/home/breadth")
    def home_breadth(request: Request, form=Depends(request_form)):
        # Drag the Breadth bar -> persist the focused<->eclectic bias and return only the re-ranked
        # four-card board. The bias is a scalar param (not a per-axis weight),
        # so it gets its own route; set_param clamps it to the spec range [-1, 1]. Center (0) is neutral.
        bias = form.get("breadth_bias")
        now = now_fn()
        commands.breadth(bias, now)
        return views.cards_safe(request, now)

    @router.post("/home/steer")
    def home_steer(request: Request, form=Depends(request_form)):
        # Drag a fingerprint bar -> set a temporary lean and return
        # only the re-ranked four-card board. The bar shows the EFFECTIVE multiplier (permanent x lean);
        # we store the lean so permanent x lean == the dragged target. Long-term taste is edited only
        # on the Taste Model page. These adjustments expire after four hours.
        axis, weight = form.get("axis"), form.get("weight")
        target = form_float(weight)
        commands.steer(axis, target, now_fn())
        return views.cards_safe(request, now_fn())

    @router.post("/home/fingerprint/add")
    def fingerprint_add(request: Request, form=Depends(request_form)):
        """Pin an axis (e.g. 'genre:gqom') so it appears as a steerable bar even with zero plays. Writes
        a neutral lean (1.0) so it persists, then returns ONLY the re-rendered genre bars (server-
        authoritative, so dedup/ordering are correct). The client swaps #fp-genre-bars with this and
        leaves the live genre picker untouched. A neutral add doesn't change rankings, so there's no
        need to re-render the whole feed (and re-rendering it would churn the picker)."""
        axis = (form.get("axis") or "").strip()
        now = now_fn()
        commands.pin(axis, now)
        return views.tuning_response(templates.TemplateResponse(
            request, "_partials/fingerprint_genre_bars.html",
            {"fingerprint": commands.fingerprint(now)}), now)

    @router.post("/home/fingerprint/remove")
    def fingerprint_remove(request: Request, form=Depends(request_form)):
        """Remove one bar from the Home panel: hide the axis (display-only) AND clear any standing lean
        so it stops steering. Works for ANY bar: a top played family, a steered family, or an added
        niche, all simply disappear. Returns the re-rendered bars (#fp-genre-bars swap, picker left
        alive). Permanent taste (the Taste page) is untouched; the genre picker re-adds it any time."""
        axis = (form.get("axis") or "").strip()
        now = now_fn()
        commands.remove(axis, now)
        return views.tuning_response(templates.TemplateResponse(
            request, "_partials/fingerprint_genre_bars.html",
            {"fingerprint": commands.fingerprint(now)}), now)

    @router.get("/home/fingerprint")
    def fingerprint_status(request: Request):
        # Recheck the persisted deadline: another tab may have extended the tuning window.
        now = now_fn()
        commands.expire(now)
        return views.tuning_fragment(request, now)

    @router.post("/home/fingerprint/reset")
    def fingerprint_reset(request: Request):
        """Clear all manual Home choices and refresh both controls and mixes together."""
        commands.reset()
        response = views.tuning_fragment(request, now_fn())
        # Close before replacing the controls so their remembered open state cannot reopen them.
        response.headers["HX-Trigger"] = "home-mixes-applied"
        return response

    @router.get("/home/genres")
    def home_genres():
        """Full genre taxonomy (families + sub-genres) for the Home taste-bar genre picker. Unlike the
        library-scoped clusters list, this is the whole map, so you can pin a bar for a genre you have
        zero plays of yet. Each option: {name, kind: 'family'|'genre'}; the Alpine picker filters it
        client-side (so reset/close are instant) and posts a pick to /home/fingerprint/add."""
        return JSONResponse({"options": commands.picker()})

    return router

