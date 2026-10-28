"""Every recap numerator selects the same account-calendar month as its total."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from yt_playlist.rec import trend_rollups, trend_payload, trend_statistics as stats
from yt_playlist.rec.recap_metrics import month_bounds, profile
from yt_playlist.repos.recap import RecapRepo


@pytest.mark.parametrize("zones", [
    ["America/Costa_Rica"], ["Pacific/Kiritimati"], ["America/New_York"],
    ["America/Costa_Rica", "Pacific/Kiritimati"],
])
@pytest.mark.parametrize("month", ["2026-03", "2025-12"])
def test_recap_podium_personality_and_details_share_account_calendar(store, zones, month):
    since, until = month_bounds(month)
    first = datetime.fromtimestamp(since, timezone.utc).replace(tzinfo=None)
    end = datetime.fromtimestamp(until, timezone.utc).replace(tzinfo=None)
    for index, zone in enumerate(zones):
        iid = store.identities.upsert_identity(f"Account {index}", f"bridge{index}", None, index == 0)
        video = f"video{index}"
        store.tracks.upsert_track(video, video, f"Artist {index}", None, 200)
        key = store.tracks.identity_key_for_video(video)
        for local in (first + timedelta(minutes=30), first + timedelta(days=14, minutes=30),
                      end - timedelta(minutes=30), first - timedelta(minutes=30),
                      end + timedelta(minutes=30)):
            at = local.replace(tzinfo=ZoneInfo(zone)).timestamp()
            store.history.record_play_event(iid, key, video, at, source_timezone=zone)
    payload = trend_rollups.build(store, until + 4 * 86400)
    day_counts = trend_rollups.day_counts_source(store)
    meta = store.trends.track_meta()
    months = stats.compute_months(day_counts, meta, store.trends.first_play_map("artist"))
    review = trend_payload.month_review(months, store, payload["built_at"], day_counts, meta)
    assert review["plays"] == 3 * len(zones)
    assert sum(a["plays"] for a in review["top_artists"]) == review["plays"]
    assert review["top_track"]["plays"] == 3
    assert review["listen_days"] == 3
    pers = next(b for b in payload["story"]["beats"] if b["kind"] == "personality")
    assert pers["axes"]["rhythm"] == 1
    data = RecapRepo(store).snapshot(since, until)
    assert len(data["plays"]) == review["plays"]
    measured = profile(month, data["plays"], data["meta"], data["spans"], [])
    assert measured.plays == review["plays"] and measured.days == review["listen_days"]
    # The instant-based API remains available to callers whose cutoffs are real times.
    for_calendar = store.trends.ledger_track_plays(since, until, calendar=True)
    assert sum(for_calendar.values()) == review["plays"]


def test_shared_playlist_creation_uses_the_earliest_observers_calendar(store):
    created = datetime(2026, 4, 1, 0, 30, tzinfo=timezone.utc).timestamp()
    store.tracks.upsert_track("v", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("v")
    for index, zone in enumerate(("America/Costa_Rica", "Pacific/Kiritimati")):
        iid = store.identities.upsert_identity(str(index), f"bridge{index}", None, index == 0)
        store.history.record_play_event(iid, key, "v", created, source_timezone=zone)
        store.playlists.upsert_playlist(iid, "shared", "Shared", index + 2, "hash", created,
                                        created_at=created + index * 3600)
    playlists = RecapRepo(store).snapshot(*month_bounds("2026-03"))["playlists"]
    assert playlists == [{"ytm_playlist_id": "shared", "title": "Shared",
                          "created_at": created, "track_count": 3, "day": "2026-03-31"}]
