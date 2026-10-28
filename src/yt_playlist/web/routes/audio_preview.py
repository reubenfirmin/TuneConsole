"""Read-only metadata preview. All audio/model processing stays in the YTM tab."""
from pathlib import Path

from fastapi import APIRouter, Request

from .bridge import DEV_EXTENSION_ID, EXTENSION_ID


MODEL = Path(__file__).resolve().parents[4] / 'extension' / 'audio_model'
ASSETS = frozenset({
    'model_worker.js', 'runtime_loader.js', 'features.js', 'embedding_graph.js', 'audio_attributes.js',
    'vendor/bundle.json', 'vendor/runtime.json', 'vendor/essentia-csp.js',
    'vendor/tf.min.js', 'vendor/meyda.min.js', 'vendor/musicnn/model.json',
    'vendor/musicnn/group1-shard1of1.bin', 'vendor/musicnn/metadata.json',
} | {f'vendor/{head}/{name}' for head in ('relaxed', 'aggressive', 'happy', 'sad', 'deam')
     for name in ('model.json', 'group1-shard1of1.bin', 'metadata.json')})


def available():
    return all((MODEL / name).is_file() for name in ASSETS)


def build(ctx):
    from . import audio_vibes

    router = APIRouter()
    router.include_router(audio_vibes.build(ctx))
    ctx.templates.env.globals['audio_preview_available'] = available
    ctx.templates.env.globals['audio_vibe_extension_ids'] = [DEV_EXTENSION_ID, EXTENSION_ID]

    @router.get('/audio-preview')
    def preview(request: Request):
        return ctx.templates.TemplateResponse(request, 'audio_preview.html', {
            'model_available': available(),
            'extension_ids': [DEV_EXTENSION_ID, EXTENSION_ID],
        })

    return router
