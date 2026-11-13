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

from yt_playlist.library import playlist_generated, playlist_navigation
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.rec import road_trip as road_trip_rec
from yt_playlist.rec import road_trip_candidates as candidates
from yt_playlist.core.activity import activity_for
from yt_playlist.core.accounts import client_for
from yt_playlist.core.road_trip_contract import MIN_TARGET_MINUTES, MAX_TARGET_MINUTES
from yt_playlist.web.context import request_form
from yt_playlist.web.road_trip_drafts import RoadTripDrafts


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
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn
    drafts = RoadTripDrafts(ctx, lambda run: _spawn(run))

    def _draft_ctx(recipe_id=None):
        """The draft to show: a named recipe's, else whichever was touched most recently."""
        entry = drafts.view_with_measurement(recipe_id)
        if entry is None:
            return None
        state, rid, tracks = entry['state'], entry['recipe_id'], entry['tracks']
        # Keep acquisition diagnostics in the durable draft and logs, outside
        # the template context. The screen only needs the incomplete outcome.
        return {"state": {**state, 'build_error': bool(state.get('build_error'))},
                "recipe_id": rid, "tracks": tracks}

    BLANK_RECIPE = {"id": None, "name": "", "own_pct": 50, "familiarity_pct": 50,
                    "target_minutes": 60, "artists": [], "genres": [], "blacklist_genres": []}

    def _body(request, recipe_id=None, error=None, blank=False):
        """The whole page body. `recipe_id` picks which recipe the editor is loaded with (and whose
        draft is shown); `blank` empties the editor for a new one."""
        draft = _draft_ctx(recipe_id)
        loaded = recipe_id if recipe_id is not None else (draft or {}).get("recipe_id")
        form_recipe = None if blank or loaded is None else store.road_trip.get_road_trip_recipe(loaded)
        return templates.TemplateResponse(request, "_partials/road_trip_body.html",
                                          {"recipes": store.road_trip.list_road_trip_recipes(),
                                           "draft": draft, "error": error,
                                           "form_recipe": form_recipe or BLANK_RECIPE})

    def _client():
        try:
            return client_for(store,ctx.client_provider())
        except ValueError:
            return None,None

    @router.get("/road_trip")
    def road_trip_page(request: Request):
        draft = _draft_ctx()
        loaded = (draft or {}).get("recipe_id")
        return templates.TemplateResponse(
            request, "road_trip.html",
            {"recipes": store.road_trip.list_road_trip_recipes(), "draft": draft,
             "form_recipe": (store.road_trip.get_road_trip_recipe(loaded) if loaded is not None else None)
                            or BLANK_RECIPE})

    @router.post("/road_trip/recipes/{recipe_id}/edit")
    def edit_recipe(request: Request, recipe_id: int):
        """Load a saved recipe into the editor (and show its draft, if it has one)."""
        return _body(request, recipe_id)

    @router.post("/road_trip/new")
    def new_road_trip(request: Request):
        """Clear the editor without mixing route creation into the live tuning controls."""
        return templates.TemplateResponse(request, "_partials/road_trip_body.html",
                                          {"recipes": store.road_trip.list_road_trip_recipes(),
                                           "draft": None, "error": None,
                                           "form_recipe": BLANK_RECIPE})

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
        _, client = _client()
        recipe_id = drafts.save_recipe(recipe_id, name, own_pct, artists, genres, target_minutes,
            familiarity_pct=familiarity_pct, blacklist_genres=_clean_list(form.get("blacklist_genres")),
            client=client)
        return _body(request, recipe_id)

    @router.delete("/road_trip/recipes/{recipe_id}")
    def delete_recipe(request: Request, recipe_id: int):
        drafts.delete(recipe_id, recipe=True)
        return _body(request)

    @router.post("/road_trip/recipes/{recipe_id}/build")
    def build_recipe(request: Request, recipe_id: int):
        """Start a build and return the page at once, with YOUR half of the playlist already in it.
        Their half needs a YouTube page and a Deezer lookup per track, so it arrives in the
        background, one input at a time; the draft panel polls /progress until it settles.

        Seeded randomly and told what the last build used, so running the same recipe again gives a
        different playlist."""
        recipe = store.road_trip.get_road_trip_recipe(recipe_id)
        if recipe is None:
            return JSONResponse({"error": "recipe not found"}, status_code=404)
        _, client = _client()
        if client is None:
            return _body(request, recipe_id, error="Connect an account to build a playlist.")
        state = drafts.build(recipe_id, client)
        if state is None:
            return JSONResponse({"error": "recipe not found"}, status_code=404)
        if not state["building"] and not state["picks"]:
            return _body(request, recipe_id,
                         error=f"Couldn't build \"{recipe['name']}\" - no tracks found.")
        return _body(request, recipe_id)

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
        _, client = _client()
        return _rendered(request, recipe_id, drafts.tilt(recipe_id, party, axis, share, client))

    @router.post("/road_trip/draft/{recipe_id}/unpin")
    def unpin_draft(request: Request, recipe_id: int, form=Depends(request_form)):
        """Reset this genre or year group to follow the available tracks again."""
        party, axis = form.get("party") or "", form.get("axis") or ""
        _, client = _client()
        return _rendered(request, recipe_id, drafts.unpin(recipe_id, party, axis, client))

    @router.post("/road_trip/draft/{recipe_id}/slot/{index}")
    def reroll_slot(request: Request, recipe_id: int, index: int):
        """Cross out one slot; the recipe fills it back in with something else of the same side."""
        _, client = _client()
        return _rendered(request, recipe_id, drafts.reroll(recipe_id, index, client))

    @router.post("/road_trip/draft/{recipe_id}/shuffle")
    def shuffle_draft(request: Request, recipe_id: int):
        """A different draw from the same pool: no network, new seed."""
        _, client = _client()
        return _rendered(request, recipe_id, drafts.shuffle(recipe_id, client))

    @router.delete("/road_trip/draft/{recipe_id}")
    def discard_draft(request: Request, recipe_id: int):
        drafts.delete(recipe_id)
        return _body(request)

    @router.post("/road_trip/draft/{recipe_id}/save")
    async def save_draft(request: Request, recipe_id: int, form=Depends(request_form)):
        """Materialize exactly what's on screen as a YouTube playlist (Generated group: quarantined
        from the taste model and GC'd on the normal schedule), using the shared save/play feedback."""
        bridge = getattr(ctx, "bridge", None)
        intent = bridge.begin_playback_intent() if bridge is not None else None
        return await activity_for(ctx).to_thread(_save_draft, request, recipe_id, intent, form.get("launch_id", ""))

    def _save_draft(request, recipe_id, intent, launch_id):
        # Reads, normalization, remote mutation, bookkeeping and rendering all
        # belong to the admitted worker, including after request cancellation.
        recipe, state = drafts.for_save(recipe_id)
        result = {"name": f"Road Trip: {recipe['name']}" if recipe else "Road Trip", "launch_id": launch_id}

        def response(error=None):
            if error:
                result["error"] = error
            return templates.TemplateResponse(request, "_partials/generated_result.html", result)

        if recipe is None or state is None:
            return response("That playlist is gone - build it again.")
        account_id, client = _client()
        tracks = road_trip_rec.draft_tracks(state)
        if client is None or not tracks:
            return response("Couldn't save it - connect the main account and keep at least one track.")
        stats = state["stats"]
        try:
            saved = playlist_generated.create_and_record(store, result["name"], tracks,
                client, now_fn(), account_id,
                recipe={"model": "road_trip", "road_trip_recipe_id": recipe_id, "journey": "road_trip",
                        "own_pct": state["own_pct"], "familiarity_pct": state["familiarity_pct"],
                        "target_minutes": state["target_minutes"],
                        "achieved_minutes": stats.get("minutes"), "own_count": stats.get("own_count"),
                        "their_count": stats.get("their_count")})
            drafts.saved(recipe_id, state, saved["new_ytm"])
            result.update(ytm=saved["new_ytm"], pid=saved["pid"], added=saved["added"])
            bridge = getattr(ctx, "bridge", None)
            if bridge is not None and getattr(bridge, "connected", False):
                watch_url = f"https://music.youtube.com/watch?list={saved['new_ytm']}"
                activity_for(ctx).start_thread(playlist_navigation.navigate_when_ready,
                                 args=(client, bridge, saved["new_ytm"], watch_url), kwargs={"intent": intent})
        except PlaylistAddUncertain as exc:
            result["partial_ytm"] = exc.playlist_id
            return response(str(exc))
        except Exception:  # noqa: BLE001 - close the loading screen and leave the draft available
            ctx.logger.exception("save road trip %s failed", recipe_id)
            return response("YouTube returned an unexpected response.")
        return response()

    @router.get("/road_trip/artist_genre")
    def artist_genre(name: str = ""):
        """The genre an artist plays, for the form to pre-fill "their genres" when one is added.

        Adding an artist almost always means "and more like this", and their genre is the handle for
        that - it pulls the genre's other top artists into the pool. Offering it as a removable chip
        beats making the user name it themselves. One memoized Last.fm call; silently empty without
        an API key, in which case the form just adds the artist."""
        name = name.strip()
        return JSONResponse({"genre": (candidates.artist_genre(store, name) or "") if name
                             else ""})

    @router.get("/road_trip/autocomplete/artists")
    def autocomplete_artists(q: str = ""):
        q = q.strip()
        if not q:
            return JSONResponse({"results": []})
        _, client = _client()
        if client is None:
            return JSONResponse({"results": []})
        try:
            results = client.search(q, filter="artists") or []
        except Exception:  # noqa: BLE001 - a flaky search must never break the form
            results = []
        names = []
        for r in results[:8]:
            name = r.get("artist") or r.get("title")
            if name and name not in names:
                names.append(name)
        return JSONResponse({"results": names})

    return router
