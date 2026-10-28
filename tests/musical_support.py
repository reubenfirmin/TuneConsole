"""Prepare the same retained profile inputs and bounded vibe pools as the worker."""
from yt_playlist.rec import measurement, musical_preparation, vibe_pools
from yt_playlist.rec.progressive import Preparation


def prepare_profiles(store, now=1000):
    preparation = Preparation(store)
    for _ in range(500):
        if not store.prepared_content.has_pending() and not musical_preparation.due(store):
            return
        assert preparation.capture(now)
    raise AssertionError('Profile preparation did not drain')


def prepare_vibes(store, now=1000):
    prepare_profiles(store, now)
    bundles = store.rec_proposals.get_proposals('mode_bundles') or {}
    vibe_pools.prepare(store, bundles, now, measurement.PoolPolicies(store, now))
    store.rec_proposals.put_proposals('mode_bundles', bundles, now)
    return bundles
