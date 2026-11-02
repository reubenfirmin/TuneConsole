"""Shared access to prepared Home destinations, including fragment entry."""
from fastapi import HTTPException, Request

from yt_playlist.rec import account_growth


def state(ctx, request=None):
    if request is not None and hasattr(request.state, 'account_growth'):
        return request.state.account_growth
    result = account_growth.view(ctx.store, ctx.now_fn(), radio=ctx.radio.bootstrap())
    if request is not None:
        request.state.account_growth = result
    return result


def guard(ctx, area, *, radio=None):
    def require(request: Request):
        if not hasattr(request.state, 'account_growth') and account_growth.retained_access(ctx.store, area):
            allowed = True
        elif area == 'radio' and not hasattr(request.state, 'account_growth'):
            allowed = account_growth.radio_access(ctx.store, ctx.now_fn(),
                radio=ctx.radio.bootstrap() if radio is None else radio)
        else:
            growth = state(ctx, request)
            allowed = account_growth.has_access(growth, area)
        if not allowed:
            raise HTTPException(409, 'This stop is still ahead on your journey. Open Your unlocks to see what is next.')
    return require


def mix_guard(ctx):
    """Keep the small starter's rotation separate from the full Mix tapes area."""
    require = guard(ctx, 'directions')
    def check(request: Request):
        if request.url.path not in ('/home/refresh-card/onboard_library', '/home/refresh-card/onboard_radio'):
            require(request)
    return check
