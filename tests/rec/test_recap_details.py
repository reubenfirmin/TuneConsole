"""Evidence gates matter more than the prose: no fabricated discoveries or habits."""
from copy import deepcopy

import pytest

from yt_playlist.rec.recap_details import build, new_this_month, screens
from yt_playlist.rec.recap_metrics import baselines, month_bounds, profile
from yt_playlist.repos.recap import RecapRepo
from yt_playlist.util.matching import identity_key


@pytest.fixture
def data():
    start, _ = month_bounds("2026-01")
    end, _ = month_bounds("2026-10")
    return {"plays": [], "first": {"old": start}, "spans": {(1, "live"): (start, end)},
            "meta": {}, "familiar_artists": set(), "albums": [], "playlists": []}


def add_plays(data, month, *, days=10, per_day=6, source="live", offset=0, identity=1,
              artist="Regular", genre="house", year="1994", album=None, variants=6):
    start, _ = month_bounds(month)
    for day in range(days):
        for play in range(per_day):
            key = f"{artist}:{play % variants}"
            at = start + (day + offset) * 86400 + play * 240 + 3600
            data["plays"].append({"identity_id": identity, "identity_key": key,
                                  "played_at": at, "source": source, "video_id": key})
            data["first"][key] = min(at, data["first"].get(key, at))
            data["meta"][key] = {"title": key, "artist": artist, "genre": genre,
                                  "mb_year": year, "album_browse_id": album}


def familiar_months(data, months=("2026-04", "2026-05", "2026-06", "2026-09"), **kwargs):
    for month in months:
        add_plays(data, month, **kwargs)
    data["familiar_artists"].add("regular")


def findings(beats, kind):
    return next((b["findings"] for b in beats if b["kind"] == kind), [])


def test_every_screen_is_independently_optional(data):
    assert screens(data, "2026-09") == []
    add_plays(data, "2026-09", per_day=1, days=3)
    data["familiar_artists"].add("regular")
    assert screens(data, "2026-09") == []


def test_new_artists_require_four_actual_plays_and_prior_history(data):
    add_plays(data, "2026-09", days=1, per_day=3)
    assert new_this_month(data, "2026-09") is None
    add_plays(data, "2026-09", days=1, per_day=1)
    beat = new_this_month(data, "2026-09")
    assert beat["groups"][0]["entries"][0]["detail"] == "4 plays"
    data["familiar_artists"].add("regular")
    assert new_this_month(data, "2026-09") is None


def test_first_tracking_month_and_previously_heard_artists_are_not_discoveries(data):
    add_plays(data, "2026-09")
    del data["first"]["old"]
    assert new_this_month(data, "2026-09") is None
    data["first"]["old"] = month_bounds("2026-01")[0]
    data["first"]["Regular:0"] = month_bounds("2026-08")[0]
    assert new_this_month(data, "2026-09") is None


def test_new_album_counts_distinct_tracks_against_full_tracklist(data):
    add_plays(data, "2026-09", per_day=3, days=2)
    data["familiar_artists"].add("regular")
    data["albums"] = [{"title": "Release", "artist": "Regular", "browse_id": "MPRE_1",
                       "keys": {f"Regular:{i}" for i in range(5)}}]
    beat = new_this_month(data, "2026-09")
    assert beat["groups"][0]["entries"][0]["detail"] == "3 of 5 tracks played"
    data["albums"][0]["keys"].add("Regular:5")  # now only half the release
    assert new_this_month(data, "2026-09") is None
    data["albums"][0]["keys"].remove("Regular:5")
    data["first"]["Regular:4"] = month_bounds("2026-07")[0]
    assert new_this_month(data, "2026-09") is None


def test_three_comparable_prior_months_required_and_missing_months_not_zero_filled(data):
    familiar_months(data, months=("2026-05", "2026-06", "2026-09"))
    assert baselines(data, "2026-09")[1] == []
    assert screens(data, "2026-09") == []
    add_plays(data, "2026-04")
    _, past = baselines(data, "2026-09")
    assert [p.month for p in past] == ["2026-04", "2026-05", "2026-06"]


@pytest.mark.parametrize("change", ["identity", "partial", "legacy"])
def test_other_identities_partial_months_and_unknown_sources_suppress_comparisons(data, change):
    familiar_months(data)
    for row in data["plays"]:
        if month_bounds("2026-04")[0] <= row["played_at"] < month_bounds("2026-07")[0]:
            if change == "identity":
                row["identity_id"] = 2
            elif change == "legacy":
                row["source"] = ""
    data["spans"][(1, "takeout")] = data["spans"][(1, "live")]
    data["spans"][(1, "")] = data["spans"][(1, "live")]
    data["spans"][(2, "live")] = data["spans"][(1, "live")]
    if change == "partial":
        data["spans"][(1, "live")] = (month_bounds("2026-06")[0] + 1, month_bounds("2026-10")[0])
    assert not findings(screens(data, "2026-09"), "different")
    assert not findings(screens(data, "2026-09"), "same")


def test_imported_history_supports_music_comparisons_and_recurring_artists(data):
    data["spans"][(1, "takeout")] = data["spans"][(1, "live")]
    for month in ("2026-04", "2026-05", "2026-06"):
        add_plays(data, month, artist="Background", days=10, per_day=10, variants=50,
                  source="takeout")
        add_plays(data, month, artist="Favorite", days=4, per_day=3, variants=3,
                  source="takeout")
    add_plays(data, "2026-09", artist="Repeated song", days=5, per_day=10, variants=1)
    add_plays(data, "2026-09", artist="Favorite", days=4, per_day=3, variants=3)
    data["familiar_artists"].update({"background", "favorite", "repeated song"})
    current, previous = baselines(data, "2026-09")
    assert len(previous) == 3
    assert all(p.source_signature != current.source_signature for p in previous)
    beats = screens(data, "2026-09")
    assert any(f["metric"] == "repetition" for f in findings(beats, "different"))
    habit = next(f for f in findings(beats, "same") if f["metric"] == "artist:Favorite")
    assert habit["detail"] == "12 plays over 4 days. You keep coming back."
    # A switch in capture can change total plays, active days, and observed listening runs.
    assert not any(f["metric"] in {"activity", "days", "intensity", "sessions", "album_runs"}
                   for kind in ("different", "same") for f in findings(beats, kind))
    # A positive habit still needs support in at least 75% of earlier months.
    start, end = month_bounds("2026-04")
    data["plays"] = [p for p in data["plays"] if not (
        start <= p["played_at"] < end and p["identity_key"].startswith("Favorite:"))]
    assert not any(f["metric"] == "artist:Favorite" for f in findings(screens(data, "2026-09"), "same"))


def test_sparse_activity_compared_to_real_baseline_and_positive_habits(data):
    familiar_months(data, months=("2026-04", "2026-05", "2026-06"), days=20, per_day=15)
    add_plays(data, "2026-09", days=5, per_day=5)
    beats = screens(data, "2026-09")
    changed = findings(beats, "different")
    assert any(f["title"] == "A quieter listening month" for f in changed)
    assert any("25 plays this month" in f["detail"] for f in changed)
    assert all(len(b.get("findings", [])) <= 3 for b in beats)
    assert not any(f["metric"] == "playlist_creation" for f in findings(beats, "same"))


def test_activity_uses_matching_capture_months_without_discarding_other_history(data):
    add_plays(data, "2026-03", days=5, per_day=4, source="takeout")
    data["spans"][(1, "takeout")] = data["spans"][(1, "live")]
    familiar_months(data, months=("2026-04", "2026-05", "2026-06"), days=20, per_day=15)
    add_plays(data, "2026-09", days=5, per_day=5)
    assert len(baselines(data, "2026-09")[1]) == 4
    changed = findings(screens(data, "2026-09"), "different")
    assert any(f["metric"] == "activity" and f["title"] == "A quieter listening month" for f in changed)


def test_month_lengths_are_normalized_and_future_content_cannot_affect_results(data):
    for month in ("2026-04", "2026-05", "2026-06", "2026-09"):
        since, until = month_bounds(month)
        add_plays(data, month, days=round((until - since) / 86400), per_day=6)
    data["familiar_artists"].add("regular")
    before = screens(data, "2026-09")
    assert not findings(before, "different")
    assert any(f["metric"] == "activity" for f in findings(before, "same"))
    add_plays(data, "2026-10", days=10, per_day=100, artist="Future")
    assert screens(data, "2026-09") == before


def test_natural_variation_and_lack_of_evidence_do_not_become_habits(data):
    for month, per_day in (("2026-04", 3), ("2026-05", 10), ("2026-06", 20), ("2026-09", 12)):
        add_plays(data, month, per_day=per_day)
    data["familiar_artists"].add("regular")
    beats = screens(data, "2026-09")
    assert not any(f["metric"] == "activity" for f in findings(beats, "different"))
    assert not any(f["metric"] == "activity" for f in findings(beats, "same"))


def test_music_shares_need_metadata_and_repetition_across_days(data, monkeypatch):
    monkeypatch.setattr("yt_playlist.util.genre_map.family", lambda g: g)
    familiar_months(data)
    add_plays(data, "2026-09", per_day=12, artist="Different", genre="jazz", year="2021")
    data["familiar_artists"].add("different")
    beats = screens(data, "2026-09")
    assert any(f["metric"] == "genre:jazz" for f in findings(beats, "different"))
    # Unknown metadata cannot be interpreted as a change in preference.
    for card in data["meta"].values():
        card.pop("genre", None)
        card.pop("mb_year", None)
    assert not any(f["metric"].startswith(("genre:", "era:"))
                   for f in findings(screens(data, "2026-09"), "different"))


def test_album_runs_require_three_distinct_consecutive_tracks(data):
    add_plays(data, "2026-09", days=5, per_day=6, album="album", variants=1)
    p = profile("2026-09", data["plays"], data["meta"], data["spans"], [])
    assert p.album_run_plays == 0
    assert p.sessions == (6,) * 5
    fresh = deepcopy(data)
    fresh["plays"] = []
    add_plays(fresh, "2026-09", days=5, per_day=6, album="album", variants=6)
    p = profile("2026-09", fresh["plays"], fresh["meta"], fresh["spans"], [])
    assert p.album_run_plays == 30


def test_shorter_listening_runs_are_detected_without_a_volume_change(data):
    familiar_months(data)
    for row in data["plays"]:
        if row["played_at"] >= month_bounds("2026-09")[0]:
            slot = int(row["identity_key"].split(":")[1])
            row["played_at"] += slot * 2000
    changed = findings(screens(data, "2026-09"), "different")
    assert any(f["metric"] == "sessions" and f["title"] == "Shorter runs of listening" for f in changed)
    assert not any(f["metric"] == "activity" for f in changed)


def test_new_album_metadata_cannot_fabricate_a_change_in_album_listening(data):
    familiar_months(data)
    # Only the target month's songs have album metadata; earlier coverage is unknown.
    add_plays(data, "2026-09", artist="New", album="MPRE")
    data["familiar_artists"].add("new")
    assert not any(f["metric"] == "album_runs" for f in findings(screens(data, "2026-09"), "different"))


def test_repo_excludes_generated_plays_and_requires_real_playlist_dates(store):
    iid = store.upsert_identity("main", "cred", None, True)
    since, until = month_bounds("2026-09")
    for ytm, date, source, generated in (("real", since, "youtube", False),
                                         ("unknown", None, None, False),
                                         ("generated", since, "local", True)):
        pid = store.upsert_playlist(iid, ytm, ytm, 0, "h", since)
        store.conn.execute("UPDATE playlists SET created_at=?, created_at_source=? WHERE id=?",
                           (date, source, pid))
        if generated:
            store.set_playlist_group(ytm, "Generated")
    store.conn.executemany(
        "INSERT INTO play_events(identity_id, identity_key, played_at, source, playlist_ytm_id) "
        "VALUES (1,?,?,?,?)", [("real", since + 1, "live", "real"),
                              ("generated", since + 2, "live", "generated")])
    store.conn.commit()
    material = RecapRepo(store).snapshot(since, until)
    assert [p["identity_key"] for p in material["plays"]] == ["real"]
    assert [p["ytm_playlist_id"] for p in material["playlists"]] == ["real"]
    beat = build(store, "2026-09")[0]
    assert beat["groups"] == [{"label": "Playlists", "total": 1, "entries": [
        {"title": "real", "detail": "0 songs · 1 play",
         "href": "https://music.youtube.com/playlist?list=real"}]}]


def test_new_playlist_counts_do_not_multiply_copies_or_attribute_song_matches(store):
    since, until = month_bounds("2026-09")
    for label in ("main", "other"):
        iid = store.upsert_identity(label, "cred", None, label == "main")
        store.upsert_playlist(iid, "real", "New mix", 4, "h", since, created_at=since)
    # The same song can be played from another playlist or with no known playlist.
    # Only the two events attributed to this list during the recap month count.
    store.conn.executemany(
        "INSERT INTO play_events(identity_id,identity_key,played_at,source,playlist_ytm_id) "
        "VALUES (?, 'song', ?, 'live', ?)",
        [(1, since - 1, "real"), (1, since + 1, "real"), (2, since + 2, "real"),
         (1, since + 3, "another"), (1, since + 4, None), (1, until, "real")])
    store.conn.commit()
    beat = build(store, "2026-09")[0]
    playlists = next(g for g in beat["groups"] if g["label"] == "Playlists")
    assert playlists["total"] == 1
    assert playlists["entries"][0]["detail"] == "4 songs · 2 plays"


@pytest.mark.parametrize("songs,expected", [(1, "1 song · 0 plays"), (None, "0 plays")])
def test_new_playlist_distinguishes_unknown_size_from_no_recorded_plays(data, songs, expected):
    since, _ = month_bounds("2026-09")
    data["playlists"] = [{"title": "New mix", "ytm_playlist_id": "real",
                          "created_at": since, "track_count": songs}]
    beat = new_this_month(data, "2026-09")
    assert beat["groups"][0]["entries"][0]["detail"] == expected


def test_repo_preserves_true_counts_with_video_variants_and_snapshot_familiarity(store):
    store.upsert_identity("main", "cred", None, True)
    key = identity_key("Song", "Artist")
    for video in ("v1", "v2"):
        store.upsert_track(video, "Song", "Artist", "Release", 100)
    since, until = month_bounds("2026-09")
    store.conn.execute("INSERT INTO history_snapshots(identity_id,taken_at) VALUES (1,?)", (since - 86400,))
    store.conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,?)", (key,))
    store.conn.executemany("INSERT INTO play_events(identity_id,identity_key,played_at,source) "
                           "VALUES (1,?,?,'live')", [(key, since + i) for i in range(4)])
    material = RecapRepo(store).snapshot(since, until)
    assert len(material["plays"]) == 4
    assert material["first"][key] == since - 86400


def test_worker_bakes_all_three_screens_and_replaces_overlapping_old_beats(store, data):
    from yt_playlist.rec import trend_rollups
    store.upsert_identity("main", "cred", None, True)
    familiar_months(data)
    add_plays(data, "2026-09", per_day=12, artist="New artist", genre="jazz", album="MPRE")
    for key, card in data["meta"].items():
        from tests.conftest import seed_catalog_track
        seed_catalog_track(store,key,card["title"],card["artist"],genre=card["genre"],album_browse_id=card["album_browse_id"])
    rows = [(p["identity_key"], p["played_at"]) for p in data["plays"]]
    rows += [("old", month_bounds("2026-01")[0]), ("future", month_bounds("2026-10")[0])]
    store.conn.executemany("INSERT INTO play_events(identity_id,identity_key,played_at,source) "
                           "VALUES (1,?,?,'live')", rows)
    store.conn.commit()
    store.collection.cache_album_tracklist("MPRE", "Release", "New artist", None,
                                          [f"New artist:{i}" for i in range(6)])
    payload = trend_rollups.build(store, month_bounds("2026-10")[0] + 86400)
    kinds = [b["kind"] for b in payload["story"]["beats"]]
    assert {"new_this_month", "different", "same"} <= set(kinds)
    assert not {"new", "diversity", "revival"} & set(kinds)
    assert store.get_proposals("trend_rollups")["story"] == payload["story"]
