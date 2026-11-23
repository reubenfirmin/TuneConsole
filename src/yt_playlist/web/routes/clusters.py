"""Cluster transport and rendering over complete query/save commands."""
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from yt_playlist.core.activity import activity_for
from yt_playlist.web.cluster_projection import search_results
from yt_playlist.web.context import Ctx


def build(ctx) -> APIRouter:
    router = APIRouter()
    commands = Ctx.cluster_commands(ctx)

    @router.get('/clusters')
    def page(request: Request):
        return ctx.templates.TemplateResponse(request, 'clusters.html', commands.page())

    @router.get('/clusters/search')
    def search(q: str = ''):
        return JSONResponse(search_results(commands.search(q)))

    @router.get('/clusters/state/{ytm}')
    def canvas(ytm: str):
        state = commands.canvas(ytm)
        if not state:
            return JSONResponse({'error': 'no canvas'}, status_code=404)
        return Response(content=state, media_type='application/json')

    @router.get('/clusters/genres')
    def genres():
        return JSONResponse(commands.genres())

    @router.post('/clusters/uploads')
    def playable_uploads(keys: list[str]):
        return JSONResponse(commands.playable_uploads(keys))

    @router.post('/clusters/expand')
    async def expand(request: Request):
        return JSONResponse(await commands.expansion(await request.json()))

    @router.post('/clusters/explain')
    async def explain(request: Request):
        return JSONResponse(await commands.explanation(await request.json()))

    @router.post('/clusters/save')
    async def save(request: Request):
        form = await request.form()
        intent = commands.admit_save()
        try:
            keep = list(json.loads(form.get('keep_keys') or '[]'))
            central = list(json.loads(form.get('central_keys') or '[]'))
            labels = list(json.loads(form.get('seed_labels') or '[]'))
            families = list(json.loads(form.get('allow_families') or '[]'))
        except (ValueError, TypeError):
            keep, central, labels, families = [], [], [], []
        canvas = form.get('state')
        try:
            json.loads(canvas) if canvas else None
        except (ValueError, TypeError):
            canvas = None
        model = await activity_for(ctx).to_thread(commands.save, (form.get('name') or '').strip(),
            keep, central, labels, families, (form.get('journey') or 'auto').strip(), canvas, intent)
        response = ctx.templates.TemplateResponse(request, '_partials/generated_result.html', model)
        if not model.get('error'):
            response.headers['HX-Trigger'] = 'cluster-saved'
        return response

    return router
