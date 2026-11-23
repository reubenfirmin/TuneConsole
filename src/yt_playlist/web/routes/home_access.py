"""Shared access to prepared Home destinations, including fragment entry."""
from contextlib import contextmanager

from fastapi import HTTPException, Request

from yt_playlist.rec import access, account_growth, growth_access, mix_tuning


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
                           radio=radio, growth=growth, load_radio=ctx.radio.bootstrap)
        except access.AccessDenied as exc:
            raise HTTPException(409, str(exc)) from exc
    return require


@contextmanager
def vibe_selection(ctx):
    now = ctx.now_fn()
    # Release the admission reader before applying tuning and assembling mixes.
    with ctx.store.read_view() as reader:
        usage = account_growth.retained_usage(reader)
    if not growth_access.retained_access(usage, 'vibes'):
        # Readiness depends on current tuning. Earned access only needs to
        # expire old controls when the new selection takes the writer.
        mix_tuning.expire(ctx.store, now)
        with ctx.store.read_view() as reader:
            if not account_growth.vibe_access(reader, now, lambda: ctx.radio.bootstrap(reader=reader), usage=usage):
                raise HTTPException(409, access.DENIED_MESSAGE)
    yield now


def mix_guard(ctx):
    """Keep the small starter's rotation separate from the full Mix tapes area."""
    require = guard(ctx, 'directions')
    def check(request: Request):
        if request.url.path not in ('/home/refresh-card/onboard_library', '/home/refresh-card/onboard_radio'):
            require(request)
    return check
