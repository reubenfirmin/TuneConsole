"""The monthly recap story ("Your Month", #79).

There is NO Trends tab: the recap is not a browsable section, it is a once-a-month event surfaced by a
nag card on Home (see _partials/alerts.html) that links straight into the full-screen reel here. This
route only renders that reel from the baked `story` payload the rec worker materialised
(rec/recap_story.py) -- no computation here.
"""
from fastapi import APIRouter, HTTPException, Request

from yt_playlist.rec.recap_story import listening_summary
from yt_playlist.rec.recap_details import slides
from yt_playlist.rec.trend_rollups import mark_viewed
from yt_playlist.web import theme


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    @router.get("/trends/story/{month}")
    def trends_story(request: Request, month: str):
        """The reel for one month. Serves the newest baked story; a mismatched month 404s (past months
        become available once the archive/recompute path lands -- see the design doc)."""
        story = (store.rec_proposals.get_proposals("trend_rollups") or {}).get("story")
        if not story or story.get("month") != month:
            raise HTTPException(status_code=404, detail="no recap for that month")
        grouped = any('groups' in beat for beat in story['beats'])
        story = {**story, 'beats': slides(story['beats'])}
        if grouped and ctx.rec_worker:
            ctx.rec_worker.trigger_trends_refresh()
        # The personality/closing beats' aura hue is a presentation token: recap_story stays
        # detached from web/theme and only bakes in the neutral genre family (top_family, on the
        # personality beat the closing beat's aura is copied from); fill the colour in here.
        personality_beat = next((b for b in story["beats"] if b["kind"] == "personality"), None)
        if personality_beat is not None:
            hue = theme.family_hue(personality_beat.get("top_family"))
            story = {**story, "beats": [
                {**b, "aura": {**b["aura"], "hue": hue}} if b["kind"] in ("personality", "closing") else b
                for b in story["beats"]
            ]}
        # Refresh the copy from baked counts so cached recaps also get accurate wording immediately.
        numbers = next((b for b in story["beats"] if b["kind"] == "numbers"), None)
        if numbers:
            thesis = listening_summary(numbers["plays"], numbers["listen_days"])
            story = {**story, "beats": [
                {**b, "thesis": thesis} if b["kind"] == "cover" else b
                for b in story["beats"]
            ]}
        response = templates.TemplateResponse(request, "story_reel.html", {"story": story})
        if not getattr(store, 'activity_paused', False):
            mark_viewed(store, month)
        return response

    return router
