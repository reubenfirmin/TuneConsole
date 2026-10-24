"""Persisted drafts can be normalized without acquiring candidates or starting a job."""
from copy import deepcopy
import json

from yt_playlist.rec import road_trip, road_trip_axes as axes
from yt_playlist.rec.road_trip_neighborhoods import ArtistNeighborhoods
from yt_playlist.rec.road_trip_state import draft_tracks
from yt_playlist.util import genre_map


def track(video_id, artist="Seed", genre="Rock", year=1995, source="theirs", **extra):
    return {"video_id": video_id, "artist": artist, "title": video_id, "album": "", "thumbnail": None,
            "genre": genre, "family": genre_map.family(genre), "year": year, "decade": str(year // 10 * 10),
            "duration": 240, "source": source, **extra}


def test_saved_legacy_json_keeps_track_order_and_metadata_without_store_reads():
    rows = [track("owned", source="mine"), track("passenger")]
    state = json.loads(json.dumps({
        "own_pct": 50, "inputs": {"artists": [], "genres": []}, "picked": rows,
        "picks": ["owned", "passenger"], "pool": deepcopy(rows), "saved_playlist_id": "PL_saved",
        "mix_version": 1, "custom_saved_metadata": {"retain": True},
        "targets": {"theirs": {"genre:Rock": .4}},
        "axes": {"theirs": [{"kind": "era", "key": "era:2000", "name": "2000"},
                            {"kind": "genre", "key": "genre:Rock", "name": "Rock"},
                            {"kind": "era", "key": "era:1990", "name": "1990"}]},
    }))
    # A store with no repository interfaces makes accidental repicking fail immediately.
    assert road_trip.normalized(state, object()) is state
    assert [c["video_id"] for c in draft_tracks(state)] == ["owned", "passenger"]
    assert state["picks"] == ["owned", "passenger"]
    assert [c["video_id"] for c in state["pool"]] == ["passenger"]
    assert state["pool"][0]["cluster_genre"] == ""
    assert state["mix_version"] == 1
    assert state["saved_playlist_id"] == "PL_saved"
    assert state["custom_saved_metadata"] == {"retain": True}
    assert [a["key"] for a in state["axes"]["theirs"]] == ["genre:Rock", "era:1990", "era:2000"]
    assert state["targets"]["theirs"] == {"genre:Rock": .4}
    before = json.dumps(state, sort_keys=True)
    road_trip.normalized(state, object())
    assert json.dumps(state, sort_keys=True) == before


def test_neighborhood_repair_needs_only_recipe_context_and_pool():
    neighborhoods = ArtistNeighborhoods(["Seed"], ["Jazz"], {
        "Seed": {"genre": "Alternative Rock", "year": 1995, "related": ["Peer"]}})
    pool = [track("seed", genre="Alternative Rock", year=2025, input="Seed"),
            track("peer", artist="Peer", genre="Indie Rock", input="Alternative Rock"),
            track("revival", artist="Peer", genre="Indie Rock", year=2025, input="Alternative Rock"),
            track("jazz", artist="Guest", genre="Jazz", input="Jazz", family="jazz")]
    repaired = neighborhoods.repair(pool)
    assert repaired.changed
    assert [c["video_id"] for c in repaired.pool] == ["seed", "peer", "jazz"]
    assert [c["cluster_genre"] for c in repaired.pool] == ["Alternative Rock", "Alternative Rock", ""]
    assert repaired.removed_inputs == {"Alternative Rock"}
    assert list(neighborhoods.for_genre("alt rock")) == ["Seed"]
    assert not neighborhoods.for_genre("Indie Rock")


def test_balancing_does_not_require_or_mutate_a_draft_or_candidate_snapshot():
    pool = [track("rock"), track("jazz", genre="Jazz", family="jazz")]
    before = deepcopy(pool)
    rows = axes.merge([], pool, pool)
    targets = {"era:1990": 1.0}
    axes.rebalance(rows, targets, "genre:Rock", .25)
    rows = axes.normalize(rows, targets)
    assert targets == {"era:1990": 1.0, "genre:Rock": .25, "genre:Jazz": .75}
    assert [(a["key"], a["balance_pct"]) for a in rows] == [
        ("genre:Rock", 25), ("genre:Jazz", 75), ("era:1990", 100)]
    assert [a["share"] for a in rows] == [.5, .5, 1.0]
    assert axes.available_seconds(pool, "genre:Rock") == 240
    assert pool == before
