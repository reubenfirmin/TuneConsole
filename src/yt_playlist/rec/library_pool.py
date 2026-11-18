"""Compatibility entry points for explicit library-pool commands."""
from yt_playlist.rec.library_pool_inputs import *
from yt_playlist.rec.library_pool_inputs import _calculate


def prepare(store, now, parameters):
    from yt_playlist.library.pool_composition import library
    return library(store).prepare(now, parameters)
