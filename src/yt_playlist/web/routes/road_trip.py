"""Road Trip tab: saved recipes blending your taste-weighted tracks with popular tracks pulled from
YouTube for other people's artists/genres.

Building a recipe produces an on-screen DRAFT, not a YouTube playlist: the expensive pool assembly
runs once (/build), and every control after it - the mine/theirs mix, the familiarity lean, the
per-party genre and era sliders, crossing a slot out - re-picks from the stored pool with no network
(rec/road_trip.py). Only "Save to YouTube" materializes it, the same way every other generated
playlist is made (Generated group, GC, taste-model quarantine) via playlist_generated.create_generated_playlist.

Editor endpoints re-render the whole page body (draft + recipe list) into #road-trip-body. The
two are coupled - saving a draft updates its recipe's "last generated" link, deleting a recipe drops
its draft - so one swap keeps them consistent. Saving to YouTube uses the standard generated-playlist
result, which closes the loading screen and opens the saved playlist.
"""
import json
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from yt_playlist.core.activity import activity_for
from yt_playlist.core.road_trip_contract import MIN_TARGET_MINUTES, MAX_TARGET_MINUTES
from yt_playlist.web.context import Ctx, request_form
from yt_playlist.library.road_trip_composition import drafts as compose_drafts


def _spawn(fn):
    """Run a build worker off the request thread. Module-level so tests can run it inline."""
    threading.Thread(target=fn, daemon=True).start()


def _clean_list(raw):
    try:
        vals = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [v.strip() for v in vals if isinstance(v, str) and v.strip()]


def _pct(raw, default):
    try:
        return max(0, min(100, int(float(raw))))
    except (TypeError, ValueError):
        return default


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates
    drafts = compose_drafts(store, now=ctx.now_fn, work=activity_for(ctx), spawn=lambda run: _spawn(run))
    commands = Ctx.road_trip_commands(ctx, drafts)

    def _body(request, recipe_id=None, error=None, blank=False):
        """The whole page body. `recipe_id` picks which recipe the editor is loaded with (and whose
        draft is shown); `blank` empties the editor for a new one."""
        return templates.TemplateResponse(request, "_partials/road_trip_body.html",
                                          commands.page(recipe_id, error=error, blank=blank))

    @router.get("/road_trip")
    def road_trip_page(request: Request):
        return templates.TemplateResponse(
            request, "road_trip.html", commands.page())

    @router.post("/road_trip/recipes/{recipe_id}/edit")
    def edit_recipe(request: Request, recipe_id: int):
        """Load a saved recipe into the editor (and show its draft, if it has one)."""
        return _body(request, recipe_id)

    @router.post("/road_trip/new")
    def new_road_trip(request: Request):
        """Clear the editor without mixing route creation into the live tuning controls."""
        return _body(request, blank=True)

    @router.post("/road_trip/recipes")
    def save_recipe(request: Request, form=Depends(request_form)):
        try:
            recipe_id = int(form.get("id")) if form.get("id") else None
        except (TypeError, ValueError):
            recipe_id = None
        name = (form.get("name") or "").strip() or "Road Trip"
        own_pct = _pct(form.get("own_pct"), 50)
        familiarity_pct = _pct(form.get("familiarity_pct"), 50)
        try:
            target_minutes = max(MIN_TARGET_MINUTES,
                                 min(MAX_TARGET_MINUTES, int(form.get("target_minutes") or 60)))
        except (TypeError, ValueError):
            target_minutes = 60
        artists, genres = _clean_list(form.get("artists")), _clean_list(form.get("genres"))
        recipe_id = commands.save_recipe(recipe_id, name, own_pct, artists, genres, target_minutes,
            familiarity_pct=familiarity_pct, blacklist_genres=_clean_list(form.get("blacklist_genres")))
        return _body(request, recipe_id)

    @router.delete("/road_trip/recipes/{recipe_id}")
    def delete_recipe(request: Request, recipe_id: int):
        commands.delete(recipe_id, recipe=True)
        return _body(request)

    @router.post("/road_trip/recipes/{recipe_id}/build")
    def build_recipe(request: Request, recipe_id: int):
        """Start a build and return the page at once, with YOUR half of the playlist already in it.
        Their half needs a YouTube page and a Deezer lookup per track, so it arrives in the
        background, one input at a time; the draft panel polls /progress until it settles.

        Seeded randomly and told what the last build used, so running the same recipe again gives a
        different playlist."""
        result = commands.build(recipe_id)
        if result.get('missing'):
            return JSONResponse({"error": "recipe not found"}, status_code=404)
        return _body(request, recipe_id, error=result.get('error'))

    @router.get("/road_trip/draft/{recipe_id}/progress")
    def draft_progress(request: Request, recipe_id: int):
        """What the panel polls while a build is running. Plain re-render: the worker is writing the
        draft, this just shows where it got to."""
        return _body(request, recipe_id)

    def _rendered(request, recipe_id, state):
        """Most changes are pure re-picks. One isn't: dragging a slider past what the pool holds
        queues YouTube searches to deepen it (rec.set_share), which the background worker then runs -
        so if anything ends up pending, it's already kicked off and the panel polls, exactly as a
        build does."""
        if state is None:
            return _body(request, error="That playlist is gone - build it again.")
        return _body(request, recipe_id)

    @router.post("/road_trip/draft/{recipe_id}/tilt")
    @router.post("/road_trip/draft/{recipe_id}/tilt/v2")
    def tilt_draft(request: Request, recipe_id: int, form=Depends(request_form)):
        """Set the requested percentage and proportionally rebalance the rest of its slider group.
        The controls retain these targets while the worker finds matching tracks."""
        party, axis = form.get("party") or "", form.get("axis") or ""
        try:
            share = float(form.get("share")) / 100.0
        except (TypeError, ValueError):
            return _body(request, recipe_id)
        return _rendered(request, recipe_id, commands.tilt(recipe_id, party, axis, share))

    @router.post("/road_trip/draft/{recipe_id}/unpin")
    def unpin_draft(request: Request, recipe_id: int, form=Depends(request_form)):
        """Reset this genre or year group to follow the available tracks again."""
        party, axis = form.get("party") or "", form.get("axis") or ""
        return _rendered(request, recipe_id, commands.unpin(recipe_id, party, axis))

    @router.post("/road_trip/draft/{recipe_id}/slot/{index}")
    def reroll_slot(request: Request, recipe_id: int, index: int):
        """Cross out one slot; the recipe fills it back in with something else of the same side."""
        return _rendered(request, recipe_id, commands.reroll(recipe_id, index))

    @router.post("/road_trip/draft/{recipe_id}/shuffle")
    def shuffle_draft(request: Request, recipe_id: int):
        """A different draw from the same pool: no network, new seed."""
        return _rendered(request, recipe_id, commands.shuffle(recipe_id))

    @router.delete("/road_trip/draft/{recipe_id}")
    def discard_draft(request: Request, recipe_id: int):
        commands.delete(recipe_id)
        return _body(request)

    @router.post("/road_trip/draft/{recipe_id}/save")
    async def save_draft(request: Request, recipe_id: int, form=Depends(request_form)):
        """Materialize exactly what's on screen as a YouTube playlist (Generated group: quarantined
        from the taste model and GC'd on the normal schedule), using the shared save/play feedback."""
        intent = commands.admit_save()
        result = await activity_for(ctx).to_thread(commands.save, recipe_id, intent, form.get('launch_id', ''))
        return templates.TemplateResponse(request, '_partials/generated_result.html', result)

    @router.get("/road_trip/artist_genre")
    def artist_genre(name: str = ""):
        """The genre an artist plays, for the form to pre-fill "their genres" when one is added.

        Adding an artist almost always means "and more like this", and their genre is the handle for
        that - it pulls the genre's other top artists into the pool. Offering it as a removable chip
        beats making the user name it themselves. One memoized Last.fm call; silently empty without
        an API key, in which case the form just adds the artist."""
        name = name.strip()
        return JSONResponse({"genre": (commands.artist_genre(name) or "") if name
                             else ""})

    @router.get("/road_trip/autocomplete/artists")
    def autocomplete_artists(q: str = ""):
        return JSONResponse({'results': commands.autocomplete(q.strip())})

    return router
