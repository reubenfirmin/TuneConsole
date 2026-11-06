import numpy as np
from tests.retained_rating_support import observe_rating
from yt_playlist.rec import onboarding


def _synced_thin(store):
    # a sync has run (so onboarding applies) but the model is thin: low coverage, no modes
    store.settings.set_setting("last_sync_at", "999")
    from tests.conftest import seed_catalog_track
    track_id = seed_catalog_track(store,'a|x','T','A')
    store.conn.execute("UPDATE recordings SET first_enriched_at=1 WHERE identity_key='a|x'")
    store.conn.commit()
    return track_id


def test_active_when_synced_and_thin(store):
    _synced_thin(store)
    assert onboarding.onboarding_active(store, now=10.0) is True


def test_inactive_before_any_sync(store):
    assert onboarding.onboarding_active(store, now=10.0) is False


def test_inactive_when_dismissed(store):
    _synced_thin(store)
    store.settings.set_setting("onboard_dismissed", "1")
    assert onboarding.onboarding_active(store, now=10.0) is False


def test_feedback_alone_does_not_graduate_without_usable_offers(store):
    _synced_thin(store)
    observe_rating(store, 'liked|artist', 'LIKE', 2, provenance='action')
    observe_rating(store, 'disliked|artist', 'DISLIKE', 3, provenance='action')
    assert store.rec_model.recent_liked_keys() == ['liked|artist']
    assert [row['item_key'] for row in store.rec_model.list_dislikes()] == ['disliked|artist']
    assert not onboarding.setup_completed(store)
    assert onboarding.initial_setup_pending(store, now=10.0)
    assert onboarding.onboarding_active(store, now=10.0) is True


def test_taste_alone_does_not_graduate_without_a_pool_and_usable_menu(store):
    _synced_thin(store)
    store.modes.replace_modes([{"mode_id": 1, "label": "m", "families": [["house", 1]],
                                "centroid": np.array([1.0], dtype=np.float32), "size": 5,
                                "rep_keys": []}], retired_ids=[], now=1.0)
    assert onboarding.onboarding_active(store, now=10.0) is True


def test_active_when_genre_coverage_is_complete_but_modes_are_not_ready(store):
    track_id = _synced_thin(store)
    store.tracks.set_track_genre(track_id, "house")
    coverage = store.enrichment_reports.coverage_stats()
    assert coverage["genre"] == coverage["total"] == 1
    assert onboarding.onboarding_active(store, now=10.0) is True
