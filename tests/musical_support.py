"""Prepare the same retained profile inputs and bounded vibe pools as the worker."""
from tests.recommendation_support import publish_bundles
from yt_playlist.rec import musical_preparation
from yt_playlist.rec.progressive import Budget, Preparation


def prepare_profiles(store, now=1000):
    # Consumers need all completed profiles, rather than a particular number
    # of worker turns. Keep every recording and real guarded publication while
    # processing this controlled input in larger, bounded batches.
    preparation = Preparation(store, budget=Budget(batch=512))
    try:
        for _ in range(500):
            if not store.prepared_content.has_pending() and not musical_preparation.due(store):
                return
            # A bounded capture may defer without completing a batch. Continue
            # until the actual work drains, with the same finite attempt bound.
            preparation.capture(now)
        raise AssertionError('Profile preparation did not drain: '
            f'pending={store.prepared_content.pending(32)!r}, '
            f'musical_due={musical_preparation.due(store)!r}')
    finally:
        preparation.close()


def prepare_vibes(store, now=1000):
    prepare_profiles(store, now)
    bundles = getattr(store, '_fixture_bundle_payload', None)
    if bundles is None:
        bundles = store.rec_proposals.get_proposals('mode_bundles') or {}
    publish_bundles(store, bundles, now, vibes=True)
    return bundles
