"""Compatibility entry points for mode acquisition and detached discovery."""
from yt_playlist.library.taste_mode_composition import modes
from yt_playlist.rec.mode_discovery import (
    _SEED, _MAX_ITERS, _MIN_CORPUS, _DISTANCE_WORK_BYTES, _distance_batches, _distance_to,
    _nearest_centers, _kmeanspp, mode_label, discover_from_inputs, REP_MATCH_THRESHOLD,
    _rep_containment, reconcile, assign_mode_ids,
)


def discover_modes(store, **kwargs):
    return modes(store).discover(**kwargs)


def live_modes(store, **kwargs):
    return modes(store).live(**kwargs)


def prepare_modes(store, now, **kwargs):
    return modes(store).prepare(now, **kwargs)
