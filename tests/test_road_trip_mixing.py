"""Mix construction, source shortfalls, familiarity and slot rerolls."""
from collections import Counter
from copy import deepcopy

import numpy as np
import pytest

from yt_playlist.controllers.road_trip_transitions import RoadTripFacts
from yt_playlist.rec import rec_params, road_trip, road_trip_candidates as candidates
from tests.conftest import FakeClient
from tests.retained_rating_support import apply_model_ratings
from tests.road_trip_support import _recipe, _stub_pools, complete_draft, no_network

def test_completed_draft_honours_the_requested_mix(store, monkeypatch):
    """The headline bug: a 50/50 recipe has to come back roughly 50/50."""
    _stub_pools(monkeypatch, store)

    state = complete_draft(store, FakeClient(), _recipe(own_pct=50), 1000.0, seed=1)

    stats = state["stats"]
    assert stats["own_count"] == stats["their_count"] == 6      # 60 min of 5-minute tracks
    assert stats["short"] == {}
    assert 55 <= stats["minutes"] <= 65


def test_driver_taste_shapes_redraw_and_reroll_within_pinned_shares(store, monkeypatch):
    rows = [candidates._candidate(video, video, f"Artist {video}", "", None, 300,
                                  "mine", genre, year)
            for video, genre, year in [("a", "Rock", 1995), ("b", "Rock", 1995),
                                       ("c", "Rock", 1995), ("d", "Jazz", 2015)]]
    for row in rows:
        store.tracks.upsert_track(row["video_id"], row["title"], row["artist"], None, 300)
        row.update(key=store.tracks.identity_key_for_video(row["video_id"]), fam=.5, score=1.0)
    monkeypatch.setattr(RoadTripFacts, "own", lambda self, state, now: rows)
    now = 100 * 86400
    state = road_trip.start_draft(store, _recipe(own_pct=100, target_minutes=5, artists=[]), now, seed=135)
    state["targets"]["mine"] = {"genre:Rock": 1, "era:1990": 1}
    road_trip.repick(state, store, now)
    assert state["picks"] == ["a"]

    # Supply a modeled taste context while keeping familiarity and trip constraints fixed.
    account = store.accounts.upsert_account("main", None, True)
    track = store.tracks.upsert_track("anchor", "Anchor", "Listener Artist", None, 300)
    anchor = store.tracks.identity_key_for_video("anchor")
    playlist = store.playlists.upsert_playlist(account, "Taste", "Taste", 1, "hash", 0)
    store.playlists.set_playlist_tracks(playlist, [track])
    vectors = dict(zip((row["key"] for row in rows), ([0, 1], [1, 0], [.5, .866], [1, 0]), strict=True))
    vectors[anchor] = [1, 0]
    store.rec_model.replace_rec_vectors([(key, np.array(vector, dtype=np.float32).tobytes())
                                         for key, vector in vectors.items()])
    road_trip.repick(state, store, now)
    assert state["picks"] == ["b"]
    unmodeled = deepcopy(state)
    road_trip.reroll_slot(state, store, 0, now)
    assert state["picks"] == ["c"]
    assert state["stats"]["minutes"] == 5

    store.rec_model.replace_rec_vectors([])
    road_trip.reroll_slot(unmodeled, store, 0, now)
    assert unmodeled["picks"] == ["a"]


def test_canonical_passenger_seeds_use_credited_tracks_before_and_after_acquisition(store):
    from yt_playlist.library.road_trip_composition import transitions

    right, left = 'UCRightArtist', 'UCLeftArtist'
    tracks = [
        ('selected', 'Twins', right, True, [1, 0]),
        ('same-name', 'Twins', left, True, [0, 1]),
        ('aligned', 'Ally', 'UCAlly', True, [1, 0]),
        ('guest', 'Twins', right, False, [1, 0]),
    ]
    vectors = []
    for video, artist, channel, library, vector in tracks:
        store.tracks.upsert_track(video, video, artist, duration_s=300, library=library,
                                  credits=[{'name': artist, 'id': channel}])
        vectors.append((store.tracks.identity_key_for_video(video), np.array(vector, dtype=np.float32).tobytes()))
    store.rec_model.replace_rec_content_vectors(vectors)
    identity = store.artists.provider_identity(right)
    recipe_id = store.road_trip.save_road_trip_recipe(None, 'Twins', 50, [f'@artist:{identity}'], [], 15, 1000)
    state = road_trip.start_draft(store, store.road_trip.get_road_trip_recipe(recipe_id), 1000, seed=1)

    def assert_fit():
        fit = transitions(store).capture(state, 1000).ranking.passenger_fit
        assert fit['selected'] == fit['aligned'] == pytest.approx(1)
        assert fit['same-name'] == pytest.approx(0)
        assert all(row['mix_source'] == 'yours' for row in state['picked'] if row['video_id'] == 'same-name')

    assert_fit()
    guest = candidates._candidate('guest', 'guest', 'Twins', '', None, 300, 'theirs', '', None)
    guest.update(artist_browse_id=right, fam=.5, score=1.)
    unknown = {**guest, 'video_id': 'unknown', 'title': 'same-name'}
    state['pool'].extend([guest, unknown])
    road_trip.repick(state, store, 1000)
    assert_fit()


@pytest.mark.parametrize('age', [180, 4200], ids=['active-session', 'earlier-today'])
def test_content_only_driver_fit_uses_supported_listening_without_a_taste_context(store, age):
    from tests.conftest import seed_catalog_track
    from yt_playlist.library.road_trip_composition import _rank

    now = 100 * 86400 + 12 * 3600
    account = store.accounts.upsert_account('main', None, True)
    content = {'match': [1., 0.], 'different': [0., 1.], 'passenger': [.707, .707],
               **{f'listened-{i}': [1., 0.] for i in range(3)}}
    for video in [*content, 'unmodeled']:
        artist = 'Passenger Artist' if video == 'passenger' else f'Artist {video}'
        seed_catalog_track(store, video, video, artist, video_id=video)
    store.rec_model.replace_rec_content_vectors([
        (key, np.array(vector, dtype=np.float32).tobytes()) for key, vector in content.items()])
    rec_params.set_param(store, 'play_transient_w', 0)
    rec_params.set_param(store, 'now_min_events', 3)
    own = [dict(video_id=video, key=video, artist=f'Artist {video}', source='mine')
           for video in ('match', 'different', 'unmodeled')]
    passengers = [dict(video_id='passenger', title='passenger', artist='Passenger Artist', source='theirs')]

    def rank():
        with store.read_view() as reader:
            return _rank(reader, own, passengers, ['Passenger Artist'], (), now)

    initial = rank()
    assert initial.driver_fit.get('match', 0.) == initial.driver_fit.get('different', 0.) == 0.
    assert initial.passenger_fit['match'] == initial.passenger_fit['different'] > 0.
    for i in range(3):
        video = f'listened-{i}'
        store.history.record_play_event(account, video, video, now - age + 60 * i, source_timezone='UTC')
        if i == 1:
            assert rank().driver_fit == initial.driver_fit

    ranked = rank()
    assert ranked.driver_fit['match'] > 0. > ranked.driver_fit['different']
    assert 'unmodeled' not in ranked.driver_fit
    assert ranked.passenger_fit == initial.passenger_fit


@pytest.mark.parametrize("genres, source", [([], "yours"), (["Rock"], "blend")])
@pytest.mark.parametrize("content_only", [False, True])
def test_another_recordings_dislike_leaves_driver_draws_unchanged_across_trip_transitions(
        store, monkeypatch, genres, source, content_only):
    trials = [candidates._candidate(video, video, f"Artist {video}", "", None, 300,
                                    "mine", "Rock", 1995)
              for video in ("similar", "unmodeled", "previous")]
    for track in trials:
        store.tracks.upsert_track(track["video_id"], track["title"], track["artist"], None, 300)
        track.update(key=store.tracks.identity_key_for_video(track["video_id"]), fam=.5, score=1.)
    own = trials[:2]
    monkeypatch.setattr(RoadTripFacts, "own", lambda self, state, now: own)
    store.tracks.upsert_track("dislike", "Disliked song", "Another artist", None, 300)
    disliked = store.tracks.identity_key_for_video("dislike")
    vector = np.array([1., 0.], dtype=np.float32).tobytes()
    rows = [(disliked, vector), (own[0]["key"], vector)]
    if content_only:
        store.rec_model.replace_rec_content_vectors(rows)
    else:
        store.rec_model.replace_rec_vectors(rows)
    recipe = _recipe(own_pct=100, target_minutes=5, artists=[], genres=genres)
    now = 1000

    def build(seed):
        state = road_trip.start_draft(store, recipe, now, seed=seed)
        return road_trip.finish_draft(state, store, now) if genres else state

    initial = build(seed=2)
    assert initial["picks"] == ["similar"]
    assert initial["picked"][0]["mix_source"] == source

    # Begin an independent draft with just the existing slot, then offer both replacements.
    own[:] = trials[2:]
    previous = build(seed=1)
    assert previous["picks"] == ["previous"]
    own[:] = trials[:2]

    apply_model_ratings(store, {disliked: "DISLIKE"}, now)
    assert build(seed=2)["picks"] == ["similar"]
    road_trip.repick(initial, store, now)
    assert initial["picks"] == ["similar"]
    rerolled = road_trip.reroll_slot(deepcopy(previous), store, 0, now)
    assert rerolled["picks"] == ["similar"]
    assert rerolled["picked"][0]["mix_source"] == source

    apply_model_ratings(store, {disliked: "INDIFFERENT"}, now)
    road_trip.repick(initial, store, now)
    assert initial["picks"] == ["similar"]
    assert road_trip.reroll_slot(previous, store, 0, now)["picks"] == ["similar"]


@pytest.mark.parametrize("library_at_acquisition", [False, True])
def test_passenger_overlap_uses_current_library_once(store, monkeypatch, library_at_acquisition):
    library = [{"key": "shared|guest artist", "video_id": "shared-video", "title": "Shared",
                "artist": "Guest Artist", "album": "Album", "thumbnail": None, "duration": 240,
                "genre": "Rock", "year": 1995, "liked": False}]
    songs = list(library) if library_at_acquisition else []
    monkeypatch.setattr(type(store.rec_query), "library_songs", lambda self: songs)
    monkeypatch.setattr(candidates, "other_input_songs", lambda *args, **kwargs: ([{
        "video_id": "shared-video", "title": "Shared", "artist": "Guest Artist", "album": "Album",
        "thumbnail": None, "duration": 240, "genre": "Rock"}], []))
    state = road_trip.start_draft(store, _recipe(artists=["Guest Artist"], target_minutes=8), 1000, seed=7)
    item = state["pending"].pop(0)
    road_trip.add_other_input(state, store, FakeClient(), item)
    if not library_at_acquisition:
        songs.extend(library)  # A sync/save after acquisition changes the owner at the next pick.
    state["building"] = False
    road_trip.repick(state, store, 1000)
    rows = road_trip.draft_tracks(state)
    assert [row["video_id"] for row in rows] == ["shared-video"]
    assert rows[0]["source"] == "mine" and rows[0]["mix_source"] == "blend"
    assert state["stats"]["minutes"] == state["stats"]["own_minutes"] == state["stats"]["blend_minutes"] == 4
    assert state["stats"]["own_count"] == state["stats"]["blend_count"] == 1
    assert state["stats"]["their_count"] == state["stats"]["their_minutes"] == 0
    assert sum(state["stats"]["mix_targets"].values()) == len(rows)


def test_default_mix_identifies_yours_theirs_and_blend_through_swaps_and_reload(store, monkeypatch):
    _stub_pools(monkeypatch, store, own_genre=lambda i: "Rock" if i % 2 == 0 else "Jazz")

    state = complete_draft(
        store, FakeClient(), _recipe(own_pct=50, genres=["Rock"]), 1000.0, seed=1)

    assert state["stats"]["overlap_count"] == 4
    assert state["stats"]["own_count"] == 8       # library accounting includes the shared pool
    assert state["stats"]["their_count"] == 4
    assert state["stats"]["yours_count"] == state["stats"]["blend_count"] == 4
    assert Counter(c["mix_source"] for c in state["picked"]) == {"yours": 4, "theirs": 4, "blend": 4}
    assert {c["genre"] for c in state["picked"] if c["mix_source"] == "blend"} == {"Rock"}
    for source in ("yours", "theirs", "blend"):
        index = next(i for i, c in enumerate(state["picked"]) if c["mix_source"] == source)
        previous = state["picked"][index]["video_id"]
        road_trip.reroll_slot(state, store, index, 1000)
        assert state["picked"][index]["video_id"] != previous
        assert state["picked"][index]["mix_source"] == source
    # Stored drafts from before source labels existed must show the same three pools on reload.
    for candidate in state["picked"]:
        candidate.pop("mix_source")
    for key in ("yours_count", "blend_count", "yours_minutes", "blend_minutes"):
        state["stats"].pop(key)
    state.pop("blend_available")
    road_trip.normalized(state)
    assert Counter(c["mix_source"] for c in state["picked"]) == {"yours": 4, "theirs": 4, "blend": 4}
    assert state["stats"]["yours_count"] == state["stats"]["blend_count"] == 4
    assert state["stats"]["yours_minutes"] == state["stats"]["blend_minutes"] == 20
    assert state["blend_available"] is True


def test_blend_sources_and_totals_include_shortfall_replacements(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=2,
                own_genre=lambda i: "Rock" if i % 2 == 0 else "Jazz")
    state = complete_draft(
        store, FakeClient(), _recipe(own_pct=50, genres=["Rock"]), 1000, seed=1)
    assert state["stats"]["short"]["theirs"] > 0
    for candidate in state["picked"]:
        if candidate["source"] == "mine":
            assert candidate["mix_source"] == ("blend" if candidate["genre"] == "Rock" else "yours")
    counts = Counter(c["mix_source"] for c in state["picked"])
    assert counts == {"yours": 6, "theirs": 2, "blend": 4}
    assert state["stats"]["yours_count"] == counts["yours"]
    assert state["stats"]["blend_count"] == counts["blend"]
    assert sum(counts.values()) == state["stats"]["own_count"] + state["stats"]["their_count"]


@pytest.mark.parametrize("own_pct, uneven_lengths", [(50, False), (50, True), (0, True), (100, True)])
def test_blend_reserves_song_slots_with_library_genre_and_era_targets(
        store, monkeypatch, own_pct, uneven_lengths):
    genres = ["Rock", "Pop", "Trance", "Deep House"]
    _stub_pools(monkeypatch, store, own_count=160, other_count=80,
                own_genre=lambda i: genres[i % 4], own_year=lambda i: 1990 if i % 4 == 0 else 2000)
    state = complete_draft(store, FakeClient(),
                                  _recipe(own_pct=own_pct, genres=["Rock"], target_minutes=240),
                                  1000, seed=7)
    # Bypass the initial per-input discovery cap so every requested mix has ample candidates.
    state["pool"] += [{**state["pool"][0], "video_id": f"extra-{i}", "artist": f"Extra artist {i}"}
                      for i in range(40)]
    state["targets"]["mine"] = {"genre:Rock": .34, "genre:Pop": .19,
                                "genre:Trance": .14, "genre:Deep House": .33,
                                "era:1990": .34, "era:2000": .66}
    if uneven_lengths:
        # Blend songs take ten minutes; Theirs takes three. The source percentages still refer
        # to song counts, and Yours' absent genres/years must not consume Blend's reserved slots.
        durations = {"Rock": 600, "Pop": 240, "Trance": 480, "Deep House": 360}
        for candidate in store.rec_query.library_songs():
            candidate["duration"] = durations[candidate["genre"]]
        for candidate in state["pool"]:
            candidate["duration"] = 180
    road_trip.repick(state, store, 1000)
    counts = Counter(c["mix_source"] for c in state["picked"])
    total = sum(counts.values())
    assert abs(counts["blend"] - total / 3) <= 1
    assert abs(counts["yours"] - total * 2 / 3 * own_pct / 100) <= 1
    assert abs(state["stats"]["minutes"] - 240) <= 6
    assert not state["stats"]["short"]
    assert counts == {k: v for k, v in state["stats"]["mix_targets"].items() if v}
    assert {c["genre"] for c in state["picked"] if c["mix_source"] == "blend"} == {"Rock"}
    assert {c["year"] for c in state["picked"] if c["mix_source"] == "blend"} == {1990}


@pytest.mark.parametrize("rock_share", [0, .34])
def test_blend_shortage_is_reported_without_overriding_exclusions(store, monkeypatch, rock_share):
    _stub_pools(monkeypatch, store, own_genre=lambda i: "Rock" if i < 2 else "Jazz")
    state = complete_draft(store, FakeClient(), _recipe(genres=["Rock"]), 1000, seed=7)
    state["targets"]["mine"] = {"genre:Rock": rock_share, "genre:Jazz": 1 - rock_share}
    road_trip.repick(state, store, 1000)
    counts = Counter(c["mix_source"] for c in state["picked"])
    assert sum(counts.values()) == 12
    assert counts["blend"] == (2 if rock_share else 0)
    assert state["stats"]["short"]["blend"] > 0
    assert state["stats"]["mix_targets"]["blend"] == 4
    assert state["stats"]["minutes"] == 60
    if not rock_share:
        assert all(c["genre"] != "Rock" for c in state["picked"])


def test_build_draft_honours_a_lopsided_mix(store, monkeypatch):
    _stub_pools(monkeypatch, store)

    state = complete_draft(store, FakeClient(), _recipe(own_pct=20), 1000.0, seed=1)

    assert state["stats"]["own_count"] == 2            # 12 min of your side, to the nearest track
    assert state["stats"]["their_count"] == 10          # 48 min of theirs


def test_build_draft_reports_when_their_side_runs_short(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=2)

    state = complete_draft(store, FakeClient(), _recipe(own_pct=50), 1000.0, seed=1)

    assert state["stats"]["their_count"] == 2          # everything they had
    assert state["stats"]["short"]["theirs"] > 0        # and it says so, instead of pretending
    assert state["stats"]["own_count"] > 6              # your side covered the rest


def test_repeated_builds_produce_different_playlists(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    recipe = _recipe()

    runs, previous = [], []
    for seed in (11, 22, 33, 44):
        state = complete_draft(store, FakeClient(), recipe, 1000.0, seed, previous)
        previous = state["picks"]
        runs.append(tuple(state["picks"]))

    assert len(set(runs)) == 4                                  # four runs, four playlists
    for a, b in zip(runs, runs[1:]):                            # and consecutive ones barely overlap
        assert len(set(a) & set(b)) <= len(a) // 2


def test_same_seed_rebuilds_the_same_playlist(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    a = complete_draft(store, FakeClient(), _recipe(), 1000.0, seed=7)
    b = complete_draft(store, FakeClient(), _recipe(), 1000.0, seed=7)
    assert a["picks"] == b["picks"]


def test_familiarity_slider_moves_between_favorites_and_deep_cuts(store, monkeypatch):
    _stub_pools(monkeypatch, store, other_count=0)
    plays = {}

    def played(state):   # the stub gives m<i> exactly i plays
        rows = state["picked"]
        return sum(int(r["video_id"][1:]) for r in rows) / max(1, len(rows))

    for pct in (0, 100):
        state = complete_draft(store, FakeClient(),
                                      _recipe(own_pct=100, familiarity_pct=pct), 1000.0, seed=3)
        plays[pct] = played(state)

    assert plays[100] > plays[0] * 2    # favorites at one end, lesser listens at the other
    assert plays[100] > 20 > plays[0]   # and each end sits in its half of the collection


def test_reroll_swaps_one_slot_and_leaves_the_rest_alone(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    state = complete_draft(store, FakeClient(), _recipe(), 1000.0, seed=9)
    before = list(state["picks"])
    before_src = [r["source"] for r in state["picked"]]

    road_trip.reroll_slot(state, store, 2)

    after = list(state["picks"])
    assert len(after) == len(before)
    assert after[:2] == before[:2] and after[3:] == before[3:]      # only slot 2 moved
    assert after[2] != before[2]
    assert state["picked"][2]["source"] == before_src[2]            # replaced like with like
    assert before[2] in state["banned"]                             # and it doesn't come back

    road_trip.reroll_slot(state, store, 2)
    assert state["picks"][2] not in (before[2], after[2])


def test_reroll_drops_the_slot_when_nothing_is_left_to_offer(store, monkeypatch):
    _stub_pools(monkeypatch, store, own_count=6, other_count=6)
    state = complete_draft(store, FakeClient(), _recipe(target_minutes=60), 1000.0, seed=9)
    picks = len(state["picks"])

    for _ in range(picks):                       # cross out slot 0 until the pool is exhausted
        road_trip.reroll_slot(state, store, 0)

    assert len(state["picks"]) < picks


def test_draft_tracks_are_ordered_playable_rows(store, monkeypatch):
    _stub_pools(monkeypatch, store)
    state = complete_draft(store, FakeClient(), _recipe(), 1000.0, seed=4)

    tracks = road_trip.draft_tracks(state)

    assert [t["video_id"] for t in tracks] == state["picks"]
    assert all(t["title"] and t["artist"] and t["duration"] for t in tracks)
