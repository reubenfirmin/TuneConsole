"""Compatibility entry points for Home mode preparation and assembly."""
from yt_playlist.rec.mode_inputs import *
from yt_playlist.rec.mode_inputs import (
    _order_key, _nearest_mode, _bucket_pool, _publish_typed_bundles, _DeferredBundles,
    _calculate_bundles, _rank_pools, _build_temporal, _tilt_key,
    _MIN_CARD, _PPR_TAIL,
)
from yt_playlist.rec.mode_rules import _item_dict, _temporal_item, _diversify, _in_band, _CARD_LABELS, _BAND_LABELS


def prepare_scheduled(store, now):
    from yt_playlist.library.mode_composition import scheduled
    return scheduled(store, now)


def prepare_bundles(store, now):
    from yt_playlist.library.mode_composition import preparation
    return preparation(store).prepare(now)
