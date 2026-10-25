"""Temporary Home genre, vibe and fingerprint adjustments."""
import json

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from yt_playlist.rec import home_prepared, home_vibe, mix_tuning, rec_params
from yt_playlist.rec.home_cards import _epoch
from yt_playlist.util import genre_map
from yt_playlist.web.context import form_float, request_form


def build(ctx, views):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.post("/home/other-genres")
    def home_other_genres(request: Request, form=Depends(request_form)):
        """One-menu request based on the genres in this browser's currently displayed cards.
        Expires with the next rotation; no lean, permanent weight or dislike is written."""
        try:
            genres = json.loads(form.get("genres") or "[]")
        except (TypeError, ValueError):
            return Response(status_code=400)
        if not isinstance(genres, list) or not genres or len(genres) > 100 or any(
                not isinstance(g, str) or not g.strip() or len(g) > 100 for g in genres):
            return Response(status_code=400)
        now = now_fn()
        with mix_tuning.adjusting(store, now):
            store.rec_ledger.refresh_card("cards", max(1, rec_params.get_param(store, "erosion_view_cap")), now)
            store.rec_proposals.put_proposals("home_genre_request", {
                "epoch": _epoch(store, "cards"),
                "avoid_genres": sorted({genre_map.family(g) for g in genres}),
                **({'held_by_vibe': True} if store.settings.get_setting(home_vibe.SETTING) else {}),
            }, now)
        return views.cards_safe(request, now)

    @router.post("/home/mix-genres")
    def home_mix_genres(request: Request, form=Depends(request_form)):
        """Choose genre families for one menu, without teaching a lasting preference."""
        genres = form.getlist("genres")
        available = {option["family"] for option in (views.prepared(now_fn()) or {}).get("genre_options", [])}
        if not genres or len(genres) > 100 or any(not isinstance(g, str) or g not in available for g in genres):
            return Response(status_code=400)
        now = now_fn()
        with mix_tuning.adjusting(store, now):
            store.rec_ledger.refresh_card("cards", max(1, rec_params.get_param(store, "erosion_view_cap")), now)
            store.rec_proposals.put_proposals("home_genre_request", {
                "epoch": _epoch(store, "cards"), "include_genres": sorted(set(genres)),
                **({'held_by_vibe': True} if store.settings.get_setting(home_vibe.SETTING) else {}),
            }, now)
        return views.cards_safe(request, now)

    @router.post("/home/vibe")
    def home_vibe_change(request: Request, form=Depends(request_form)):
        vibe = form.get("vibe", "")
        if vibe and vibe not in home_vibe.PRESETS:
            return Response(status_code=400)
        now = now_fn()
        with mix_tuning.adjusting_vibe(store, now):
            if vibe:
                store.settings.set_setting(home_vibe.SETTING, vibe)
            else:
                store.settings.delete_setting(home_vibe.SETTING)
        return views.cards_safe(request, now)

    @router.post("/home/breadth")
    def home_breadth(request: Request, form=Depends(request_form)):
        # Drag the Breadth bar -> persist the focused<->eclectic bias and return only the re-ranked
        # four-card board. The bias is a scalar param (not a per-axis weight),
        # so it gets its own route; set_param clamps it to the spec range [-1, 1]. Center (0) is neutral.
        bias = form.get("breadth_bias")
        now = now_fn()
        if bias is not None:
            with mix_tuning.adjusting(store, now):
                rec_params.set_param(store, "breadth_bias", bias)
        return views.cards_safe(request, now)

    @router.post("/home/steer")
    def home_steer(request: Request, form=Depends(request_form)):
        # Drag a fingerprint bar -> set a temporary lean and return
        # only the re-ranked four-card board. The bar shows the EFFECTIVE multiplier (permanent x lean);
        # we store the lean so permanent x lean == the dragged target. Long-term taste is edited only
        # on the Taste Model page. These adjustments expire after four hours.
        axis, weight = form.get("axis"), form.get("weight")
        target = form_float(weight)
        if axis and target is not None and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            now = now_fn()
            with mix_tuning.adjusting(store, now):
                perm = store.rec_model.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d")).get(axis, 1.0)
                lean = target / perm if perm > 0 else target
                lo, hi = rec_params.GENRE_MIN, rec_params.GENRE_MAX
                lean = max(lo, min(hi, lean))
                store.rec_model.set_lean(axis, lean, now)
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
        if axis and axis.split(":", 1)[0] in ("genre", "era", "artist"):
            with mix_tuning.adjusting(store, now):
                store.rec_model.unhide_facet(axis)              # re-adding an axis un-hides it
                # Only write if not already present. Don't overwrite a user-set lean.
                if store.rec_model.get_lean(axis) == 1.0 and axis not in store.rec_model.get_leans():
                    store.rec_model.set_lean(axis, 1.0, now)
        return views.tuning_response(templates.TemplateResponse(
            request, "_partials/fingerprint_genre_bars.html",
            {"fingerprint": home_prepared.fingerprint(store, views.prepared(now), now)}), now)

    @router.post("/home/fingerprint/remove")
    def fingerprint_remove(request: Request, form=Depends(request_form)):
        """Remove one bar from the Home panel: hide the axis (display-only) AND clear any standing lean
        so it stops steering. Works for ANY bar: a top played family, a steered family, or an added
        niche, all simply disappear. Returns the re-rendered bars (#fp-genre-bars swap, picker left
        alive). Permanent taste (the Taste page) is untouched; the genre picker re-adds it any time."""
        axis = (form.get("axis") or "").strip()
        now = now_fn()
        if axis:
            with mix_tuning.adjusting(store, now):
                store.rec_model.hide_facet(axis)
                store.rec_model.clear_lean(axis)
        return views.tuning_response(templates.TemplateResponse(
            request, "_partials/fingerprint_genre_bars.html",
            {"fingerprint": home_prepared.fingerprint(store, views.prepared(now), now)}), now)

    @router.get("/home/fingerprint")
    def fingerprint_status(request: Request):
        # Recheck the persisted deadline: another tab may have extended the tuning window.
        now = now_fn()
        mix_tuning.expire(store, now)
        return views.tuning_fragment(request, now)

    @router.post("/home/fingerprint/reset")
    def fingerprint_reset(request: Request):
        """Clear all manual Home choices and refresh both controls and mixes together."""
        mix_tuning.reset(store)
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
        fams = genre_map.all_families()
        seen = set(fams)
        options = [{"name": f, "kind": "family"} for f in fams]
        options += [{"name": g, "kind": "genre"} for g in genre_map.all_genres() if g not in seen]
        prepared = views.prepared(now_fn()) or {}
        plays = {g.lower(): count for g, count in
                 (prepared.get("fingerprint") or {}).get("genre_plays", {}).items()}
        family_plays = {}
        for genre, count in plays.items():
            family = genre_map.family(genre)
            family_plays[family] = family_plays.get(family, 0) + count
        options.sort(key=lambda o: (-(family_plays.get(o["name"], 0) if o["kind"] == "family"
                                     else plays.get(o["name"], 0)), o["name"]))
        return JSONResponse({"options": options})

    return router

