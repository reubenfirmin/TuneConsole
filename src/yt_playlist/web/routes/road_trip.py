"""Road Trip tab: saved recipes blending your taste-weighted tracks with popular tracks pulled from
YouTube for other people's artists/genres.

Building a recipe produces an on-screen DRAFT, not a YouTube playlist: the expensive pool assembly
runs once (/build), and every control after it - the mine/theirs mix, the familiarity lean, the
per-party genre and era sliders, crossing a slot out - re-picks from the stored pool with no network
(rec/road_trip.py). Only "Save to YouTube" materializes it, the same way every other generated
playlist is made (Generated group, GC, taste-model quarantine) via executor.create_generated_playlist.

Every endpoint here re-renders the whole page body (draft + recipe list) into #road-trip-body. The
two are coupled - saving a draft updates its recipe's "last generated" link, deleting a recipe drops
its draft - so one swap keeps them consistent without out-of-band trickery.
"""
import asyncio
import json
import random
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from yt_playlist.library import executor
from yt_playlist.rec import road_trip as road_trip_rec

MIN_TARGET_MINUTES = 15
MAX_TARGET_MINUTES = 12 * 60


def _spawn(fn):
    """Run a build worker off the request thread. Module-level so tests can run it inline."""
    threading.Thread(target=fn, daemon=True).start()


# Recipe ids with a build worker alive in THIS process, and the lock that keeps two workers (a
# double-clicked Build) off the same draft. A draft that says it is building but isn't in here has
# been orphaned - the process restarted, or the worker died - and is closed out on the next render
# rather than polling forever.
_BUILDING: set = set()
_BUILD_LOCK = threading.Lock()
_WORKER_LOCKS: dict = {}


def _draft_worker_lock(recipe_id):
    """The per-recipe lock a build worker holds while it walks that draft's pending inputs."""
    with _BUILD_LOCK:
        return _WORKER_LOCKS.setdefault(recipe_id, threading.Lock())


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

    def _draft_ctx(recipe_id=None):
        """The draft to show: a named recipe's, else whichever was touched most recently."""
        entry = (None if recipe_id is None
                 else {"recipe_id": recipe_id, "state": store.get_road_trip_draft(recipe_id)})
        if entry is None or entry["state"] is None:
            entry = store.latest_road_trip_draft()
        if not entry or not entry.get("state"):
            return None
        # A draft persisted by an older build must not crash the page it reopens on.
        state, rid = road_trip_rec.normalized(entry["state"]), entry["recipe_id"]
        if state.get("building") and rid not in _BUILDING:      # orphaned build: settle it
            state = road_trip_rec.finish_draft(state, store, now_fn())
            store.save_road_trip_draft(rid, state, now_fn())
        return {"state": state, "recipe_id": rid,
                "tracks": road_trip_rec.draft_tracks(state)}

    BLANK_RECIPE = {"id": None, "name": "", "own_pct": 50, "familiarity_pct": 50,
                    "target_minutes": 60, "artists": [], "genres": [], "blacklist_genres": []}

    def _body(request, recipe_id=None, error=None, blank=False):
        """The whole page body. `recipe_id` picks which recipe the editor is loaded with (and whose
        draft is shown); `blank` empties the editor for a new one."""
        draft = _draft_ctx(recipe_id)
        loaded = recipe_id if recipe_id is not None else (draft or {}).get("recipe_id")
        form_recipe = None if blank or loaded is None else store.get_road_trip_recipe(loaded)
        return templates.TemplateResponse(request, "_partials/road_trip_body.html",
                                          {"recipes": store.list_road_trip_recipes(),
                                           "draft": draft, "error": error,
                                           "form_recipe": form_recipe or BLANK_RECIPE})

    def _client():
        return next(iter((ctx.client_provider() or {}).items()), (None, None))

    @router.get("/road_trip")
    def road_trip_page(request: Request):
        draft = _draft_ctx()
        loaded = (draft or {}).get("recipe_id")
        return templates.TemplateResponse(
            request, "road_trip.html",
            {"recipes": store.list_road_trip_recipes(), "draft": draft,
             "form_recipe": (store.get_road_trip_recipe(loaded) if loaded is not None else None)
                            or BLANK_RECIPE})

    @router.post("/road_trip/recipes/{recipe_id}/edit")
    def edit_recipe(request: Request, recipe_id: int):
        """Load a saved recipe into the editor (and show its draft, if it has one)."""
        return _body(request, recipe_id)

    @router.post("/road_trip/new")
    def new_road_trip(request: Request):
        """Clear the editor without mixing route creation into the live tuning controls."""
        return templates.TemplateResponse(request, "_partials/road_trip_body.html",
                                          {"recipes": store.list_road_trip_recipes(),
                                           "draft": None, "error": None,
                                           "form_recipe": BLANK_RECIPE})

    @router.post("/road_trip/recipes")
    async def save_recipe(request: Request):
        form = await request.form()
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
        recipe_id = store.save_road_trip_recipe(
            recipe_id, name, own_pct, _clean_list(form.get("artists")),
            _clean_list(form.get("genres")), target_minutes, now_fn(),
            familiarity_pct=familiarity_pct,
            blacklist_genres=_clean_list(form.get("blacklist_genres")))
        # Editing the recipe a draft is already showing steers THAT draft rather than starting over:
        # tracks for a removed artist leave at once, an added one streams in, the rest is a re-pick.
        state = store.get_road_trip_draft(recipe_id)
        if state is not None:
            recipe = store.get_road_trip_recipe(recipe_id)
            state = road_trip_rec.apply_recipe(road_trip_rec.normalized(state), store, now_fn(),
                                               recipe)
            store.save_road_trip_draft(recipe_id, state, now_fn())
            _, client = _client()
            if state["pending"] and client is not None:
                with _BUILD_LOCK:
                    _BUILDING.add(recipe_id)
                _spawn(lambda: _fill_in_their_half(recipe_id, client))
        return _body(request, recipe_id)

    @router.delete("/road_trip/recipes/{recipe_id}")
    def delete_recipe(request: Request, recipe_id: int):
        store.delete_road_trip_recipe(recipe_id)
        return _body(request)

    @router.post("/road_trip/recipes/{recipe_id}/build")
    def build_recipe(request: Request, recipe_id: int):
        """Start a build and return the page at once, with YOUR half of the playlist already in it.
        Their half needs a YouTube page and a Deezer lookup per track, so it arrives in the
        background, one input at a time; the draft panel polls /progress until it settles.

        Seeded randomly and told what the last build used, so running the same recipe again gives a
        different playlist."""
        recipe = store.get_road_trip_recipe(recipe_id)
        if recipe is None:
            return JSONResponse({"error": "recipe not found"}, status_code=404)
        _, client = _client()
        if client is None:
            return _body(request, recipe_id, error="Connect an account to build a playlist.")
        previous = (store.get_road_trip_draft(recipe_id) or {}).get("picks") or []
        state = road_trip_rec.start_draft(store, recipe, now_fn(), random.randrange(1 << 30),
                                          previous)
        store.save_road_trip_draft(recipe_id, state, now_fn())
        if state["building"]:
            with _BUILD_LOCK:            # registered here, not in the worker: a poll can land first
                _BUILDING.add(recipe_id)
            _spawn(lambda: _fill_in_their_half(recipe_id, client))
        elif not state["picks"]:
            return _body(request, recipe_id,
                         error=f"Couldn't build \"{recipe['name']}\" - no tracks found.")
        return _body(request, recipe_id)

    def _fill_in_their_half(recipe_id, client):
        """Background worker: walk the draft's pending inputs, saving after each so the page picks
        the new tracks up on its next poll. Owns the draft while it runs (the panel's controls are
        inert until it finishes), so there is no last-write-wins race with the user. Serialized per
        recipe, so a double-clicked Build doesn't put two workers on one draft."""
        error = None
        try:
            with _draft_worker_lock(recipe_id):
                while True:
                    state = store.get_road_trip_draft(recipe_id)
                    if state is None or not state.get("pending"):
                        break
                    road_trip_rec.add_other_input(state, store, client, state["pending"].pop(0))
                    store.save_road_trip_draft(recipe_id, state, now_fn())
                # Their side is in. Now tag YOUR untagged tracks so your half gets sliders too -
                # last, because the playlist is already usable without it.
                state = store.get_road_trip_draft(recipe_id)
                if state is not None and state.get("own_facts_left"):
                    state["phase"] = "mine"
                    store.save_road_trip_draft(recipe_id, state, now_fn())
                    road_trip_rec.fill_own_facts(state, store, now_fn())
                    store.save_road_trip_draft(recipe_id, state, now_fn())
        except Exception as e:  # noqa: BLE001 - a failed lookup must not leave the draft stuck
            error = str(e) or type(e).__name__
            raise
        finally:
            state = store.get_road_trip_draft(recipe_id)
            if state is not None:
                if error:
                    state["build_error"] = error
                store.save_road_trip_draft(
                    recipe_id, road_trip_rec.finish_draft(state, store, now_fn()), now_fn())
            with _BUILD_LOCK:
                _BUILDING.discard(recipe_id)

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
        state = store.get_road_trip_draft(recipe_id)
        if state is None:
            return _body(request, error="That playlist is gone - build it again.")
        state = change(road_trip_rec.normalized(state))
        store.save_road_trip_draft(recipe_id, state, now_fn())
        _, client = _client()
        if state.get("pending") and client is not None:
            with _BUILD_LOCK:
                _BUILDING.add(recipe_id)
            _spawn(lambda: _fill_in_their_half(recipe_id, client))
        return _body(request, recipe_id)

    @router.post("/road_trip/draft/{recipe_id}/tilt")
    async def tilt_draft(request: Request, recipe_id: int):
        """A genre or era slider moved. The value posted is the SHARE of that party's tracks the
        genre should have (0-100), which is also where the slider sits, so what you drag and what
        you see are the same quantity."""
        form = await request.form()
        party, axis = form.get("party") or "", form.get("axis") or ""
        try:
            share = float(form.get("share")) / 100.0
        except (TypeError, ValueError):
            return _body(request, recipe_id)
        return _mutate(request, recipe_id,
                       lambda s: road_trip_rec.set_share(s, party, axis, share, store,
                                                        now_fn()))

    @router.post("/road_trip/draft/{recipe_id}/unpin")
    async def unpin_draft(request: Request, recipe_id: int):
        """Release a pinned slider: that genre floats with the rest of the mix again."""
        form = await request.form()
        party, axis = form.get("party") or "", form.get("axis") or ""
        return _mutate(request, recipe_id,
                       lambda s: road_trip_rec.clear_share(s, party, axis, store, now_fn()))

    @router.post("/road_trip/draft/{recipe_id}/mix")
    async def mix_draft(request: Request, recipe_id: int):
        """The mine/theirs or favorites/deep-cuts slider moved: re-pick the whole playlist. The
        recipe on file is left alone - this steers the mix in front of you, not the saved recipe."""
        form = await request.form()

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
        store.delete_road_trip_draft(recipe_id)
        return _body(request)

    @router.post("/road_trip/draft/{recipe_id}/save")
    async def save_draft(request: Request, recipe_id: int):
        """Materialize exactly what's on screen as a YouTube playlist (Generated group: quarantined
        from the taste model and GC'd on the normal schedule)."""
        recipe = store.get_road_trip_recipe(recipe_id)
        state = store.get_road_trip_draft(recipe_id)
        if recipe is None or state is None:
            return _body(request, error="That playlist is gone - build it again.")
        identity_id, client = _client()
        tracks = road_trip_rec.draft_tracks(state)
        if client is None or not tracks:
            return _body(request, recipe_id,
                         error="Couldn't save it - connect an account and keep at least one track.")
        stats = state["stats"]
        result = await asyncio.to_thread(
            executor.create_generated_playlist, store, f"Road Trip: {recipe['name']}", tracks,
            client, now_fn(), identity_id,
            recipe={"model": "road_trip", "road_trip_recipe_id": recipe_id, "journey": "road_trip",
                    "own_pct": state["own_pct"], "familiarity_pct": state["familiarity_pct"],
                    "target_minutes": state["target_minutes"],
                    "achieved_minutes": stats.get("minutes"), "own_count": stats.get("own_count"),
                    "their_count": stats.get("their_count")})
        store.set_road_trip_last_playlist(recipe_id, result["new_ytm"])
        state["saved_playlist_id"] = result["new_ytm"]
        store.save_road_trip_draft(recipe_id, state, now_fn())
        return _body(request, recipe_id)

    @router.get("/road_trip/artist_genre")
    def artist_genre(name: str = ""):
        """The genre an artist plays, for the form to pre-fill "their genres" when one is added.

        Adding an artist almost always means "and more like this", and their genre is the handle for
        that - it pulls the genre's other top artists into the pool. Offering it as a removable chip
        beats making the user name it themselves. One memoized Last.fm call; silently empty without
        an API key, in which case the form just adds the artist."""
        name = name.strip()
        return JSONResponse({"genre": (road_trip_rec.artist_genre(store, name) or "") if name
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
