"""Fixed candidate snapshots characterize allocation without providers or a database."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import random

import pytest

from yt_playlist.rec.road_trip_mix import MixRequest, construct


def track(number, source="mine", genre="Jazz", year=1995, duration=240):
    return {"video_id": str(number), "title": f"Song {number}", "artist": f"Artist {number}",
            "source": source, "genre": genre, "family": genre.lower(),
            "year": year, "decade": str(year // 10 * 10), "duration": duration,
            "score": 1.0, "fam": .5, "cluster_genre": ""}


def request(rows, **changes):
    return replace(MixRequest(rows, 50, 50, 48, 7, [], [], {}, [], [], False), **changes)


def mix(spec):
    return construct(spec, random.Random(spec.seed))


def test_fixed_inputs_preserve_three_way_shares_and_order_without_mutating_snapshot():
    rows = ([track(i) for i in range(30)]
            + [track(i, genre="Rock") for i in range(30, 60)]
            + [track(i, source="theirs", genre="Pop") for i in range(60, 90)])
    spec = request(rows, genres=["Rock"], target_minutes=72, previous=["0", "30", "60"])
    before = deepcopy(spec)
    first, second = mix(spec), mix(spec)
    assert first == second
    assert spec == before
    assert first.blend_available
    assert {side: len(picked) for side, picked in first.picked.items()} == {
        "yours": 6, "theirs": 6, "blend": 6}
    assert sum(row["duration"] for row in first.ordered) == 72 * 60
    assert len({row["video_id"] for row in first.ordered}) == 18
    assert {row["video_id"] for row in first.ordered} == {
        row["video_id"] for picked in first.picked.values() for row in picked}
    assert mix(replace(spec, seed=8)).ordered != first.ordered


def test_genre_and_era_quotas_apply_together():
    rows = [track(f"{genre}-{year}-{i}", genre=genre, year=year)
            for genre in ("Rock", "Jazz") for year in (1995, 2015) for i in range(10)]
    result = mix(request(rows, own_pct=100, targets={"mine": {
        "genre:Rock": .5, "genre:Jazz": .5, "era:1990": .5, "era:2010": .5}}))
    assert Counter(row["genre"] for row in result.ordered) == {"Rock": 6, "Jazz": 6}
    assert Counter(row["decade"] for row in result.ordered) == {"1990": 6, "2010": 6}


def test_bans_zero_weights_and_duplicate_rows_do_not_fill_slots():
    rows = [track(i) for i in range(20)]
    rows += [deepcopy(rows[0]), {**track("unranked"), "score": 0}]
    result = mix(request(rows, own_pct=100, banned=["0", "1"]))
    ids = [row["video_id"] for row in result.ordered]
    assert len(ids) == len(set(ids)) == 12
    assert not {"0", "1", "unranked"} & set(ids)


@pytest.mark.parametrize("library_first", [False, True])
def test_cross_group_duplicates_belong_to_library_before_shares_and_quotas(library_first):
    library = [track("shared", genre="Rock"), track("own", genre="Jazz")]
    passenger = [{**track("shared", source="theirs", genre="Pop"), "duration": 600},
                 track("guest", source="theirs", genre="Pop")]
    rows = library + passenger if library_first else passenger + library
    spec = request(rows, genres=["Rock"], target_minutes=12,
                   targets={"mine": {"genre:Rock": .5, "genre:Jazz": .5}})
    before = deepcopy(spec)
    result = mix(spec)
    grouped = [row for group in result.groups.values() for row in group]
    assert Counter(row["video_id"] for row in grouped) == {"shared": 1, "own": 1, "guest": 1}
    assert result.groups["blend"] == [library[0]]
    assert result.groups["theirs"] == [passenger[1]]
    assert Counter(row["video_id"] for row in result.ordered) == {"shared": 1, "own": 1, "guest": 1}
    assert {side: len(rows) for side, rows in result.picked.items()} == {"yours": 1, "blend": 1, "theirs": 1}
    assert sum(row["duration"] for row in result.ordered) == 12 * 60
    assert spec == before


@pytest.mark.parametrize("building,expected_count,missing_minutes", [(True, 5, 21), (False, 10, 18)])
def test_missing_side_reports_shortfall_and_final_mix_fills_from_available_side(building, expected_count, missing_minutes):
    rows = [track(i, source="theirs") for i in range(20)]
    result = mix(request(rows, target_minutes=40, building=building))
    assert len(result.ordered) == expected_count
    # An absent side uses the existing 210-second estimate, while available rows are 240 seconds.
    assert result.short == {"mine": missing_minutes}
    assert not result.blend_available


def test_unknown_durations_use_estimate_and_empty_pool_reports_shortfall():
    result = mix(request([track(i, duration=None) for i in range(20)], own_pct=100, target_minutes=35))
    assert len(result.ordered) == 10
    empty = mix(request([], own_pct=100, target_minutes=35))
    assert empty.ordered == []
    assert empty.short == {"mine": 35}
