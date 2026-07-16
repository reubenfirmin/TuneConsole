"""The monthly recap story ("Your Month", #79).

There is NO Trends tab: the recap is not a browsable section, it is a once-a-month event surfaced by a
nag card on Home (see _partials/alerts.html) that links straight into the full-screen reel here. This
route only renders that reel from the baked `story` payload the rec worker materialised
(rec/recap_story.py) -- no computation here.
"""
from fastapi import APIRouter, HTTPException, Request


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    @router.get("/trends/story/{month}")
    def trends_story(request: Request, month: str):
        """The reel for one month. Serves the newest baked story; a mismatched month 404s (past months
        become available once the archive/recompute path lands -- see the design doc)."""
        story = (store.get_proposals("trend_rollups") or {}).get("story")
        if not story or story.get("month") != month:
            raise HTTPException(status_code=404, detail="no recap for that month")
        return templates.TemplateResponse(request, "story_reel.html", {"story": story})

    return router
