"""Transport-neutral access/progression decisions shared by Home, Radio, TV and Vibe.

`check`/`require` take a store, explicit time and optional Radio facts. Callers
can supply an existing Radio snapshot or a loader for the readiness check.
"""
from yt_playlist.rec import account_growth, growth_access

DENIED_MESSAGE = 'This stop is still ahead on your journey. Open Your unlocks to see what is next.'


class AccessDenied(Exception):
    """Raised by `require` when `area` is not yet unlocked."""

    def __init__(self, area, message=DENIED_MESSAGE):
        self.area = area
        super().__init__(message)


def check(store, area, *, now, radio=None, growth=None, load_radio=None):
    """Decide access to `area`.

    `growth` reuses an already-computed Home growth snapshot when the caller
    has one; otherwise the cheapest sufficient check for `area` is used, same
    precedence as the prior web-only `guard()`: retained (historical) access,
    then an area-specific fast path for radio/vibes, then the full journey.
    `account_growth` only acquires facts from `store` here; `growth_access`'s
    detached policies make every actual access/progression decision.
    """
    if growth is not None:
        return growth_access.has_access(growth, area)
    if growth_access.retained_access(account_growth.retained_usage(store), area):
        return True
    if radio is None and load_radio is not None:
        radio = load_radio()
    if area == 'radio':
        return account_growth.radio_access(store, now, radio=radio)
    if area == 'vibes':
        return account_growth.vibe_access(store, now, radio)
    return growth_access.has_access(account_growth.view(store, now, radio=radio), area)


def require(store, area, *, now, radio=None, growth=None, load_radio=None):
    if not check(store, area, now=now, radio=radio, growth=growth, load_radio=load_radio):
        raise AccessDenied(area)
