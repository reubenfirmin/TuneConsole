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
        choice = form.get('format')
        if choice not in home_preferences.PANELS:
            return Response('Choose Long play, Mix tapes or On air.', status_code=422)
        state = account_growth.view(ctx.store, ctx.now_fn(), radio=ctx.radio.bootstrap())
        if not all(state['ready_formats'].values()):
            return Response('Wait until all three listening formats are ready.', status_code=409)
        ctx.store.settings.set_setting('home_format', choice)
        return Response(status_code=204)

    return router
