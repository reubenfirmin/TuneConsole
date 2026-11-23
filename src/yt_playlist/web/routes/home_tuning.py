"""Temporary Home genre, vibe and fingerprint adjustments."""
import json
from threading import Lock

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from yt_playlist.rec import home_vibe, mix_tuning
from yt_playlist.web.context import Ctx, form_float, request_form
from yt_playlist.web.routes.home_access import guard, vibe_selection


def build(ctx, views):
    router = APIRouter()
    mixes = APIRouter(dependencies=[Depends(guard(ctx, 'directions'))])
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates
    commands = Ctx.home_tuning(ctx, views.prepared)
    vibe_lock = Lock()

    @mixes.post("/home/other-genres")
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

    @mixes.post("/home/mix-genres")
    def home_mix_genres(request: Request, form=Depends(request_form)):
        """Choose genre families for one menu, without teaching a lasting preference."""
        genres = form.getlist("genres")
        now = now_fn()
        try:
            commands.genre_request(genres, now)
        except ValueError:
            return Response(status_code=400)
        return views.cards_safe(request, now)

    # Vibe access already establishes access through Mix Tapes.
    @router.post("/home/vibe")
    def home_vibe_change(request: Request, form=Depends(request_form)):
        with vibe_selection(ctx) as now:
            vibe = form.get("vibe", "")
            if vibe and vibe not in home_vibe.PRESETS:
                return Response(status_code=400)
            with vibe_lock, home_vibe.authorized(store, vibe):
                with mix_tuning.adjusting_vibe(store, now) as adjustment:
                    if vibe:
                        store.settings.set_setting(home_vibe.SETTING, vibe)
                    else:
                        store.settings.delete_setting(home_vibe.SETTING)
                return views.cards_safe(request, now, remaining=adjustment.remaining)

    @mixes.post("/home/breadth")
    def home_breadth(request: Request, form=Depends(request_form)):
        # Drag the Breadth bar -> persist the focused<->eclectic bias and return only the re-ranked
        # four-card board. The bias is a scalar param (not a per-axis weight),
        # so it gets its own route; set_param clamps it to the spec range [-1, 1]. Center (0) is neutral.
        bias = form.get("breadth_bias")
        now = now_fn()
        commands.breadth(bias, now)
        return views.cards_safe(request, now)

    @mixes.post("/home/steer")
    def home_steer(request: Request, form=Depends(request_form)):
        # Drag a fingerprint bar -> set a temporary lean and return
        # only the re-ranked four-card board. The bar shows the EFFECTIVE multiplier (permanent x lean);
        # we store the lean so permanent x lean == the dragged target. Long-term taste is edited only
        # on the Taste Model page. These adjustments expire after four hours.
        axis, weight = form.get("axis"), form.get("weight")
        target = form_float(weight)
        commands.steer(axis, target, now_fn())
        return views.cards_safe(request, now_fn())

    @mixes.post("/home/fingerprint/add")
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

    @mixes.post("/home/fingerprint/remove")
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

    @mixes.get("/home/fingerprint")
    def fingerprint_status(request: Request, dialog: bool = False):
        # Recheck the persisted deadline: another tab may have extended the tuning window.
        now = now_fn()
        commands.expire(now)
        if dialog:
            return views.tuning_dialog(request, now)
        return views.tuning_fragment(request, now)

    @mixes.post("/home/fingerprint/reset")
    def fingerprint_reset(request: Request):
        """Clear all manual Home choices and refresh both controls and mixes together."""
        commands.reset()
        response = views.tuning_fragment(request, now_fn())
        # Close before replacing the controls so their remembered open state cannot reopen them.
        response.headers["HX-Trigger"] = "home-mixes-applied"
        return response

    @mixes.get("/home/genres")
    def home_genres():
        """Full genre taxonomy (families + sub-genres) for the Home taste-bar genre picker. Unlike the
        library-scoped clusters list, this is the whole map, so you can pin a bar for a genre you have
        zero plays of yet. Each option: {name, kind: 'family'|'genre'}; the Alpine picker filters it
        client-side (so reset/close are instant) and posts a pick to /home/fingerprint/add."""
        return JSONResponse({"options": commands.picker()})

    router.include_router(mixes)
    return router

