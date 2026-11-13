"""Transport-neutral access/progression decisions shared by Home, Radio, TV and Vibe.

`check`/`require` take a store, explicit time and (for radio/vibes) an explicit
radio bootstrap snapshot -- never a Request, Ctx or live provider. Home, Radio,
TV, Vibe and background workers call these directly; the web layer is the only
place that translates a denial into an HTTP response.
"""
from yt_playlist.rec import account_growth

DENIED_MESSAGE = 'This stop is still ahead on your journey. Open Your unlocks to see what is next.'


class AccessDenied(Exception):
    """Raised by `require` when `area` is not yet unlocked."""

    def __init__(self, area, message=DENIED_MESSAGE):
        self.area = area
        super().__init__(message)


def check(store, area, *, now, radio=None, growth=None):
    """Decide access to `area`.

    `growth` reuses an already-computed Home growth snapshot when the caller
    has one; otherwise the cheapest sufficient check for `area` is used, same
    precedence as the prior web-only `guard()`: retained (historical) access,
    then an area-specific fast path for radio/vibes, then the full journey.
    """
    if growth is not None:
        return account_growth.has_access(growth, area)
    if account_growth.retained_access(store, area):
        return True
    if area == 'radio':
        return account_growth.radio_access(store, now, radio=radio)
    if area == 'vibes':
        return account_growth.vibe_access(store, now, radio)
    return account_growth.has_access(account_growth.view(store, now, radio=radio), area)


def require(store, area, *, now, radio=None, growth=None):
    if not check(store, area, now=now, radio=radio, growth=growth):
        raise AccessDenied(area)
