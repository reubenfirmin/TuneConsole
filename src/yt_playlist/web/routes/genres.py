"""Enrichment's shared genre vocabulary and two-level recommendation hierarchy."""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from yt_playlist.providers import genres as genre_lib
from yt_playlist.util import genre_catalog, genre_map


def genre_list_context(store, query=""):
    """Group recognized tags using the same family assignments as recommendations."""
    names = store.get_genre_whitelist()
    records, labels = genre_catalog.catalog()
    aliases = {}
    for key, (genre, label, source) in labels.items():
        if source != "spelling" and key != genre:
            aliases.setdefault(records[genre]["name"], []).append(label)
    family_labels = genre_catalog.family_labels()
    query = query.strip()
    query_key = genre_catalog.normalize(query)
    grouped, unassigned = {}, []
    total_families = set()
    matching = 0
    for name in names:
        family = genre_map.family(name)
        if not family.startswith("other:"):
            total_families.add(family)
        if query_key and not any(query_key in genre_catalog.normalize(text) for text in
                                 [name, family_labels.get(family, ""), *aliases.get(name, [])]):
            continue
        matching += 1
        if family.startswith("other:"):
            unassigned.append(name)
        else:
            grouped.setdefault(family, []).append(name)
    families = [{"label": family_labels.get(family, family.title()), "genres": members}
                for family, members in grouped.items()]
    families.sort(key=lambda group: group["label"].lower())
    return {"genres": names, "genre_families": families, "unassigned_genres": unassigned,
            "genre_aliases": aliases, "genre_query": query, "matching_genres": matching,
            "genre_family_count": len(total_families)}


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    def _list(request, query=""):
        return templates.TemplateResponse(request, "_partials/genre_list.html",
                                          genre_list_context(store, query))

    def _toast(request, message):
        return templates.TemplateResponse(
            request, "_partials/error_toast.html", {"message": message},
            status_code=422, headers={"HX-Reswap": "none"})

    @router.get("/genres")
    def genres_page():
        return RedirectResponse("/enrich?advanced=genres#recognized-genre-tags", status_code=303)

    @router.get("/genres/list")
    def genres_list(request: Request, q: str = ""):
        return _list(request, q)

    @router.post("/genres/add")
    async def genres_add(request: Request):
        form = await request.form()
        name = (form.get("name") or "").strip()
        if not genre_catalog.normalize(name):
            return _toast(request, "enter a genre name")
        store.add_genre(genre_lib.canonical_name(name))
        genre_lib.configure(store)                     # rebuild the active matcher
        return _list(request, form.get("q") or "")

    @router.post("/genres/remove")
    async def genres_remove(request: Request):
        form = await request.form()
        store.remove_genre(genre_lib.canonical_name(form.get("name") or ""))
        genre_lib.configure(store)
        return _list(request, form.get("q") or "")

    @router.post("/genres/reset")
    async def genres_reset(request: Request):
        form = await request.form()
        genre_lib.reset(store)
        return _list(request, form.get("q") or "")

    return router
