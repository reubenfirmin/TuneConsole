"""Genre and era budgets, slider changes and scarce-candidate balance."""
from collections import Counter

import pytest

from yt_playlist.rec import road_trip, road_trip_candidates as candidates
from yt_playlist.rec import road_trip_axes as axes
from tests.conftest import FakeClient
from tests.road_trip_support import _recipe, _stub_pools, no_network

def _share(state, party, axis):
    return next(a["share"] for a in state["axes"][party] if a["key"] == axis)


def test_a_slider_sits_at_the_share_that_genre_actually_has(store, monkeypatch):
    """The complaint this replaced: a 0-2 steering weight that always started centred told you
    nothing about the playlist. A bar now reads as the proportion in the list below it."""
    _stub_pools(monkeypatch, store, own_count=0)          # their side only: one genre, so it is all of it
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=0), 1000.0, seed=5)

    axis = next(a["key"] for a in state["axes"]["theirs"] if a["kind"] == "genre")
    assert _share(state, "theirs", axis) == 1.0
    assert all(a["target"] is None for a in state["axes"]["theirs"])     # nothing pinned yet


def test_sliding_one_decade_to_100_empties_the_others(store, monkeypatch):
    """Dragging a bar to 100% has to mean it: the other decades go, rather than quietly refilling
    the slots the moment that decade runs out. A quota, not a weight."""
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    decades = {r["decade"] for r in state["picked"]}
    assert len(decades) > 1                                  # a mix of decades to start with

    road_trip.set_share(state, "mine", "era:2010", 1.0, store)

    assert {r["decade"] for r in state["picked"]} == {"2010"}
    assert _share(state, "mine", "era:2010") == 1.0
    assert all(a["share"] == 0.0 for a in state["axes"]["mine"]
               if a["kind"] == "era" and a["key"] != "era:2010")


def test_asking_for_more_of_a_genre_than_the_pool_has_goes_and_finds_it(store, monkeypatch):
    """Their side is pulled fresh from YouTube, so a slider is not limited to what was drawn first:
    asking for 100% of a genre queues searches to deepen the pool, and what comes back is filtered
    to that genre so a loose search can't pollute the mix."""
    _stub_pools(monkeypatch, store, own_count=10, other_count=4)
    state = road_trip.build_draft(store, FakeClient(), _recipe(), 1000.0, seed=5)
    axis = next(a["key"] for a in state["axes"]["theirs"] if a["kind"] == "genre")
    assert not state["pending"]

    road_trip.set_share(state, "theirs", axis, 1.0, store)

    assert state["building"] is True
    assert [p["want"] for p in state["pending"]] == [axis] * len(state["pending"])
    assert state["pending"], "a pin the pool can't cover should queue a search, not give up"

    # The queued searches return a mix; only the pinned genre survives into the pool.
    def songs(client, kind, name, cap, store=None, decade=None):
        return ([{"video_id": f"w{i}", "title": f"Wide {i}", "artist": f"Wide Artist {i}",
                  "album": "", "thumbnail": None, "duration": 300,
                  "genre": "synthpop" if i % 2 == 0 else "polka"} for i in range(6)], [])
    monkeypatch.setattr(candidates, "other_input_songs", songs)
    monkeypatch.setattr(candidates, "_facts", lambda t, a: {"popularity": 500, "year": 1985,
                                                           "genre": "synthpop" if "0" in t or "2" in t
                                                           or "4" in t else "polka", "duration": 300})
    while state["pending"]:
        road_trip.add_other_input(state, store, FakeClient(), state["pending"].pop(0))

    widened = [c for c in state["pool"] if c["video_id"].startswith("w")]
    assert widened, "the search results should have reached the pool"
    assert all(axes.matches(c, axis) for c in widened)


def test_a_pin_survives_a_slot_being_crossed_out(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    road_trip.set_share(state, "mine", "era:2010", 1.0, store)
    by_id = {c["video_id"]: c for c in state["pool"]}

    road_trip.reroll_slot(state, store, 0)

    assert {r["decade"] for r in state["picked"]} == {"2010"}


def test_moving_another_slider_rebalances_the_previous_targets(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)

    road_trip.set_share(state, "mine", "era:2010", 0.5, store)
    before = dict(state["targets"]["mine"])
    road_trip.set_share(state, "mine", "era:1990", 0.5, store)

    decades = Counter(r["decade"] for r in state["picked"])
    targets = state["targets"]["mine"]
    assert targets["era:1990"] == 0.5
    assert sum(v for k, v in targets.items() if k.startswith("era:")) == pytest.approx(1)
    assert targets["era:2010"] / targets["era:2000"] == pytest.approx(
        before["era:2010"] / before["era:2000"], abs=.1)
    assert decades["1990"] == sum(decades.values()) // 2


@pytest.mark.parametrize("initial, edited, requested, expected", [
    ([50, 30, 20], 0, 20, [20, 48, 32]),
    ([20, 48, 32], 1, 60, [15, 60, 25]),
    ([50, 30, 20], 0, 100, [100, 0, 0]),
    ([100, 0, 0], 0, 40, [40, 30, 30]),
    ([0, 0, 0], 0, 33, [33, 34, 33]),
])
def test_balance_percentages_sum_to_100(initial, edited, requested, expected):
    rows = [{"key": f"genre:{i}", "kind": "genre", "share": p / 100}
            for i, p in enumerate(initial)]
    state = {"axes": {"theirs": rows},
             "targets": {"theirs": {"era:1990": .7}, "mine": {"genre:Rock": .4}}}
    axes.rebalance(rows, state["targets"]["theirs"], f"genre:{edited}", requested / 100)
    assert [state["targets"]["theirs"][f"genre:{i}"] for i in range(3)] == [p / 100 for p in expected]
    assert sum(expected) == 100
    assert state["targets"]["theirs"]["era:1990"] == .7
    assert state["targets"]["mine"] == {"genre:Rock": .4}


@pytest.mark.parametrize("initial, additions, expected", [
    ([60, 30, 10], ["Funk"], [45, 23, 7, 25]),
    ([80, 20], ["Funk"], [54, 13, 33]),
    ([100, 0], ["Funk"], [67, 0, 33]),
    ([60, 40], ["Funk", "Soul"], [30, 20, 25, 25]),
    ([], ["Funk"], [100]),
    ([], ["Funk", "Soul"], [50, 50]),
])
def test_new_passenger_genres_get_an_equal_share_of_the_new_count(initial, additions, expected):
    state = {"axes": {"theirs": [
        {"key": "genre:" + g, "kind": "genre", "name": g, "share": p / 100}
        for g, p in zip(["Rock", "Pop", "Jazz"], initial)]},
        "targets": {"mine": {"genre:Blues": .6}, "theirs": {"era:1990": .8}}}
    axes.add_genres(state["axes"]["theirs"], state["targets"]["theirs"], additions)
    road_trip.normalized(state)
    rows = [a for a in state["axes"]["theirs"] if a["kind"] == "genre"]
    assert [a["balance_pct"] for a in rows] == expected
    assert sum(a["balance_pct"] for a in rows) == 100
    assert state["targets"]["mine"] == {"genre:Blues": .6}
    assert state["targets"]["theirs"]["era:1990"] == .8
    # Repeated saves and aliases must preserve the existing balance and avoid duplicate rows.
    axes.add_genres(state["axes"]["theirs"], state["targets"]["theirs"], [g.lower() for g in additions])
    road_trip.normalized(state)
    assert [a["balance_pct"] for a in state["axes"]["theirs"] if a["kind"] == "genre"] == expected


@pytest.mark.parametrize("seed_tracks", [20, 1])
@pytest.mark.parametrize("alt_share", [.5, 0])
def test_named_artists_lead_each_genre_with_related_tracks_filling_the_rest(store, seed_tracks, alt_share):
    artists = {"Alternative Rock": ["Weezer", "Staind"], "Indie Rock": ["We Were Promised Jetpacks"]}
    state = road_trip.start_draft(store, _recipe(
        own_pct=0, target_minutes=120, artists=sum(artists.values(), []), genres=[]), 1000, seed=7)
    for genre, seeds in artists.items():
        # Many more, higher-scoring discovery tracks than named-artist tracks: popularity and
        # the general per-artist cap must not drown out the artists the passenger selected.
        catalog = [(name, seed_tracks, .05) for name in seeds]
        catalog += [(f"{genre} Peer {i}", 4, 1) for i in range(12)]
        for name, count, score in catalog:
            for i in range(count):
                candidate = candidates._candidate(f"{name}-{i}", f"Song {i}", name, "", None,
                                                 300, "theirs", genre, 1995)
                candidate["score"] = score
                state["pool"].append(candidate)
    state["targets"]["theirs"] = {"genre:Alternative Rock": alt_share, "genre:Indie Rock": 1 - alt_share,
                                  "era:1990": 1, "era:2000": 0}
    road_trip.finish_draft(state, store, 1000)
    assert state["stats"]["minutes"] == 120
    assert {c["decade"] for c in state["picked"]} == {"1990"}
    for genre, share in (("Alternative Rock", alt_share), ("Indie Rock", 1 - alt_share)):
        rows = [c for c in state["picked"] if c["genre"] == genre]
        assert len(rows) == round(24 * share)
        counts = Counter(c["artist"] for c in rows)
        named = sum(counts[a] for a in artists[genre])
        assert named == min(round(len(rows) * 2 / 3), seed_tracks * len(artists[genre]))
        if genre == "Alternative Rock":
            assert abs(counts["Weezer"] - counts["Staind"]) <= 1
    if seed_tracks > 1:
        index = next(i for i, c in enumerate(state["picked"]) if c["artist"] in state["inputs"]["artists"])
        old_id = state["picked"][index]["video_id"]
        road_trip.reroll_slot(state, store, index, 1000)
        assert state["picked"][index]["video_id"] != old_id
        assert state["picked"][index]["artist"] in state["inputs"]["artists"]


@pytest.mark.parametrize("shares, targets, expected", [
    ([.103, .207, .414, .276], [1, .25, .53, .35], [47, 12, 25, 16]),
    ([.103, .207, .414, .276], [1, None, None, None], [100, 0, 0, 0]),
    ([.1, .75, .25], [.35, None, None], [35, 49, 16]),
    ([.333, .333, .333], [None, None, None], [34, 33, 33]),
    ([1, 0, .4], [1, 0, None], [100, 0, 0]),
    ([0, 0, 0], [None, None, None], [34, 33, 33]),
])
def test_loading_a_saved_balance_uses_one_100_percent_budget(shares, targets, expected):
    state = {"axes": {"theirs": [
        {"key": f"genre:{i}", "kind": "genre", "name": str(i), "share": share, "target": target}
        for i, (share, target) in enumerate(zip(shares, targets))]},
        "targets": {"theirs": {f"genre:{i}": value for i, value in enumerate(targets) if value is not None}}}
    road_trip.normalized(state)
    rows = state["axes"]["theirs"]
    assert [row["balance_pct"] for row in rows] == expected
    assert sum(row["balance_pct"] for row in rows) == 100
    assert [row["share"] for row in rows] == shares  # actual track shares are not UI targets
    assert sum(state["targets"]["theirs"].values()) <= 1 + 1e-9
    road_trip.normalized(state)
    assert [row["balance_pct"] for row in rows] == expected  # no drift on repeated loads


def test_sliding_a_genre_down_reaches_the_requested_share(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    axis = "era:1990"
    assert _share(state, "mine", axis) > 0.2

    road_trip.set_share(state, "mine", axis, 0.1, store)

    assert abs(_share(state, "mine", axis) - 0.1) <= 0.12    # steered to roughly what was asked
    assert _share(state, "mine", axis) < 0.25                 # and clearly down from where it was
    assert next(a["target"] for a in state["axes"]["mine"] if a["key"] == axis) == 0.1


def test_sliding_a_genre_to_zero_removes_it_and_it_can_come_back(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(), 1000.0, seed=5)
    assert any(r["source"] == "theirs" for r in state["picked"])

    axis = next(a["key"] for a in state["axes"]["theirs"] if a["kind"] == "genre")
    road_trip.set_share(state, "theirs", axis, 0.0, store)

    assert not any(r["source"] == "theirs" for r in state["picked"])
    assert axis in {a["key"] for a in state["axes"]["theirs"]}   # the slider stays, so it can come back

    road_trip.clear_share(state, "theirs", axis, store)
    assert any(r["source"] == "theirs" for r in state["picked"])


def _mixed_library(monkeypatch, store, rock=4, other=36):
    """A collection deliberately thin on rock: 4 of 40, i.e. 10%. Play counts run 0..n, so the
    lowest-numbered song of each genre is the least played."""
    songs = [{"key": f"kr{i}", "video_id": f"r{i}", "title": f"Rock {i}",
              "artist": f"Rock Artist {i}", "album": "", "thumbnail": None, "duration": 300,
              "genre": "Rock", "year": 1990, "liked": False} for i in range(rock)]
    songs += [{"key": f"kp{i}", "video_id": f"p{i}", "title": f"Pop {i}",
               "artist": f"Pop Artist {i}", "album": "", "thumbnail": None, "duration": 300,
               "genre": "Pop", "year": 2000, "liked": False} for i in range(other)]
    monkeypatch.setattr(store.rec_query, "library_songs", lambda: songs)
    monkeypatch.setattr(store.rec_query, "play_counts", lambda: {s["key"]: i for i, s in enumerate(songs)})
    monkeypatch.setattr(candidates, "other_input_songs", lambda *a, **k: ([], []))


def _genre_share(state, genre):
    mine = [r for r in state["picked"] if r["source"] == "mine"]
    return sum(1 for r in mine if r["genre"] == genre) / max(1, len(mine))


def test_asking_for_40_percent_rock_gets_40_percent_of_the_playlist(store, monkeypatch):
    """The bar is a share of the PLAYLIST, not of the pool. A quota that was only a ceiling let the
    draw hand back whatever proportion the pool happened to hold (ask 40, get 13); it has to be a
    floor too, filled first."""
    _mixed_library(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    assert _genre_share(state, "Rock") < 0.2         # the pool is thin on rock to start with

    road_trip.set_share(state, "mine", "genre:Rock", 0.4, store)

    assert 0.3 <= _genre_share(state, "Rock") <= 0.5
    assert next(a["share"] for a in state["axes"]["mine"] if a["key"] == "genre:Rock") >= 0.3


def test_forcing_a_genre_up_does_not_shrink_the_playlist(store, monkeypatch):
    """...and it must not cost you the rest of the playlist: capping everything else at 60% while
    rock can only reach 10% is what left a 5-song side."""
    _mixed_library(monkeypatch, store)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    before = state["stats"]["own_count"]

    road_trip.set_share(state, "mine", "genre:Rock", 0.4, store)

    assert state["stats"]["own_count"] >= before - 1


def test_a_barely_played_song_is_still_reachable_when_its_genre_is_asked_for(store, monkeypatch):
    """Nothing is held back in a sample: pin a genre high and the least-played songs of it get
    picked, because the whole collection is the pool."""
    _mixed_library(monkeypatch, store, rock=12, other=28)
    state = road_trip.build_draft(store, FakeClient(),
                                  _recipe(own_pct=100, familiarity_pct=100), 1000.0, seed=5)

    road_trip.set_share(state, "mine", "genre:Rock", 1.0, store)

    rock = [r for r in state["picked"] if r["genre"] == "Rock"]
    assert len(rock) == len(state["picked"])              # the whole side is rock
    # r0 is the least-played rock song in the collection; a favorites-leaning draw still reaches it
    # once rock is the only thing allowed, rather than running out and shortening the playlist.
    assert len(rock) > 4


def test_one_genre_gets_one_bar_whatever_spelled_it(store, monkeypatch):
    """The same genre arrives as "Alternative Rock" (Last.fm), "alternative rock" (typed into the
    recipe) and "ALT ROCK". Three bars would each hold a quota the other two ignore."""
    assert (axes.canonical_genre("alternative rock") == axes.canonical_genre("ALT ROCK")
            == axes.canonical_genre("Alternative Rock") == "Alternative Rock")
    assert axes.canonical_genre("bagpipe funk") == "Bagpipe Funk"    # unknown: just tidied
    assert axes.canonical_genre("  ") == ""


def test_a_bar_the_pool_can_no_longer_fill_goes_away(store, monkeypatch):
    """Sticky isn't forever. A bar left over from a pool that has since been re-drawn can never be
    satisfied again, so it stops taking up space - unless it's pinned, which is a standing request."""
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    state["axes"]["mine"].append({"key": "genre:Ghost", "kind": "genre", "name": "Ghost",
                                  "share": 0.0, "target": None})
    state["axes"]["mine"].append({"key": "genre:Pinned Ghost", "kind": "genre",
                                  "name": "Pinned Ghost", "share": 0.0, "target": 0.5})
    state["targets"]["mine"]["genre:Pinned Ghost"] = 0.5

    road_trip.repick(state, store)

    keys = {a["key"] for a in state["axes"]["mine"]}
    assert "genre:Ghost" not in keys
    assert "genre:Pinned Ghost" in keys


def test_era_bars_stay_in_chronological_order(store, monkeypatch):
    """Decades are a number line. A decade that only turns up on a later re-pick still has to slot
    into place rather than land on the end."""
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    eras = [a["name"] for a in state["axes"]["mine"] if a["kind"] == "era"]
    assert eras == sorted(eras)

    # A decade discovered later (a bar that wasn't in the first draw) is inserted, not appended.
    state["axes"]["mine"] = [a for a in state["axes"]["mine"] if a["key"] != "era:2000"]
    road_trip.repick(state, store)

    eras = [a["name"] for a in state["axes"]["mine"] if a["kind"] == "era"]
    assert eras == sorted(eras) and "2000" in eras


def test_era_slider_reweights_years(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=0)
    state = road_trip.build_draft(store, FakeClient(), _recipe(own_pct=100), 1000.0, seed=5)
    eras = {a["key"] for a in state["axes"]["mine"] if a["kind"] == "era"}
    assert eras == {"era:1990", "era:2000", "era:2010"}

    road_trip.set_share(state, "mine", "era:1990", 0.0, store)

    assert all(r["decade"] != "1990" for r in state["picked"])


