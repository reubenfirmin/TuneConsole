"""Cleanup page (`/cleanup`) plus the overlap suppress/ignore endpoints it drives."""
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from yt_playlist.library import analysis
from yt_playlist.rec.actions import refresh_cleanup


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    def _refresh():
        # htmx does a full page reload; the page recomputes, so a restored comparison
        # lands back in whichever cleanup section now applies.
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    @router.get("/cleanup")
    def cleanup(request: Request):
        result = analysis.analyze_cleanup(store)
        prefs = result.analysis.preferences
        ci, ignored_ytm = prefs.ignored, prefs.ignored_ytm
        groups, near_pairs, overlaps = result.groups, result.near_pairs, result.overlaps
        empties, tiny = result.empties, result.tiny
        playlists = result.analysis.memberships.playlists
        pl_by_ytm = {p.scope_ref: p for p in playlists}
        kinds = result.analysis.memberships.kinds
        # Only surface prefs whose playlists still exist: a deleted side leaves a stale pair.
        hidden = [{"a": a, "b": b, "a_title": pl_by_ytm[a].title, "b_title": pl_by_ytm[b].title}
                  for a, b, _ in prefs.suppressed
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
                          for m in prefs.merges]
        ignored_merges = [m for m in ignored_merges if len(m["titles"]) > 1]   # need 2+ live members
        # Every cleanup edit HX-Refreshes back through here, so this is where the home card's cached
        # summary goes stale. Refresh it now so the home "Playlist cleanups" count stays honest.
        refresh_cleanup(store, now_fn(), summary=result.summary())
        identities = store.identities.get_identities()
        return templates.TemplateResponse(request, "cleanup.html", {
            "groups": groups, "near_pairs": near_pairs, "overlaps": overlaps,
            "empties": empties, "tiny": tiny, "kinds": kinds,
            "identities": {i.id: i.label for i in identities},
            "n_playlists": len(playlists),
            "n_identities": len(identities),
            "n_groups": len(groups), "n_near": len(near_pairs), "n_overlaps": len(overlaps),
            "hidden": hidden, "ignored": ignored,
            "ignored_empty": ignored_empty, "ignored_tiny": ignored_tiny, "ignored_merges": ignored_merges,
            "flash": request.query_params.get("flash"),
            "flash_pl": request.query_params.get("flash_pl"),
            "flasherr": request.query_params.get("flasherr"),
        })

    @router.post("/overlaps/ignore")
    def ignore_overlap(ytm: str = Form(...)):
        store.overlaps.ignore_overlap_playlist(ytm, now_fn())
        return _refresh()

    @router.post("/overlaps/unignore")
    def unignore_overlap(ytm: str = Form(...)):
        store.overlaps.unignore_overlap_playlist(ytm)
        return _refresh()   # restore: recompute so the comparison reappears in its section

    @router.post("/overlaps/mute-others")
    def mute_other_overlaps(a: str = Form(...), b: str = Form(...)):
        # Keep the a–b pair, but mute every OTHER overlap involving either a or b.
        store.overlaps.keep_overlap_pair(a, b, now_fn())
        store.overlaps.ignore_overlap_playlist(a, now_fn())
        store.overlaps.ignore_overlap_playlist(b, now_fn())
        return _refresh()

    @router.post("/overlaps/suppress")
    def suppress_overlap(a: str = Form(...), b: str = Form(...)):
        store.overlaps.suppress_overlap(a, b, now_fn())
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
                store.overlaps.suppress_overlap(pair[0], pair[1], now)
        return _refresh()

    @router.post("/overlaps/unsuppress")
    def unsuppress_overlap(a: str = Form(...), b: str = Form(...)):
        store.overlaps.unsuppress_overlap(a, b)
        return _refresh()   # restore: recompute so the comparison reappears in its section

    # --- category-scoped cleanup ignores (Empty / Tiny per playlist; Exact / Near per merge) ------
    @router.post("/cleanup/ignore")
    def cleanup_ignore(ytm: str = Form(...), category: str = Form(...)):
        store.overlaps.ignore_cleanup(ytm, category, now_fn())
        return _refresh()

    @router.post("/cleanup/unignore")
    def cleanup_unignore(ytm: str = Form(...), category: str = Form(...)):
        store.overlaps.unignore_cleanup(ytm, category)
        return _refresh()

    @router.post("/cleanup/ignore-merge")
    def cleanup_ignore_merge(members: str = Form(...)):
        # `members` is the group's member ytm ids, comma-joined. The signature is membership-only,
        # so the dismissal sticks to THIS set of playlists and no other relationship they're in.
        ytms = [y for y in members.split(",") if y]
        store.overlaps.ignore_merge(analysis.merge_signature(ytms), ytms, now_fn())
        return _refresh()

    @router.post("/cleanup/unignore-merge")
    def cleanup_unignore_merge(signature: str = Form(...)):
        store.overlaps.unignore_merge(signature)
        return _refresh()

    return router
