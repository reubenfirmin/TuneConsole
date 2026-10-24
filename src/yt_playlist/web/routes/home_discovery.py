"""Prepared discovery/story reads and explicit visible-story measurements."""
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from yt_playlist.rec import home_prepared, into_recently
from yt_playlist.rec.home_cards import _epoch


class StoryImpressions(BaseModel):
    subjects: list[str] = Field(max_length=into_recently.MAX_STORIES)


def build(ctx, views):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.get("/home/into-recently")
    def home_into_recently(request: Request):
        """Read worker-prepared stories; the request only rotates the already decorated pool."""
        now = now_fn()
        schedule = ctx.rec_worker.trigger_story_refresh if ctx.rec_worker else lambda: None
        cards, preparing = into_recently.load(store, now, epoch=_epoch(store, "into_recently"), schedule=schedule)
        if cards or preparing:
            groups = into_recently.story_groups(cards)
            return templates.TemplateResponse(request, "_partials/into_recently.html",
                                              {"cards": cards, "groups": groups,
                                               "spreads": into_recently.editorial_spreads(groups),
                                               "preparing": preparing})
        return Response(status_code=200)

    @router.post("/home/into-recently/impressions", status_code=204)
    def story_impressions(visible: StoryImpressions):
        payload = store.rec_proposals.get_proposals(into_recently.SURFACE) or {}
        known = {card['subject'] for card in payload.get('cards', [])}
        store.rec_ledger.record_story_views(known.intersection(visible.subjects), now_fn())
        return Response(status_code=204)

    @router.get("/home/discovery")
    def home_discovery(request: Request):
        return templates.TemplateResponse(request, "_partials/home_discovery.html",
                                          views.discovery_context(now_fn()))

    @router.get("/home/discover")
    def home_discover(request: Request):
        # From the accumulating discovery pool: recency-biased, repeat-aware, filled in by the
        # background scan of every artist you're interested in (not a top-10 overwrite).
        now = now_fn()
        albums = home_prepared.discovery(store, views.prepared(now), now)["discovery_albums"]
        return templates.TemplateResponse(request, "_partials/discover.html",
                                          {"albums": albums,
                                           "building": not albums and views.busy(),
                                           "stale": bool(albums) and views.busy()})

    @router.get("/home/new-artists")
    def home_new_artists(request: Request):
        # From the accumulating new-artist pool: best taste-fit first, de-prioritizing recently-shown.
        now = now_fn()
        artists = home_prepared.discovery(store, views.prepared(now), now)["discovery_artists"]
        views.queue_artist_images({'discovery_artists': artists}, now)
        return templates.TemplateResponse(request, "_partials/new_artists.html",
                                          {"artists": artists,
                                           "building": not artists and views.busy(),
                                           "stale": bool(artists) and views.busy()})

    return router

