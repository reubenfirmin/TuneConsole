"""Tools > Taste Model: full visibility into the recommendation model + tinkering controls."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from yt_playlist import embed, eval_recs, recommend
from yt_playlist.rec_dao import RecDao

LANES = ("resurface", "neighbourhood", "rotation", "deep_cut", "explore")


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    def _refresh():
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    def _model_context():
        bd = recommend.taste_breadth(store)
        dao = RecDao(store)
        tracks_total = len(store.get_playlists()) and dao.tracks_total()
        weights = store.get_weights()
        return {
            "vectors": store.rec_vectors_count(),
            "tracks_total": tracks_total,
            "tagged": bd["n_tagged"],
            "coverage": (bd["n_tagged"] / tracks_total) if tracks_total else 0.0,
            "breadth": bd["breadth"],
            "families": sorted(bd["families"].items(), key=lambda x: -x[1]),
            "baskets": len(store.rec_baskets()),
            "lanes": [(ln, weights.get(f"lane:{ln}", 1.0)) for ln in LANES],
            "feedback": dao.feedback_summary(),
        }

    @router.get("/taste")
    def taste_page(request: Request):
        return templates.TemplateResponse(request, "taste.html", _model_context())

    @router.get("/taste/recall")
    def taste_recall(request: Request):
        # the one expensive stat — lazy-loaded so the page paints instantly
        return templates.TemplateResponse(request, "_partials/taste_recall.html",
                                          {"recall": eval_recs.recall_at_k(store, k=20)})

    @router.post("/taste/weight")
    async def taste_weight(request: Request):
        form = await request.form()
        axis, val = form.get("axis"), form.get("weight")
        if axis and val:
            store.set_weight(axis, float(val))
        return _refresh()

    @router.post("/taste/autotune")
    def taste_autotune():
        """Grid-search the embedding dim by recall@k and rebuild on the winner."""
        eval_recs.autotune(store)
        return _refresh()

    @router.post("/taste/reset-weights")
    def taste_reset_weights():
        store.reset_weights()
        return _refresh()

    @router.post("/taste/clear-feedback")
    def taste_clear_feedback():
        RecDao(store).clear_feedback()
        return _refresh()

    @router.post("/taste/rebuild")
    def taste_rebuild():
        if ctx.rec_worker:
            ctx.rec_worker.rebuild()
        else:
            embed.build_and_store(store)
        return _refresh()

    @router.post("/taste/purge-vectors")
    def taste_purge():
        store.replace_rec_vectors([])
        return _refresh()

    return router
