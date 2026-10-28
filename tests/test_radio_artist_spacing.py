"""Radio keeps artists apart across heard history and every proposed stream."""
import numpy as np
import pytest

from yt_playlist.rec import radio_tuning
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections


def prepare(store, groups):
    modes, bundles, pools = [], {}, []
    for mid, artists in enumerate(groups, 1):
        tracks = []
        for i, artist in enumerate(artists):
            video = f"spacing-{mid}-{i}"
            tid = store.tracks.upsert_track(video, video, artist, "", 200)
            store.tracks.set_track_enrichment(tid, "Ambient", "2002")
            tracks.append(dict(key=store.tracks.identity_key_for_video(video), video_id=video,
                               title=video, artist=artist, genre="Ambient"))
        modes.append(dict(mode_id=mid, label="Ambient", families=[["ambient", len(tracks)]],
                          centroid=np.array([1., mid * .01], dtype=np.float32),
                          size=len(tracks), rep_keys=[]))
        bundles[str(mid)] = {"wheelhouse": tracks}
        pools.append(tracks)
    store.modes.replace_modes(modes, [], 1)
    store.rec_proposals.put_proposals("mode_bundles", bundles)
    return pools


def candidates(store, **controls):
    result = RadioCandidates(store, 100_000, {**radio_tuning.DEFAULTS, **controls})
    result.rng.seed(387)
    return result


def assert_spaced(tracks):
    for i, track in enumerate(tracks):
        previous = {t["artist"].strip().casefold() for t in tracks[max(0, i - 6):i]}
        assert track["artist"].strip().casefold() not in previous


@pytest.mark.parametrize("theme,comfort", [(.5, .5), (.5, 1), (1, 1)])
def test_artist_does_not_repeat_within_six_songs_even_when_highly_ranked(store, theme, comfort):
    tracks, = prepare(store, [[f"Artist {i % 9}" for i in range(54)]])
    c = candidates(store, theme=theme, comfort=comfort)
    history = [tracks[0]]
    for _ in range(24):
        history.append(c.next_track(history, target=tracks[0]))
    assert_spaced(history)


def test_each_direction_spaces_artists_against_history_and_its_own_prefix(store):
    tracks, = prepare(store, [[f"Artist {i % 12}" for i in range(84)]])
    c = candidates(store)
    history = tracks[:6]
    directions = RadioDirections(c, history[-1])
    directions.update(history, [], 0)
    for branch in directions.slots:
        assert len(branch["tracks"]) == 6
        assert_spaced(history + branch["tracks"])


@pytest.mark.parametrize("anchor", [False, True])
def test_recent_only_local_pool_checks_prepared_neighbours_before_repeating(store, anchor):
    local, neighbour = prepare(store, [["Cliff Martinez"] * 8, ["Floating Points"] * 8])
    c = candidates(store)
    history = [local[0]]
    if anchor:
        pick = c.direction_anchor(history, [neighbour[0]])
    else:
        pick = c.next_track(history, target=local[0])
    assert pick["artist"] == "Floating Points"
    assert len(c.loaded) <= 2


@pytest.mark.parametrize("theme", [.5, 1])
def test_thin_pool_alternates_artists_instead_of_stopping_or_favouring_recent_one(store, theme):
    tracks, = prepare(store, [["Cliff Martinez"] * 10 + ["M83"] * 10])
    c = candidates(store, theme=theme, comfort=1)
    history = [tracks[0], tracks[10]]
    for _ in range(8):
        pick = c.next_track(history, target=history[-1])
        assert pick and pick["artist"] != history[-1]["artist"]
        history.append(pick)
    assert len({t["video_id"] for t in history}) == len(history)


def test_artist_spacing_normalizes_case_and_whitespace(store):
    tracks, = prepare(store, [["Cliff Martinez", "  CLIFF   MARTINEZ ", "M83"]])
    c = candidates(store)
    assert c.next_track([tracks[0]])["artist"] == "M83"
    assert c.next_track([tracks[0], tracks[2]])["video_id"] == tracks[1]["video_id"]


def test_single_artist_can_continue_when_there_is_no_alternative(store):
    tracks, = prepare(store, [["Cliff Martinez"] * 3])
    c = candidates(store, theme=1, comfort=1)
    history = [tracks[0]]
    for _ in range(2):
        history.append(c.next_track(history))
    assert c.next_track(history) is None
    assert len({t["video_id"] for t in history}) == 3
