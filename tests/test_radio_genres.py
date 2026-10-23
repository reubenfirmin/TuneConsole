"""Genre endpoints must escape style clusters and remain reversible during playback."""
import numpy as np
import pytest

from tests.radio_session_support import poc, play
from yt_playlist.rec import embedding_vectors as embed, radio_tuning
from yt_playlist.rec.radio_candidates import CLUSTER_POOL_LIMIT, GENRE_POOL_LIMIT, RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec.radio_genres import GenrePolicy, genre
from yt_playlist.web import radio_session


@pytest.fixture
def genre_pools(store):
    # All four nearest neighbours of the seed are Electronic. Other genres are
    # prepared and playable but deliberately lie outside that neighbourhood.
    genres = ["Electronic"] * 5 + ["Synthwave", "Jazz", "Rock", "Ambient"]
    modes, bundles, pools = [], {}, {}
    for index, label in enumerate(genres):
        mid = str(index + 1)
        angle = index * .01 if index < 6 else (index - 5) * 1.2
        vector = np.array([np.cos(angle), np.sin(angle)], dtype=np.float32)
        modes.append(dict(mode_id=index + 1, label=label, families=[[label, 12]],
                          centroid=vector, size=12, rep_keys=[]))
        rows = []
        for i in range(12):
            video = f"genre-{mid}-{i}"
            tid = store.tracks.upsert_track(video, video, f"Artist {video}", "", 200)
            store.tracks.set_track_enrichment(tid, label, "2001")
            key = store.tracks.catalog_subject(tid)["identity_key"]
            rows.append(dict(key=key, video_id=video, title=video, artist=f"Artist {video}", genre=label))
        pools[mid] = rows
        bundles[mid] = {"wheelhouse": rows}
    store.modes.replace_modes(modes, retired_ids=[], now=1)
    store.rec_proposals.put_proposals("mode_bundles", bundles)
    return pools


def candidates(store, theme, **values):
    result = RadioCandidates(store, 100_000, {**radio_tuning.DEFAULTS, "theme": theme, **values})
    result.rng.seed(3)
    return result


@pytest.mark.parametrize("energy", [0, .5, 1])
def test_eclectic_actively_changes_genre_beyond_nearest_clusters(store, genre_pools, monkeypatch, energy):
    c = candidates(store, 0, comfort=1, energy=energy)
    seed = genre_pools["1"][0]
    assert all(genre_pools[mid][0]["genre"] == "Electronic" for mid in c.neighbours["1"])

    def forbidden(*args, **kwargs):
        pytest.fail("Playback selection must not load the full library embedding matrix")

    monkeypatch.setattr(embed, "load_content_vectors", forbidden)
    loaded, scored = [], []
    load, score = c._load, c._score

    def loading(mid):
        loaded.append(mid)
        return load(mid)

    def scoring(track, *args):
        scored.append(track["video_id"])
        return score(track, *args)

    monkeypatch.setattr(c, "_load", loading)
    monkeypatch.setattr(c, "_score", scoring)
    history = [seed]
    for _ in range(12):
        loaded.clear()
        scored.clear()
        pick = c.next_track(history, target=seed)
        assert pick and genre(pick) != genre(history[-1])
        assert len(loaded) <= GENRE_POOL_LIMIT
        assert len(scored) <= GENRE_POOL_LIMIT * CLUSTER_POOL_LIMIT
        history.append(pick)
    assert len({genre(t) for t in history}) >= 4
    assert len({t["video_id"] for t in history}) == len(history)


def test_middle_keeps_embedding_style_continuity(store, genre_pools):
    c = candidates(store, .5)
    seed = genre_pools["1"][0]
    history = [seed]
    for _ in range(6):
        history.append(c.next_track(history, target=seed))
    assert all(t["genre"] == "Electronic" for t in history)


def test_themed_uses_observed_genre_even_with_an_old_seed_and_unheard_prefix(store, genre_pools):
    c = candidates(store, 1)
    seed, playing, unheard = genre_pools["1"][0], genre_pools["7"][0], genre_pools["8"][0]
    pick = c.next_track([seed, playing, unheard], target=seed, current=playing)
    assert pick["genre"] == "Jazz"
    # Genre identity is narrower than the embedding cluster or genre family.
    assert not c.fits(genre_pools["6"][0], seed)  # Synthwave and Electronic share a family.
    assert not c.fits({**seed, "genre": None}, seed)


def test_themed_exhaustion_does_not_spill_into_other_genres(store, genre_pools):
    c = candidates(store, 1)
    seed = genre_pools["1"][0]
    excluded = {t["video_id"] for rows in genre_pools.values() for t in rows if t["genre"] == "Electronic"}
    assert c.next_track([seed], excluded=excluded) is None
    assert c.direction_anchor([seed], [seed], excluded=excluded) is None
    assert c.next_track([{**seed, "genre": None}]) is None


def test_all_themed_branches_follow_a_changed_playing_genre(store, genre_pools):
    c = candidates(store, 1)
    seed, playing = genre_pools["1"][0], genre_pools["7"][0]
    plan = RadioDirections(c, seed)
    plan.update([seed], [], 0)
    scheduled = plan.slots[0]["tracks"][:1]
    plan.update([seed, playing], scheduled, 1)
    assert all(branch["tracks"] for branch in plan.slots)
    assert all(t["genre"] == "Jazz" for branch in plan.slots for t in branch["tracks"])
    assert all(t["video_id"] != playing["video_id"] for branch in plan.slots for t in branch["tracks"])
    assert not plan.update([seed, playing], scheduled, 1)


def test_genre_identity_handles_equivalents_without_collapsing_families():
    assert genre({"genre": "DRUM AND BASS"}) == genre({"genre": "Drum & Bass"})
    assert genre({"genre": "Synthwave"}) != genre({"genre": "Electronic"})
    policy = GenrePolicy(0, [{"genre": "Electronic"}])
    assert policy.contrast({"genre": "Jazz"}) > policy.contrast({"genre": "Synthwave"})
    assert policy.contrast({"genre": None}) < policy.contrast({"genre": "Electronic"})


def test_dj_can_alternate_genre_endpoints_without_restarting_or_rejecting_songs(poc, genre_pools, monkeypatch):
    c, remote, frames, _, _ = poc
    monkeypatch.setattr(radio_session, "RadioCandidates", RadioCandidates)
    seed = genre_pools["1"][0]
    assert c.start(seed["video_id"])["ok"]
    play(c, remote.ids[0])
    playlist = c.session.playlist
    initial_frames = list(frames)

    def tune(theme):
        assert c.tune({**radio_tuning.DEFAULTS, "theme": theme})["ok"]
        c.advance(c.session)
        assert not c.status()["updating"]
        assert c.session.playlist == playlist
        assert frames == initial_frames

    tune(0)
    first = c.session.tracks[c.session.position + 1]
    assert first["genre"] != "Electronic"
    # The song on air, rather than the seed or the previous slider value,
    # establishes the next genre pin.
    play(c, first["video_id"])
    playing = c.session.history_view().current
    history = list(c.session.heard)
    for theme in (1, 0, 1):
        tune(theme)
        future = c.session.tracks[c.session.position + 1:]
        assert future
        if theme == 1:
            assert all(genre(t) == genre(playing) for t in future)
            assert all(genre(t) == genre(playing) for b in c.session.directions.slots for t in b["tracks"])
        else:
            assert genre(future[0]) != genre(playing)
        assert c.session.heard == history
        assert c.session.observed_video == playing["video_id"]
    assert not c.session.rejected


def test_pending_genre_pin_rebases_after_a_native_song_change(poc, genre_pools, monkeypatch):
    c, remote, _, _, _ = poc
    monkeypatch.setattr(radio_session, "RadioCandidates", RadioCandidates)
    assert c.start(genre_pools["1"][0]["video_id"])["ok"]
    play(c, remote.ids[0])
    assert c.tune({**radio_tuning.DEFAULTS, "theme": 0})["ok"]
    c.advance(c.session)
    next_song = c.session.tracks[c.session.position + 1]
    assert next_song["genre"] != "Electronic"
    assert c.tune({**radio_tuning.DEFAULTS, "theme": 1})["ok"]
    # Playback moves while the new future is being added. The transaction must
    # keep the heard prefix and repin its unsent future to the new playing genre.
    def transition():
        c.ctx.bridge.now_playing = {"video_id": next_song["video_id"],
            "playlist_ytm_id": c.session.playlist, "paused": False, "position": 30, "duration": 300}

    remote.after_add = transition
    c.advance(c.session)
    remote.after_add = lambda: None
    c.advance(c.session)
    assert not c.status()["updating"]
    assert c.session.observed_video == next_song["video_id"]
    assert next_song["video_id"] in [t["video_id"] for t in c.session.heard]
    assert all(genre(t) == genre(next_song) for t in c.session.tracks[c.session.position + 1:])
    assert all(genre(t) == genre(next_song) for b in c.session.directions.slots for t in b["tracks"])
    assert not c.session.rejected
