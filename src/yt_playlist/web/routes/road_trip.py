"""Road Trip tab: saved recipes blending your taste-weighted tracks with popular tracks pulled from
YouTube for other people's artists/genres. Generation runs the recipe through rec/road_trip.py and
materializes the result the same way every other generated playlist does (Generated group, GC,
taste-model quarantine) via executor.create_generated_playlist.
"""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from yt_playlist.library import executor
from yt_playlist.rec import road_trip as road_trip_rec

MIN_TARGET_MINUTES = 15
MAX_TARGET_MINUTES = 12 * 60


def _clean_list(raw):
    try:
        vals = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [v.strip() for v in vals if isinstance(v, str) and v.strip()]


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn

    @router.get("/road_trip")
    def road_trip_page(request: Request):
        recipes = store.list_road_trip_recipes()
        return templates.TemplateResponse(request, "road_trip.html", {"recipes": recipes})

    @router.post("/road_trip/recipes")
    async def save_recipe(request: Request):
        form = await request.form()
        try:
            recipe_id = int(form.get("id")) if form.get("id") else None
        except (TypeError, ValueError):
            recipe_id = None
        name = (form.get("name") or "").strip() or "Road Trip"
        try:
            own_pct = max(0, min(100, int(form.get("own_pct") or 50)))
        except (TypeError, ValueError):
            own_pct = 50
        try:
            target_minutes = max(MIN_TARGET_MINUTES,
                                 min(MAX_TARGET_MINUTES, int(form.get("target_minutes") or 60)))
        except (TypeError, ValueError):
            target_minutes = 60
        artists = _clean_list(form.get("artists"))
        genres = _clean_list(form.get("genres"))
        blacklist_genres = _clean_list(form.get("blacklist_genres"))
        store.save_road_trip_recipe(recipe_id, name, own_pct, artists, genres, blacklist_genres,
                                    target_minutes, now_fn())
        recipes = store.list_road_trip_recipes()
        return templates.TemplateResponse(request, "_partials/road_trip_recipes.html",
                                          {"recipes": recipes})

    @router.delete("/road_trip/recipes/{recipe_id}")
    def delete_recipe(request: Request, recipe_id: int):
        store.delete_road_trip_recipe(recipe_id)
        recipes = store.list_road_trip_recipes()
        return templates.TemplateResponse(request, "_partials/road_trip_recipes.html",
                                          {"recipes": recipes})

    @router.post("/road_trip/recipes/{recipe_id}/generate")
    async def generate_recipe(request: Request, recipe_id: int):
        recipe = store.get_road_trip_recipe(recipe_id)
        if recipe is None:
            return JSONResponse({"error": "recipe not found"}, status_code=404)
        identity_id, client = next(iter((ctx.client_provider() or {}).items()), (None, None))
        recipes = store.list_road_trip_recipes()
        if client is None:
            return templates.TemplateResponse(request, "_partials/road_trip_recipes.html",
                                              {"recipes": recipes,
                                               "error": "Connect an account to generate."})
        now = now_fn()
        tracks, stats = await asyncio.to_thread(
            road_trip_rec.assemble_playlist, store, client, recipe, now)
        if not tracks:
            return templates.TemplateResponse(request, "_partials/road_trip_recipes.html",
                                              {"recipes": recipes,
                                               "error": f"Couldn't build \"{recipe['name']}\" - no tracks found."})
        title = f"Road Trip: {recipe['name']}"
        result = await asyncio.to_thread(
            executor.create_generated_playlist, store, title, tracks, client, now, identity_id,
            recipe={"model": "road_trip", "road_trip_recipe_id": recipe_id, "journey": "road_trip",
                    "own_pct": recipe["own_pct"], "target_minutes": recipe["target_minutes"],
                    "achieved_minutes": stats["achieved_minutes"], "own_count": stats["own_count"],
                    "their_count": stats["their_count"]})
        store.set_road_trip_last_playlist(recipe_id, result["new_ytm"])
        recipes = store.list_road_trip_recipes()
        return templates.TemplateResponse(request, "_partials/road_trip_recipes.html",
                                          {"recipes": recipes})

    @router.get("/road_trip/autocomplete/artists")
    def autocomplete_artists(q: str = ""):
        q = q.strip()
        if not q:
            return JSONResponse({"results": []})
        _, client = next(iter((ctx.client_provider() or {}).items()), (None, None))
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
