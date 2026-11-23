"""Local account-growth projection and the deferred, explicit Home choice."""
from fastapi import APIRouter, Depends, Response

from yt_playlist.library.home_query_composition import action_notices
from yt_playlist.rec import account_growth, home_preferences, home_prepared
from yt_playlist.web.context import request_form


def build(ctx):
    router = APIRouter()

    @router.get('/home/growth')
    def growth(navigation: bool = False):
        now = ctx.now_fn()
        with ctx.store.read_view(tracked=True) as reader:
            state = account_growth.view(reader, now, radio=ctx.radio.bootstrap(reader=reader), navigation=navigation)
            if navigation:
                state = {**state, 'has_action_notices': bool(
                    home_prepared.recap_notice(reader, now) or action_notices(ctx, reader, now))}
            return state

    @router.post('/home/growth/format')
    def choose_format(form=Depends(request_form)):
        try:
            home_preferences.choose(ctx.store, form.get('format'), now=ctx.now_fn(), radio=ctx.radio)
        except home_preferences.FormatNotReady as exc:
            return Response(str(exc), status_code=409)
        except ValueError as exc:
            return Response(str(exc), status_code=422)
        return Response(status_code=204)

    return router
