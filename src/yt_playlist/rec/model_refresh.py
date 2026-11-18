"""Legacy model-preparation entry points over detached fits and guarded publication."""
from yt_playlist.rec.model_calculations import *
from yt_playlist.rec.model_calculations import _check_fit_memory, _fingerprint


def prepare(reader, now, *, keep_collaborative=False, memory_bytes=None, fit_cache=None):
    from yt_playlist.library.model_composition import capture
    return calculate(capture(reader), now, keep_collaborative=keep_collaborative,
                     memory_bytes=memory_bytes, fit_cache=fit_cache)


def rebuild(store, now, *, keep_collaborative=False, memory_bytes=None, fit_cache=None):
    from yt_playlist.library.model_composition import preparation
    return preparation(store).rebuild(now, keep_collaborative=keep_collaborative,
                                     memory_bytes=memory_bytes, fit_cache=fit_cache)
