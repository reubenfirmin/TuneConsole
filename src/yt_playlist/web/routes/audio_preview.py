"""Read-only metadata preview. All audio/model processing stays in the YTM tab."""
from fastapi import APIRouter, Request

from yt_playlist.core.audio_preview_assets import ASSETS, MODEL
from yt_playlist.core.audio_preview_assets import available as _available
from .bridge import DEV_EXTENSION_ID, EXTENSION_ID


def available():
    return _available(MODEL, ASSETS)


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
