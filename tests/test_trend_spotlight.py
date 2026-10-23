"""The Home trend-spotlight card was removed when the Trends analytics tab was replaced by the monthly
recap (surfaced via the recap nag card; see tests/test_trends_recap_route.py). What remains here is the
rollup-side stamp: build() still computes a spotlight candidate and, when the month-in-review wins the
cascade, stamps trend_spotlight_review_month so the month-rollover detector does not re-fire."""
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import trend_rollups as tr


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


@pytest.fixture(autouse=True)
def _toy_genres(monkeypatch):
    from yt_playlist.util import genre_map
    monkeypatch.setattr(genre_map, "family", lambda g: (g or "").lower())
    monkeypatch.setattr(genre_map, "family_distance", lambda a, b: 0.0 if a == b else 0.5)


def _snap(store, day, keys):
    """Record independent observations on an explicit UTC calendar date."""
    store.history.record_history_plays(1, day * 86400.0, keys, source_timezone="UTC")


def _track(store, key, artist, genre=""):
    from tests.conftest import seed_catalog_track
    seed_catalog_track(store,key,"T"+key,artist,video_id="v"+key,genre=genre)


def test_build_stamps_spotlight_review_month(store):
    """When build() computes a month-in-review recap AND it's the candidate that actually wins the
    spotlight cascade (no insight outranks it here), it stamps trend_spotlight_review_month so the next
    build() doesn't re-fire the month-rollover detector for the same month. now=59*86400.0 (Mar 1,
    1970-03) so Feb (the day-31 snapshot) is a genuinely completed past month under month_review's own
    `now` argument (not real wall-clock time.time())."""
    store.identities.upsert_identity("me", "c", None, True)   # identity id=1, referenced by history_snapshots FK
    _track(store, "k1", "A1", "house")
    for d in (0, 1, 2, 3):
        _snap(store, d, ["k1"])
    _snap(store, 31, ["k1"])
    now = 59 * 86400.0

    assert store.settings.get_setting("trend_spotlight_review_month") is None   # sanity: unset before build()
    payload = tr.build(store, now)
    assert payload["review"]["month"] == "1970-02"
    assert payload["spotlight"]["signature"] == "month_review:1970-02"   # review actually won the slot
    assert store.settings.get_setting("trend_spotlight_review_month") == "1970-02"
