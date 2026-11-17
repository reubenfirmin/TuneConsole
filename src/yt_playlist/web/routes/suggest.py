"""Recommendation-serving endpoints, returned as lazy htmx fragments."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from yt_playlist.web.context import Ctx, request_form

def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn

    @router.get("/playlist/{pid}/suggestions")
    def playlist_suggestions(request: Request, pid: int):
        try:
            model = Ctx.suggestions(ctx).completion(pid, now_fn())
        except LookupError:
            raise HTTPException(status_code=404, detail="playlist not found")
        return templates.TemplateResponse(request, "_partials/playlist_suggestions.html", model)

    @router.get("/track/{vid}/similar")
    def track_similar(request: Request, vid: str, pid: int | None = None):
        """'Songs like this': embedding neighbours of one track, rendered as a modal fragment. When
        `pid` is given (the playlist the track was opened from) the modal lets you add any neighbour
        into that playlist, slotted just below `vid`."""
        return templates.TemplateResponse(request, "_partials/similar_modal.html",
                                          Ctx.suggestions(ctx).similar(vid, pid, now_fn()))

    @router.post("/recs/rebuild")
    def recs_rebuild():
        """Kick off a model rebuild + proposal materialization. Dispatched to the background worker
        (coalesced) so a hung YTM/Last.fm call during materialization can't tie up a request worker."""
        result = Ctx.suggestions(ctx).request_rebuild()
        return JSONResponse(result, status_code=200 if result['ok'] else 503)

    @router.post("/recs/feedback")
    def recs_feedback(request: Request, form=Depends(request_form)):
        """Persist a feedback event. htmx removes the card by swapping in the empty response."""
        item = form.get("item")
        if not item:
            return HTMLResponse("", status_code=422)
        Ctx.suggestion_feedback(ctx).feedback(item, form.get('kind', 'dismiss'), form.get('reason'),
            form.get('surface', 'for_you'), form.get('scope', ''), form.get('lane'), form.get('axis'), now_fn())
        return HTMLResponse("")

    @router.post("/recs/mood")
    def recs_mood(request: Request, form=Depends(request_form)):
        """Transient mood feedback: tilts the Home lanes toward (+) or away (-) from a vibe. #85: it
        fades on the wall clock (mood_halflife_d), reactive, but NOT a permanent taste signal. Two
        shapes:
          - whole-mix (simple panel): `pid` -> seeds with the whole playlist; swaps in a confirmation.
          - facet/track levers: explicit `keys` (JSON list) of just that subset; returns a light ack.
        `intensity=lot` doubles the magnitude (a stronger tilt)."""
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
            Ctx.suggestion_feedback(ctx).mood(keys, signed, now_fn())
            return HTMLResponse("")
        try:                                          # whole-mix simple buttons
            pid = int(form.get("pid"))
        except (TypeError, ValueError):
            return HTMLResponse("", status_code=422)
        Ctx.suggestion_feedback(ctx).mood([], signed, now_fn(), playlist_id=pid)
        return HTMLResponse("")                        # no swap: the panel stays put (Advanced reachable)

    @router.post("/recs/journey")
    def recs_journey(request: Request, form=Depends(request_form)):
        """Permanent feedback on a generated mix's JOURNEY (its ordering shape). 👍/👎 nudges that
        journey's weight via the same graduation ledger as genres/eras, so preferred flows roll more
        often. Reads the journey from the playlist's stored recipe."""
        try:
            pid = int(form.get("pid"))
            direction = int(form.get("dir", 1))
        except (TypeError, ValueError):
            return HTMLResponse("", status_code=422)
        Ctx.suggestion_feedback(ctx).journey(pid, direction, now_fn())
        return HTMLResponse("")

    return router
