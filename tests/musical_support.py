"""Prepare the same retained profile inputs and bounded vibe pools as the worker."""
from tests.recommendation_support import publish_bundles
from yt_playlist.rec import musical_preparation
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
    bundles = getattr(store, '_fixture_bundle_payload', None)
    if bundles is None:
        bundles = store.rec_proposals.get_proposals('mode_bundles') or {}
    publish_bundles(store, bundles, now, vibes=True)
    return bundles
