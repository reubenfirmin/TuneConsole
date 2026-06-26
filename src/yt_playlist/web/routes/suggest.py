"""Recommendation-serving endpoints, returned as lazy htmx fragments."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from yt_playlist.rec import embed, rec_params, recommend, scoring
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.util import genre_map

# feedback kinds that suppress an item (vs 'more'/'less' which only nudge future weights)
_SNOOZE_DAYS = 14


def _feedback_axis(store, form, reason, item):
    """The single taste axis a suggestion-dismiss / why-chip should steer, derived from the dismissed
    track (#43). An explicit `axis` form value wins (the Home why-chips, and the 'not this artist'
    tile, which sends artist:<name>). Otherwise map the reason via the track's own metadata. Returns
    None when there is nothing to steer (the track has no genre/decade/artist, or it is not actually
    mainstream), so the caller no-ops gracefully rather than inventing a steer."""
    axis = form.get("axis")
    if axis and axis.split(":", 1)[0] in ("genre", "era", "artist", "pop"):
        return axis
    dao = RecDao(store)
    if reason == "vibe":
        g = dao.track_genres([item]).get(item)
        return f"genre:{genre_map.family(g)}" if g else None
    if reason == "era":
        d = dao.track_decades([item]).get(item)
        return f"era:{d}" if d else None
    if reason == "artist":
        a = dao.track_artists([item]).get(item)
        return f"artist:{a}" if a else None
    if reason == "mainstream":
        pop = dao.track_popularity([item]).get(item)
        band = scoring._pop_band(pop, rec_params.get_param(store, "pop_mainstream_min"))
        return f"pop:{band}" if band else None
    return None


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn

    @router.get("/playlist/{pid}/suggestions")
    def playlist_suggestions(request: Request, pid: int):
        if store.get_playlist(pid) is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        now = now_fn()
        suggestions = recommend.complete_playlist(store, pid, now=now)
        # #24/#28: append related-artist pulls (incl. out-of-corpus tracks the in-library completer
        # can't reach), deduped against the completer's own picks.
        have = {s.key for s in suggestions}
        suggestions += [r for r in recommend.related_artist_suggestions(store, pid, now)
                        if r.key not in have]
        return templates.TemplateResponse(request, "_partials/playlist_suggestions.html", {
            "suggestions": suggestions,
            "pid": pid,
        })

    @router.get("/track/{vid}/similar")
    def track_similar(request: Request, vid: str, pid: int | None = None):
        """'Songs like this': embedding neighbours of one track, rendered as a modal fragment. When
        `pid` is given (the playlist the track was opened from) the modal lets you add any neighbour
        into that playlist, slotted just below `vid`."""
        dao = RecDao(store)
        key = dao.key_for_video(vid)
        nbrs = embed.neighbors(store, key, topn=12) if key else []
        if not nbrs and key:                              # new/quarantined track: proxy via its artist
            nbrs = embed.neighbors_for_unmodeled(store, key, topn=12)
        meta = store.tracks_by_keys([k for k, _ in nbrs] + ([key] if key else []))
        items = [meta[k] for k, _ in nbrs if k in meta]
        return templates.TemplateResponse(request, "_partials/similar_modal.html",
                                          {"items": items, "seed": meta.get(key, {}),
                                           "have_model": store.rec_vectors_count() > 0,
                                           "pid": pid, "seed_vid": vid})

    @router.post("/recs/rebuild")
    def recs_rebuild():
        """Kick off a model rebuild + proposal materialization. Dispatched to the background worker
        (coalesced) so a hung YTM/Last.fm call during materialization can't tie up a request worker;
        falls back to a synchronous rebuild only when no worker is configured."""
        if ctx.rec_worker:
            ctx.rec_worker.trigger()
        else:
            embed.build_and_store(store)
        return JSONResponse({"ok": True, "queued": bool(ctx.rec_worker),
                             "count": store.rec_vectors_count()})

    @router.post("/recs/feedback")
    async def recs_feedback(request: Request):
        """Persist a feedback event. htmx removes the card by swapping in the empty response."""
        form = await request.form()
        item = form.get("item")
        if not item:
            return HTMLResponse("", status_code=422)
        kind, reason = form.get("kind", "dismiss"), form.get("reason")
        now = now_fn()
        until = now + _SNOOZE_DAYS * 86400 if kind == "not_now" else None
        store.record_feedback(form.get("surface", "for_you"), item, kind,
                              reason=reason, scope=form.get("scope", ""), until=until, now=now)
        # Taste steering goes THROUGH the graduation ledger, never a direct permanent nudge: the funnel
        # owns permanent weights (#43 / §4b, the old leak). Each reason maps to the axis its label
        # promises (wrong era -> era, wrong vibe -> genre, too mainstream -> pop, not this artist ->
        # artist); 'already know it' (own_it) suppresses ONLY, with no taste penalty.
        if reason != "own_it":
            axis = _feedback_axis(store, form, reason, item)
            if axis:
                signed = 1.0 if kind == "more" else -1.0
                recommend.graduate_facet(store, axis, signed, now,
                                         source=rec_params.get_param(store, "source_w_feedback"),
                                         source_label="feedback")
        # Lane balance stays a direct nudge: it is a UI mechanic (which lane fills the feed), not a
        # taste facet the graduation ledger owns.
        lane = form.get("lane")
        if lane and reason != "own_it":
            if kind in ("dismiss", "less", "not_now"):
                store.nudge_weight(f"lane:{lane}", 0.85)
            elif kind == "more":
                store.nudge_weight(f"lane:{lane}", 1.15)
        return HTMLResponse("")

    @router.post("/recs/mood")
    async def recs_mood(request: Request):
        """Transient mood feedback: tilts the Home lanes toward (+) or away (-) from a vibe. It sticks
        until you change it (and only relaxes once your sync goes stale), reactive, but NOT a
        permanent taste signal. Two shapes:
          - whole-mix (simple panel): `pid` -> seeds with the whole playlist; swaps in a confirmation.
          - facet/track levers: explicit `keys` (JSON list) of just that subset; returns a light ack.
        `intensity=lot` doubles the magnitude (a stronger tilt)."""
        form = await request.form()
        try:
            direction = int(form.get("dir", 1))
        except (TypeError, ValueError):
            return HTMLResponse("", status_code=422)
        signed = (1 if direction >= 0 else -1) * (2 if form.get("intensity") == "lot" else 1)
        keys_raw = form.get("keys")
        if keys_raw:                                  # facet / per-track lever: tilt just this subset
            try:
                keys = json.loads(keys_raw)
            except (ValueError, TypeError):
                keys = []
            if keys:
                store.record_mood(keys, signed, now_fn())
                recommend.graduate_moods(store, keys, signed, now_fn(), source=rec_params.get_param(store, "source_w_vibe"))
            return HTMLResponse("")
        try:                                          # whole-mix simple buttons
            pid = int(form.get("pid"))
        except (TypeError, ValueError):
            return HTMLResponse("", status_code=422)
        keys = store.get_playlist_track_keys(pid)
        if keys:
            store.record_mood(keys, signed, now_fn())
            recommend.graduate_moods(store, list(keys), signed, now_fn(), source=rec_params.get_param(store, "source_w_vibe"))
        return HTMLResponse("")                        # no swap: the panel stays put (Advanced reachable)

    @router.post("/recs/journey")
    async def recs_journey(request: Request):
        """Permanent feedback on a generated mix's JOURNEY (its ordering shape). 👍/👎 nudges that
        journey's weight via the same graduation ledger as genres/eras, so preferred flows roll more
        often. Reads the journey from the playlist's stored recipe."""
        form = await request.form()
        try:
            pid = int(form.get("pid"))
            direction = int(form.get("dir", 1))
        except (TypeError, ValueError):
            return HTMLResponse("", status_code=422)
        pl = store.get_playlist(pid)
        recipe = store.get_recipe(pl.ytm_playlist_id) if pl else None
        journey = (recipe or {}).get("journey")
        if journey:
            recommend.graduate_facet(store, f"journey:{journey}", 1 if direction >= 0 else -1, now_fn())
        return HTMLResponse("")

    return router
