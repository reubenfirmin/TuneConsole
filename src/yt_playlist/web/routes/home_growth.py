"""Local account-growth projection and the deferred, explicit Home choice."""
from fastapi import APIRouter, Depends, Response

from yt_playlist.rec import account_growth, home_preferences
from yt_playlist.web.context import request_form


def build(ctx):
    router = APIRouter()

    @router.get('/home/growth')
    def growth():
        return account_growth.view(ctx.store, ctx.now_fn(), radio=ctx.radio.bootstrap())

    @router.post('/home/growth/format')
    def choose_format(form=Depends(request_form)):
        state = account_growth.view(ctx.store, ctx.now_fn(), radio=ctx.radio.bootstrap())
        try:
            home_preferences.choose(ctx.store, form.get('format'), ready_formats=state['ready_formats'])
        except home_preferences.FormatNotReady as exc:
            return Response(str(exc), status_code=409)
        except ValueError as exc:
            return Response(str(exc), status_code=422)
        return Response(status_code=204)

    return router
