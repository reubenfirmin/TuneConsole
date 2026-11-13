"""Shared access to prepared Home destinations, including fragment entry."""
from contextlib import contextmanager

from fastapi import HTTPException, Request

from yt_playlist.rec import access, account_growth, mix_tuning


def state(ctx, request=None):
    if request is not None and hasattr(request.state, 'account_growth'):
        return request.state.account_growth
    result = account_growth.view(ctx.store, ctx.now_fn(), radio=ctx.radio.bootstrap())
    if request is not None:
        request.state.account_growth = result
    return result


def guard(ctx, area, *, radio=None):
    """A FastAPI dependency; the access decision itself needs no Request.

    See `yt_playlist.rec.access` for the transport-neutral decision that Radio,
    TV, Vibe and background workers call directly without this wrapper.
    """
    def require(request: Request):
        growth = getattr(request.state, 'account_growth', None)
        try:
            access.require(ctx.store, area, now=ctx.now_fn(),
                           radio=ctx.radio.bootstrap() if radio is None else radio, growth=growth)
        except access.AccessDenied as exc:
            raise HTTPException(409, str(exc)) from exc
    return require


@contextmanager
def vibe_selection(ctx, request):
    now = ctx.now_fn()
    mix_tuning.expire(ctx.store, now)
    # Admission ends before accepting the mutation. Generation belongs to the
    # independent worker and never inherits this request reader's deadline.
    with ctx.store.read_view() as reader:
        if not account_growth.vibe_access(reader, now, ctx.radio.bootstrap(reader=reader)):
            raise HTTPException(409, 'This stop is still ahead on your journey. Open Your unlocks to see what is next.')
    yield now


def mix_guard(ctx):
    """Keep the small starter's rotation separate from the full Mix tapes area."""
    require = guard(ctx, 'directions')
    def check(request: Request):
        if request.url.path not in ('/home/refresh-card/onboard_library', '/home/refresh-card/onboard_radio'):
            require(request)
    return check
