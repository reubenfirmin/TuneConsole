"""Cleanup page (`/cleanup`) plus the overlap suppress/ignore endpoints it drives."""
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from yt_playlist import analysis


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    def _refresh():
        # htmx does a full page reload; the page recomputes, so a restored comparison
        # lands back in whichever section now applies (Exact duplicates or Overlaps).
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    @router.get("/cleanup")
    def cleanup(request: Request):
        dupes = analysis.find_dupes(store)
        groups = analysis.find_identical_groups(store)         # exact-duplicate clusters
        empties = analysis.find_empty_playlists(store)
        tiny = analysis.find_tiny_playlists(store)             # 1..3-track playlists
        exact_ids = {p.id for g in groups for p in g.playlists}
        near_groups = analysis.find_near_duplicate_groups(store, exclude_playlist_ids=exact_ids)
        dupe_ids = {p.id for d in dupes for p in (d.playlist_a, d.playlist_b)}
        suppressed = store.get_suppressed_overlap_pairs()
        ignored_ytm = store.get_overlap_ignored()
        kept_pairs = store.get_overlap_kept_pairs()
        overlaps = analysis.find_overlaps(store, exclude_playlist_ids=dupe_ids,
                                          suppressed=suppressed, ignored_ytm=ignored_ytm, kept=kept_pairs)
        playlists = store.get_playlists()
        pl_by_ytm = {p.ytm_playlist_id: p for p in playlists}
        kinds = {p.ytm_playlist_id: store.playlist_kind(p.id) for p in playlists}   # audio/video/mixed
        # Only surface prefs whose playlists still exist — a deleted side leaves a stale pair.
        hidden = [{"a": a, "b": b, "a_title": pl_by_ytm[a].title, "b_title": pl_by_ytm[b].title}
                  for a, b, _ in store.get_suppressed_overlaps()
                  if a in pl_by_ytm and b in pl_by_ytm]
        ignored = [{"ytm": y, "title": pl_by_ytm[y].title}
                   for y in sorted(ignored_ytm) if y in pl_by_ytm]
        return templates.TemplateResponse(request, "cleanup.html", {
            "groups": groups, "near_groups": near_groups, "overlaps": overlaps,
            "empties": empties, "tiny": tiny, "kinds": kinds,
            "identities": {i.id: i.label for i in store.get_identities()},
            "n_playlists": len(playlists),
            "n_identities": len(store.get_identities()),
            "n_groups": len(groups), "n_near": len(near_groups), "n_overlaps": len(overlaps),
            "hidden": hidden, "ignored": ignored,
            "flash": request.query_params.get("flash"),
            "flash_pl": request.query_params.get("flash_pl"),
            "flasherr": request.query_params.get("flasherr"),
        })

    @router.post("/overlaps/ignore")
    def ignore_overlap(ytm: str = Form(...)):
        store.ignore_overlap_playlist(ytm, now_fn())
        return _refresh()

    @router.post("/overlaps/unignore")
    def unignore_overlap(ytm: str = Form(...)):
        store.unignore_overlap_playlist(ytm)
        return _refresh()   # restore: recompute so the comparison reappears in its section

    @router.post("/overlaps/mute-others")
    def mute_other_overlaps(a: str = Form(...), b: str = Form(...)):
        # Keep the a–b pair, but mute every OTHER overlap involving either a or b.
        store.keep_overlap_pair(a, b, now_fn())
        store.ignore_overlap_playlist(a, now_fn())
        store.ignore_overlap_playlist(b, now_fn())
        return _refresh()

    @router.post("/overlaps/suppress")
    def suppress_overlap(a: str = Form(...), b: str = Form(...)):
        store.suppress_overlap(a, b, now_fn())
        return _refresh()   # the pair moves to the "Hidden overlaps" section

    @router.post("/overlaps/suppress-many")
    async def suppress_many(request: Request):
        # Bulk-hide the low-overlap "tail": the floating Dismiss-below control sends [[a,b],...].
        form = await request.form()
        try:
            pairs = json.loads(form.get("pairs", "[]"))
        except (ValueError, TypeError):
            pairs = []
        now = now_fn()
        for pair in pairs:
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and all(pair):
                store.suppress_overlap(pair[0], pair[1], now)
        return _refresh()

    @router.post("/overlaps/unsuppress")
    def unsuppress_overlap(a: str = Form(...), b: str = Form(...)):
        store.unsuppress_overlap(a, b)
        return _refresh()   # restore: recompute so the comparison reappears in its section

    return router
