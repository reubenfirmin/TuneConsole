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
import hashlib
import json
import random
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from yt_playlist.library import playlist_generated, playlist_navigation
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.rec import measurement, road_trip as road_trip_rec
from yt_playlist.rec import road_trip_candidates as candidates
from yt_playlist.rec.road_trip_axes import canonical_genre
from yt_playlist.util.matching import identity_key
from yt_playlist.core.activity import activity_for
from yt_playlist.core.accounts import client_for
from yt_playlist.web.context import request_form
from yt_playlist.web.road_trip_drafts import RoadTripDrafts

MIN_TARGET_MINUTES = 15
MAX_TARGET_MINUTES = 12 * 60


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
        entry = drafts.view(recipe_id)
        if entry is None:
            return None
        state, rid = entry['state'], entry['recipe_id']
        tracks = road_trip_rec.draft_tracks(state)
        # Progress polls of an unchanged draft are one offer, rather than new opportunities.
        fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]
        request_id = f"road-trip:{rid}:{fingerprint}"
        with store._lock:
            already_shown = store.conn.execute("SELECT 1 FROM rec_served_impressions WHERE request_id=? LIMIT 1",
                                                (request_id,)).fetchone()
        if not already_shown:
            measurement.record_recommendations(store, "road_trip", [
                {**track, "key": store.tracks.identity_key_for_video(track["video_id"]) or
                                identity_key(track["title"], track["artist"]),
                 "out_of_corpus": track.get("source") == "theirs"} for track in tracks],
                now_fn(), lane="draft", ranker="road-trip-mix", request_id=request_id,
                provenance={"recipe_id": rid, "seed": state.get("seed"),
                            "own_pct": state.get("own_pct"),
                            "familiarity_pct": state.get("familiarity_pct"),
                            "targets": state.get("targets"), "blacklist_genres": state.get("blacklist_genres")})
        return {"state": state, "recipe_id": rid, "tracks": tracks}

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
        previous = store.road_trip.get_road_trip_recipe(recipe_id) if recipe_id is not None else None
        previous_artists = {a.casefold() for a in (previous or {}).get("artists", [])}
        known_genres = {canonical_genre(g) for g in genres}
        for artist in artists:
            if artist.casefold() in previous_artists:
                continue
            genre = canonical_genre(candidates.artist_genre(store, artist))
            if genre and genre not in known_genres:
                genres.append(genre)
                known_genres.add(genre)

        def save():
            return store.road_trip.save_road_trip_recipe(
                recipe_id, name, own_pct, artists, genres, target_minutes, now_fn(),
                familiarity_pct=familiarity_pct,
                blacklist_genres=_clean_list(form.get("blacklist_genres")))

        _, client = _client()
        recipe_id = drafts.edit_recipe(save, client)
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

    def _mutate(request, recipe_id, change):
        """Apply a change to the stored draft and re-render. `change(state)` returns the state.

        Most changes are pure re-picks. One isn't: dragging a slider past what the pool holds queues
        YouTube searches to deepen it (rec.set_share), which the background worker then runs - so if
        anything ends up pending, kick it off and let the panel poll, exactly as a build does."""
        _, client = _client()
        state = drafts.mutate(recipe_id, change, client)
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
        return _mutate(request, recipe_id,
                       lambda s: road_trip_rec.set_share(s, party, axis, share, store,
                                                        now_fn()))

    @router.post("/road_trip/draft/{recipe_id}/unpin")
    def unpin_draft(request: Request, recipe_id: int, form=Depends(request_form)):
        """Reset this genre or year group to follow the available tracks again."""
        party, axis = form.get("party") or "", form.get("axis") or ""
        return _mutate(request, recipe_id,
                       lambda s: road_trip_rec.clear_share(s, party, axis, store, now_fn()))

    @router.post("/road_trip/draft/{recipe_id}/mix")
    def mix_draft(request: Request, recipe_id: int, form=Depends(request_form)):
        """The mine/theirs or favorites/deep-cuts slider moved: re-pick the whole playlist. The
        recipe on file is left alone - this steers the mix in front of you, not the saved recipe."""
        def change(state):
            # The mix bar posts THEIR share (its left end is labelled "Mine", so left must mean more
            # of yours); own_pct is still accepted for anything posting the other way round.
            if form.get("their_pct") is not None:
                state["own_pct"] = 100 - _pct(form.get("their_pct"), 100 - state["own_pct"])
            elif form.get("own_pct") is not None:
                state["own_pct"] = _pct(form.get("own_pct"), state["own_pct"])
            if form.get("familiarity_pct") is not None:
                state["familiarity_pct"] = _pct(form.get("familiarity_pct"),
                                                state["familiarity_pct"])
            return road_trip_rec.repick(state, store, now_fn())

        return _mutate(request, recipe_id, change)

    @router.post("/road_trip/draft/{recipe_id}/slot/{index}")
    def reroll_slot(request: Request, recipe_id: int, index: int):
        """Cross out one slot; the recipe fills it back in with something else of the same side."""
        return _mutate(request, recipe_id,
                       lambda s: road_trip_rec.reroll_slot(s, store, index, now_fn()))

    @router.post("/road_trip/draft/{recipe_id}/shuffle")
    def shuffle_draft(request: Request, recipe_id: int):
        """A different draw from the same pool: no network, new seed."""
        def change(state):
            state["seed"] = random.randrange(1 << 30)
            return road_trip_rec.repick(state, store, now_fn())

        return _mutate(request, recipe_id, change)

    @router.delete("/road_trip/draft/{recipe_id}")
    def discard_draft(request: Request, recipe_id: int):
        drafts.delete(recipe_id)
        return _body(request)

    @router.post("/road_trip/draft/{recipe_id}/save")
    async def save_draft(request: Request, recipe_id: int):
        """Materialize exactly what's on screen as a YouTube playlist (Generated group: quarantined
        from the taste model and GC'd on the normal schedule), using the shared save/play feedback."""
        return await activity_for(ctx).to_thread(_save_draft, request, recipe_id)

    def _save_draft(request, recipe_id):
        # Reads, normalization, remote mutation, bookkeeping and rendering all
        # belong to the admitted worker, including after request cancellation.
        recipe, state = drafts.for_save(recipe_id)
        result = {"name": f"Road Trip: {recipe['name']}" if recipe else "Road Trip"}

        def response(error=None):
            if error:
                result["error"] = error
            return templates.TemplateResponse(request, "_partials/generated_result.html", result)

        if recipe is None or state is None:
            return response("That playlist is gone - build it again.")
        identity_id, client = _client()
        tracks = road_trip_rec.draft_tracks(state)
        if client is None or not tracks:
            return response("Couldn't save it - connect the main account and keep at least one track.")
        stats = state["stats"]
        try:
            saved = playlist_generated.create_generated_playlist(store, result["name"], tracks,
                client, now_fn(), identity_id,
                recipe={"model": "road_trip", "road_trip_recipe_id": recipe_id, "journey": "road_trip",
                        "own_pct": state["own_pct"], "familiarity_pct": state["familiarity_pct"],
                        "target_minutes": state["target_minutes"],
                        "achieved_minutes": stats.get("minutes"), "own_count": stats.get("own_count"),
                        "their_count": stats.get("their_count")})
            drafts.saved(recipe_id, state, saved["new_ytm"])
            result.update(ytm=saved["new_ytm"], pid=saved["pid"], added=saved["added"])
            if saved["added"]:
                measurement.record_action(store, "save", now_fn(), playlist_id=saved["pid"],
                                          event_id=f"generate:{saved['new_ytm']}", generated=True)
            bridge = getattr(ctx, "bridge", None)
            if bridge is not None and getattr(bridge, "connected", False):
                watch_url = f"https://music.youtube.com/watch?list={saved['new_ytm']}"
                activity_for(ctx).start_thread(playlist_navigation.navigate_when_ready,
                                 args=(client, bridge, saved["new_ytm"], watch_url))
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
