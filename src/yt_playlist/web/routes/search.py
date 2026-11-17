"""Shared music search: local navigation, catalog results and explicit song saves."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal


class SongSave(BaseModel):
    video_id: str = Field(min_length=3, max_length=150)
    query: str = Field(min_length=2, max_length=80)
    onboarding: bool = False
    check: bool = False
    saved: bool = True


class EntitySave(BaseModel):
    kind: Literal['artist', 'album', 'playlist']
    browse_id: str = Field(min_length=3, max_length=150)
    check: bool = False


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates
    catalog = ctx.music_search()

    @router.get("/search/omni")
    def omni(request: Request, q: str = ""):
        result = store.search.omni_search(q)
        return templates.TemplateResponse(request, "_partials/omni_results.html", result)

    @router.get('/search/catalog')
    def songs(q: str = ''):
        try:
            return {'songs': catalog.search(q)}
        except Exception as exc:
            ctx.logger.debug('Catalog song search unavailable', exc_info=True)
            raise HTTPException(503) from exc

    @router.post('/search/songs/save')
    def save(selection: SongSave):
        try:
            return catalog.save(selection.video_id, selection.query,
                                onboarding=selection.onboarding, check=selection.check, saved=selection.saved)
        except ValueError as exc:
            raise HTTPException(422) from exc
        except Exception as exc:
            ctx.logger.warning('Song save response unavailable', exc_info=True)
            raise HTTPException(503) from exc

    @router.get('/search/catalog/sections')
    def sections(q: str = ''):
        try:
            return catalog.sections(q)
        except Exception as exc:
            ctx.logger.debug('Catalog section search unavailable', exc_info=True)
            raise HTTPException(503) from exc

    @router.post('/search/entities/save')
    def save_entity(selection: EntitySave):
        try:
            return catalog.save_entity(selection.kind, selection.browse_id, check=selection.check)
        except ValueError as exc:
            raise HTTPException(422) from exc
        except Exception as exc:
            ctx.logger.warning('Catalog save response unavailable', exc_info=True)
            raise HTTPException(503) from exc

    return router
