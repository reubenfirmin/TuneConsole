from datetime import datetime, timezone

import pytest

from yt_playlist.rec import trend_rollups, trend_statistics as stats
from yt_playlist.repos.trends import TrendsRepo


@pytest.mark.parametrize("now, expected", [
    ("2026-10-01", "2026-09"), ("2026-10-31", "2026-09"),
    ("2027-01-01", "2026-12"), ("2024-03-01", "2024-02"),
])
def test_last_complete_month(now, expected):
    assert stats.last_complete_month(datetime.fromisoformat(now).replace(tzinfo=timezone.utc).timestamp()) == expected


def test_build_only_publishes_live_recap_and_preserves_first_play(store, monkeypatch):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    store.tracks.upsert_track("video", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("video")
    for day in (0, 1, 2, 31):
        store.history.record_history_plays(iid, day * 86400, [key], source_timezone="UTC")

    def retired(*args, **kwargs):
        pytest.fail("Retired Trends analysis was called")

    for name in ("repetition_series", "catalog_track_keys", "never_played", "track_last_play",
                 "dead_playlists", "rediscover_tracks"):
        monkeypatch.setattr(TrendsRepo, name, retired)
    payload = trend_rollups.build(store, 59 * 86400)
    assert set(payload) == {"built_at", "story"}
    assert store.rec_proposals.get_proposals("trend_rollups") == payload
    assert store.trends.first_play_map("track")[key] == 0
    assert store.settings.get_setting("trend_spotlight_review_month") is None
    story = payload["story"]
    assert story["month"] == "1970-02" and story["month_name"] == "February"
    numbers = next(b for b in story["beats"] if b["kind"] == "numbers")
    assert (numbers["plays"], numbers["listen_days"], numbers["streak"]) == (1, 1, 1)
    assert trend_rollups.build(store, 90 * 86400)["story"] is None


def test_backfill_and_reset_still_rebuild_first_play(store):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    store.history.record_history_plays(iid, 5 * 86400, ["song"], source_timezone="UTC")
    trend_rollups.build(store, 6 * 86400)
    assert store.trends.first_play_map("track")["song"] == 5
    store.history.reset_play_history(iid)
    store.history.record_history_plays(iid, 2 * 86400, ["song"], source_timezone="UTC")
    store.settings.set_setting("takeout_imported_at", "12345")
    trend_rollups.build(store, 6 * 86400)
    assert store.trends.first_play_map("track")["song"] == 2
    assert store.settings.get_setting("trend_first_play_takeout_seen") == "12345"


def test_month_statistics_keep_streak_discovery_diversity_and_calendar(monkeypatch):
    from yt_playlist.util import genre_map
    monkeypatch.setattr(genre_map, "family", lambda g: g)
    monkeypatch.setattr(genre_map, "family_distance", lambda a, b: .3)
    rows = [(2, "a", 2), (3, "a", 1), (4, "b", 2), (31, "b", 1)]
    meta = {"a": ("Known", "house"), "b": ("Fresh", "techno")}
    jan, feb = stats.compute_months(rows, meta, {"Known": 2, "Fresh": 4}, {"Known"})
    assert (jan["plays"], jan["listen_days"], jan["longest_streak"], jan["new_artist_plays"]) == (5, 3, 3, 2)
    assert jan["diversity"] == pytest.approx(2 * .6 * .4 * .3)
    assert feb["new_artist_plays"] == 0
    assert len(stats.month_day_counts(rows, "1970-02")) == 28
    assert sum(d["n"] for d in stats.month_day_counts(rows, "1970-01")) == 5
    assert stats.month_peak_day(rows, meta, "1970-02") == {
        "day": 31, "plays": 1, "artist": "Fresh", "pct": 1}
