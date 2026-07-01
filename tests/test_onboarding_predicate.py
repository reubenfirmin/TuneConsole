import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import onboarding


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema(); return s


def _synced_thin(store):
    # a sync has run (so onboarding applies) but the model is thin: low coverage, no modes
    store.set_setting("last_sync_at", "999")
    store.conn.execute("INSERT INTO tracks (identity_key, title, artist) VALUES ('a|x','T','A')")
    store.conn.execute("UPDATE tracks SET first_enriched_at=1 WHERE identity_key='a|x'")
    store.conn.commit()


def test_active_when_synced_and_thin(store):
    _synced_thin(store)
    assert onboarding.onboarding_active(store, now=10.0) is True


def test_inactive_before_any_sync(store):
    assert onboarding.onboarding_active(store, now=10.0) is False


def test_inactive_when_dismissed(store):
    _synced_thin(store)
    store.set_setting("onboard_dismissed", "1")
    assert onboarding.onboarding_active(store, now=10.0) is False


def test_inactive_when_enough_feedback(store, monkeypatch):
    _synced_thin(store)
    monkeypatch.setattr(onboarding, "feedback_count", lambda s: 999)
    assert onboarding.onboarding_active(store, now=10.0) is False


def test_inactive_when_taste_ready(store, monkeypatch):
    _synced_thin(store)
    # genre coverage high AND a mode present -> ready
    monkeypatch.setattr(onboarding, "_genre_coverage", lambda s: 1.0)
    store.modes.replace_modes([{"mode_id": 1, "label": "m", "families": [["house", 1]],
                                "centroid": np.array([1.0], dtype=np.float32), "size": 5,
                                "rep_keys": []}], retired_ids=[], now=1.0)
    assert onboarding.onboarding_active(store, now=10.0) is False


def test_warmup_progress_scales_and_caps(store):
    assert onboarding.warmup_progress(store) == 0           # nothing enriched -> 0%
    store.conn.executescript(
        "INSERT INTO tracks (identity_key,title,artist,genre) VALUES ('a|1','t','a','house');"
        "INSERT INTO tracks (identity_key,title,artist,genre) VALUES ('a|2','t','a','');"
        "UPDATE tracks SET first_enriched_at=1;")
    store.conn.commit()
    assert 0 < onboarding.warmup_progress(store) <= 100     # partial coverage -> partial progress
    store.conn.execute("UPDATE tracks SET genre='house'")   # full coverage
    store.conn.commit()
    assert onboarding.warmup_progress(store) == 100         # caps at 100
