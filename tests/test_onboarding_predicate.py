import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import onboarding


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema(); return s


def _synced_thin(store):
    # a sync has run (so onboarding applies) but the model is thin: low coverage, no modes
    store.settings.set_setting("last_sync_at", "999")
    from tests.conftest import seed_catalog_track
    track_id = seed_catalog_track(store,'a|x','T','A')
    store.conn.execute("UPDATE tracks SET first_enriched_at=1 WHERE identity_key='a|x'")
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


def test_inactive_when_enough_feedback(store, monkeypatch):
    from yt_playlist.rec import rec_params

    _synced_thin(store)
    rec_params.set_param(store, "onboard_feedback_min", 3)
    monkeypatch.setattr(onboarding, "feedback_count", lambda s: 2)
    assert onboarding.onboarding_active(store, now=10.0) is True
    monkeypatch.setattr(onboarding, "feedback_count", lambda s: 3)
    assert onboarding.onboarding_active(store, now=10.0) is False


def test_inactive_when_taste_ready_with_no_genre_coverage(store):
    _synced_thin(store)
    store.modes.replace_modes([{"mode_id": 1, "label": "m", "families": [["house", 1]],
                                "centroid": np.array([1.0], dtype=np.float32), "size": 5,
                                "rep_keys": []}], retired_ids=[], now=1.0)
    assert onboarding.onboarding_active(store, now=10.0) is False


def test_active_when_genre_coverage_is_complete_but_modes_are_not_ready(store):
    track_id = _synced_thin(store)
    store.tracks.set_track_genre(track_id, "house")
    coverage = store.enrichment_reports.coverage_stats()
    assert coverage["genre"] == coverage["total"] == 1
    assert onboarding.onboarding_active(store, now=10.0) is True
