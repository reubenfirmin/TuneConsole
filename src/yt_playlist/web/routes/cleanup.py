"""Cleanup page (`/cleanup`) plus the overlap suppress/ignore endpoints it drives."""
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from yt_playlist.library import analysis
from yt_playlist.rec import recommend


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    def _refresh():
        # htmx does a full page reload; the page recomputes, so a restored comparison
        # lands back in whichever section now applies (Exact duplicates or Overlaps).
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    @router.get("/cleanup")
    def cleanup(request: Request):
        ci = store.get_cleanup_ignored()                       # {category: set(ytm)} per-playlist dismissals
        ignored_sigs = store.get_ignored_merge_sigs()          # dismissed merge suggestions
        dupes = analysis.find_dupes(store)
        groups = analysis.find_identical_groups(store, ignored_sigs=ignored_sigs)   # exact-dup clusters
        empties = analysis.find_empty_playlists(store, ignored=ci.get("empty"))
        tiny = analysis.find_tiny_playlists(store, ignored=ci.get("tiny"))          # 1..3-track playlists
        exact_ids = {p.id for g in groups for p in g.playlists}
        near_groups = analysis.find_near_duplicate_groups(store, exclude_playlist_ids=exact_ids,
                                                          ignored_sigs=ignored_sigs)
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
        # Category-scoped dismissals, resolved to titles for the "Ignored cleanups" section. Stale
        # rows (a playlist since deleted) are dropped so the list only shows what can be restored.
        def _titles(ytms):
            return [{"ytm": y, "title": pl_by_ytm[y].title} for y in ytms if y in pl_by_ytm]
        ignored_empty = _titles(sorted(ci.get("empty", set())))
        ignored_tiny = _titles(sorted(ci.get("tiny", set())))
        ignored_merges = [{"signature": m["signature"],
                           "titles": [pl_by_ytm[y].title for y in m["members"] if y in pl_by_ytm]}
                          for m in store.get_ignored_merges()]
        ignored_merges = [m for m in ignored_merges if len(m["titles"]) > 1]   # need 2+ live members
        # Every cleanup edit HX-Refreshes back through here, so this is where the home card's cached
        # summary goes stale — refresh it now so the home "Playlist cleanups" count stays honest.
        recommend.refresh_cleanup(store, now_fn())
        return templates.TemplateResponse(request, "cleanup.html", {
            "groups": groups, "near_groups": near_groups, "overlaps": overlaps,
            "empties": empties, "tiny": tiny, "kinds": kinds,
            "identities": {i.id: i.label for i in store.get_identities()},
            "n_playlists": len(playlists),
            "n_identities": len(store.get_identities()),
            "n_groups": len(groups), "n_near": len(near_groups), "n_overlaps": len(overlaps),
            "hidden": hidden, "ignored": ignored,
            "ignored_empty": ignored_empty, "ignored_tiny": ignored_tiny, "ignored_merges": ignored_merges,
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

    # --- category-scoped cleanup ignores (Empty / Tiny per playlist; Exact / Near per merge) ------
    @router.post("/cleanup/ignore")
    def cleanup_ignore(ytm: str = Form(...), category: str = Form(...)):
        store.ignore_cleanup(ytm, category, now_fn())
        return _refresh()

    @router.post("/cleanup/unignore")
    def cleanup_unignore(ytm: str = Form(...), category: str = Form(...)):
        store.unignore_cleanup(ytm, category)
        return _refresh()

    @router.post("/cleanup/ignore-merge")
    def cleanup_ignore_merge(members: str = Form(...)):
        # `members` is the group's member ytm ids, comma-joined. The signature is membership-only,
        # so the dismissal sticks to THIS set of playlists and no other relationship they're in.
        ytms = [y for y in members.split(",") if y]
        store.ignore_merge(analysis.merge_signature(ytms), ytms, now_fn())
        return _refresh()

    @router.post("/cleanup/unignore-merge")
    def cleanup_unignore_merge(signature: str = Form(...)):
        store.unignore_merge(signature)
        return _refresh()

    return router
