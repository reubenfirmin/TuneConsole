"""Recommendation-serving endpoints, returned as lazy htmx fragments."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from yt_playlist.rec import embed, rec_params, recommend
from yt_playlist.rec.rec_dao import RecDao

# feedback kinds that suppress an item (vs 'more'/'less' which only nudge future weights)
_SNOOZE_DAYS = 14


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn

    @router.get("/playlist/{pid}/suggestions")
    def playlist_suggestions(request: Request, pid: int):
        if store.get_playlist(pid) is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        return templates.TemplateResponse(request, "_partials/playlist_suggestions.html", {
            "suggestions": recommend.complete_playlist(store, pid, now=now_fn()),
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
        # online weight update, but 'already know it' (own_it) suppresses WITHOUT a taste penalty
        lane = form.get("lane")
        if lane and reason != "own_it":
            if kind in ("dismiss", "less", "not_now"):
                store.nudge_weight(f"lane:{lane}", 0.85)
            elif kind == "more":
                store.nudge_weight(f"lane:{lane}", 1.15)
        # explicit-axis steering (Home why-chips): nudge a genre/era/artist weight directly
        axis = form.get("axis")
        if axis:
            lo, hi = (rec_params.GENRE_MIN, rec_params.GENRE_MAX) \
                if axis.split(":", 1)[0] in ("genre", "era", "artist") else (0.2, 3.0)
            store.nudge_weight(axis, 1.15 if kind == "more" else 0.85, lo=lo, hi=hi)
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
