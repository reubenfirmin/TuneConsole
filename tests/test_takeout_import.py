"""#61 end-to-end import: parse -> match (videoId exact, title/artist fallback) -> both stores."""
import json

from yt_playlist.core.store import Store
from yt_playlist.library.takeout import import_takeout, seed_discovery_from_unmatched

DAY = 86400.0


def _store():
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    s.upsert_track("vidA", "Suave", "Danny Wabbit", "", 200)
    s.upsert_track("vidB", "Pulse", "Klockworks", "", 300)
    return s


def _raw(entries):
    return json.dumps(entries)


def _e(title, artist, vid, iso):
    return {"header": "YouTube Music", "title": f"Watched {title}",
            "titleUrl": f"https://music.youtube.com/watch?v={vid}" if vid else None,
            "subtitles": [{"name": f"{artist} - Topic"}], "time": iso}


def test_video_id_match_and_title_fallback_and_unmatched():
    s = _store()
    entries = [_e("Suave", "Danny Wabbit", "vidA", "2024-01-01T10:00:00Z"),      # videoId match
               _e("Pulse", "Klockworks", "OTHERVID", "2024-01-02T10:00:00Z"),    # falls back to title/artist
               _e("Ghost Song", "Nobody", "NOVID9", "2024-01-03T10:00:00Z")]     # unmatched
    report = import_takeout(s, _raw(entries))
    assert report["matched"] == 2 and report["unmatched"] == 1
    assert report["plays_added"] == 2 and report["events_added"] == 2
    assert report["unmatched_artists"] == {"Nobody": 1}
    assert report["span_days"] >= 2


def test_reimport_adds_nothing():
    s = _store()
    entries = [_e("Suave", "Danny Wabbit", "vidA", "2024-01-01T10:00:00Z")]
    import_takeout(s, _raw(entries))
    report = import_takeout(s, _raw(entries))
    assert report["plays_added"] == 0 and report["events_added"] == 0


def test_import_coexists_with_prior_live_day():
    s = _store()
    # live capture already recorded a play of Suave on 2024-01-01 (different instant: 09:00 UTC,
    # vs. the takeout entry's 10:00 UTC below; 1704103200.0 would be the *same* instant as the
    # takeout entry and wrongly dedupe events_added to 0, defeating the point of this test)
    s.record_play_event(1, "suave|danny wabbit", "vidA", 1704099600.0)
    s.record_history_plays(1, 1704099600.0, ["suave|danny wabbit"])
    report = import_takeout(s, _raw([_e("Suave", "Danny Wabbit", "vidA", "2024-01-01T10:00:00Z")]))
    assert report["plays_added"] == 0            # same UTC day in the day model
    assert report["events_added"] == 1           # distinct instant in the event stream


def test_no_identity_configured_returns_error_shape():
    s = Store(":memory:"); s.init_schema()
    report = import_takeout(s, _raw([]))
    assert report == {"error": "no identity configured"}


def test_seed_discovery_from_unmatched_respects_min_plays():
    s = _store()
    unmatched = {"Prolific Nobody": 3, "One-Off Nobody": 2}
    n = seed_discovery_from_unmatched(s, unmatched, now=1700000000.0)
    assert n == 1
    pool = {a["artist"] for a in s.get_discovered_artists()}
    assert pool == {"Prolific Nobody"}


def test_seed_discovery_from_unmatched_default_threshold_is_three():
    s = _store()
    n = seed_discovery_from_unmatched(s, {"Exactly Three": 3}, now=1700000000.0)
    assert n == 1
    assert {a["artist"] for a in s.get_discovered_artists()} == {"Exactly Three"}


def test_seed_discovery_from_unmatched_honors_custom_min_plays():
    s = _store()
    n = seed_discovery_from_unmatched(s, {"Two Plays": 2}, now=1700000000.0, min_plays=2)
    assert n == 1
    assert {a["artist"] for a in s.get_discovered_artists()} == {"Two Plays"}


def test_seeding_never_clobbers_existing_pool_entries():
    # A taste-bridge entry carries rich because/fits; re-seeding the same artist from Takeout would
    # overwrite them with empty fields (upsert clobbers on conflict), so existing names are skipped.
    s = _store()
    s.upsert_discovered_artist("Bridge Artist", 9.5, ["anchor a"], ["Playlist A"], None, 1700000000.0)
    n = seed_discovery_from_unmatched(s, {"Bridge Artist": 50, "New Nobody": 5}, now=1700000001.0)
    assert n == 1
    pool = {a["artist"]: a for a in s.get_discovered_artists()}
    assert set(pool) == {"Bridge Artist", "New Nobody"}
    assert pool["Bridge Artist"]["score"] == 9.5          # untouched, not replaced by play count
