import pytest

from yt_playlist.rec import embed, eval_recs
from tests import model_support
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key


def test_recall_recovers_held_out_cluster_track(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    # one tight 8-track cluster (one playlist) + a distractor cluster, so the held-out
    # track should rank near the rest of its own playlist.
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None, created_at=0) for i in range(8)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None, created_at=0) for i in range(8)]
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "PA", 8, "h", 0.0), A)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PB", "PB", 8, "h2", 0.0), B)
    model_support.build_track_model(store, dim=4)

    res = eval_recs.recall_at_k(store, k=5, min_size=5)
    assert res["trials"] == 2
    assert res["recall_at_k"] == 1.0          # both held-out tracks recovered in top-5


def test_recall_none_without_vectors(store):
    store.identities.upsert_identity("main", "cred", None, True)
    assert eval_recs.recall_at_k(store)["recall_at_k"] is None


def _two_cluster_playlists(store):
    """Two tight 8-track clusters, one per playlist, so the embedding separates A from B. Returns iid."""
    iid = store.identities.upsert_identity("main", "cred", None, True)
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None, created_at=0) for i in range(8)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None, created_at=0) for i in range(8)]
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "PA", 8, "h", 0.0), A)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PB", "PB", 8, "h2", 0.0), B)
    model_support.build_track_model(store, dim=4)
    return iid


def test_temporal_recall_recovers_recent_plays_from_history(store):
    # The model's real job: predict what you played next. Context = cluster-A tracks played before the
    # holdout window; held-out = the rest of cluster A played inside it. A tight cluster should rank its
    # own held-out members near the context centroid, so recall is high.
    iid = _two_cluster_playlists(store)
    day, t_recent = 86400, 1_000_000.0
    store.history.record_history_plays(iid, t_recent - 10 * day,
        [identity_key(f"A{i}", "AB") for i in range(4)], source_timezone="UTC")
    store.history.record_history_plays(iid, t_recent,
        [identity_key(f"A{i}", "AB") for i in range(4, 8)], source_timezone="UTC")

    res = eval_recs.temporal_recall(store, holdout_days=5, k=8)
    assert res["trials"] == 4                 # the four held-out recent plays, none seen before the cutoff
    assert res["recall"] == 1.0               # all recovered in the top-8 by the context centroid


@pytest.mark.parametrize("source", ["ytm_history", "independent"])
def test_temporal_recall_excludes_generated_only_held_plays(store, source):
    # #83 closing the last circular path: a held-out play the app CAUSED (played only from a
    # quarantined generated playlist, per play_events provenance) must not count as a successful
    # prediction. Keys with organic evidence in the window (mixed, or no play_events rows at all)
    # stay. Independent dated evidence also prevents exclusion.
    from yt_playlist.repos.base import GENERATED_GROUP
    iid = _two_cluster_playlists(store)
    day, t_recent = 86400, 1_000_000.0
    store.history.record_history_plays(iid, t_recent - 10 * day,
        [identity_key(f"A{i}", "AB") for i in range(4)], source_timezone="UTC")
    held = [identity_key(f"A{i}", "AB") for i in range(4, 8)]
    store.history.record_history_plays(iid, t_recent,
        [(key, 'Today') for key in held] if source == 'ytm_history' else held, source_timezone="UTC")
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    store.playlists.set_playlist_group("PLGEN", GENERATED_GROUP)
    store.playlists.mark_generated_playlist('PLGEN')
    # cutoff = 1_000_000 - 5*86400 = 568_000; all provenance rows below are inside the window.
    store.history.record_play_event(iid, identity_key("A6", "AB"), "a6", 999_000.0, playlist_ytm_id="PLGEN")
    store.history.record_play_event(iid, identity_key("A7", "AB"), "a7", 998_000.0, playlist_ytm_id="PLGEN")
    # A5 is MIXED evidence: one generated play plus one organic (no-playlist) play, spaced beyond
    # the 30-minute live-dedup merge window so they land as two distinct rows. It must stay held.
    store.history.record_play_event(iid, identity_key("A5", "AB"), "a5", 997_000.0, playlist_ytm_id="PLGEN")
    store.history.record_play_event(iid, identity_key("A5", "AB"), "a5", 990_000.0, playlist_ytm_id=None)

    res = eval_recs.temporal_recall(store, holdout_days=5, k=8)

    assert res["generated_excluded"] == (2 if source == "ytm_history" else 0)
    assert res["trials"] == (2 if source == "ytm_history" else 4)
    assert res["recall"] == 1.0


@pytest.mark.parametrize("source", ["ytm_history", "independent"])
def test_temporal_recall_all_held_generated_is_insufficient_not_perfect(store, source):
    # When EVERY held-out play is app-caused, the honest answer is "no usable split", never a
    # recall score computed over zero real plays.
    from yt_playlist.repos.base import GENERATED_GROUP
    iid = _two_cluster_playlists(store)
    day, t_recent = 86400, 1_000_000.0
    store.history.record_history_plays(iid, t_recent - 10 * day,
        [identity_key(f"A{i}", "AB") for i in range(4)], source_timezone="UTC")
    held = [identity_key(f"A{i}", "AB") for i in range(4, 8)]
    store.history.record_history_plays(iid, t_recent,
        [(key, 'Today') for key in held] if source == 'ytm_history' else held, source_timezone="UTC")
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    store.playlists.set_playlist_group("PLGEN", GENERATED_GROUP)
    store.playlists.mark_generated_playlist('PLGEN')
    for i in range(4, 8):
        store.history.record_play_event(iid, identity_key(f"A{i}", "AB"), f"a{i}", 990_000.0 + i * 3600,
                                playlist_ytm_id="PLGEN")

    res = eval_recs.temporal_recall(store, holdout_days=5, k=8)

    if source == "ytm_history":
        assert res["recall"] is None
        assert res["trials"] == 0 and res["generated_excluded"] == 4
        assert res["reason"] == "insufficient temporal split"
    else:
        assert res["recall"] == 1.0
        assert res["trials"] == 4 and res["generated_excluded"] == 0


def test_temporal_recall_none_without_vectors(store):
    store.identities.upsert_identity("main", "cred", None, True)
    assert eval_recs.temporal_recall(store)["recall"] is None


def test_temporal_recall_none_without_history(store):
    _two_cluster_playlists(store)             # vectors exist, but no history snapshots were taken
    assert eval_recs.temporal_recall(store)["recall"] is None


def test_rolling_temporal_recall_rebuilds_as_of_cutoff(store):
    iid = _two_cluster_playlists(store)
    day, t = 86400, 5_000_000.0
    store.history.record_history_plays(iid, t - 20 * day,
        [identity_key(f"A{i}", "AB") for i in range(4)], source_timezone="UTC")
    store.history.record_history_plays(iid, t,
        [identity_key(f"A{i}", "AB") for i in range(4, 8)], source_timezone="UTC")

    result = eval_recs.rolling_temporal_recall(store, holdout_days=5, windows=3, k=8, dim=4)

    assert result["causal"] is True
    assert result["viable_windows"] == 1
    assert result["trials"] == 4
    assert result["recall"] == 1.0
    assert len(result["windows"]) == 3
    assert result["windows"][0]["coverage"]["safe_playlists"] == 2


def test_as_of_baskets_ignore_post_cutoff_playlist_changes(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    tracks = [store.tracks.upsert_track(f"v{i}", f"T{i}", "Artist", None, None, created_at=10) for i in range(8)]
    stable = store.playlists.upsert_playlist(iid, "stable", "Stable", 4, "s", 20)
    changed = store.playlists.upsert_playlist(iid, "changed", "Changed", 4, "old", 20)
    store.playlists.set_playlist_tracks(stable, tracks[:4])
    store.playlists.set_playlist_tracks(changed, tracks[4:])
    store.playlists.upsert_playlist(iid, "changed", "Changed", 4, "future", 200)

    before, coverage = store.rec_evidence.rec_baskets_as_of(100)
    changed_keys = {identity_key(f"T{i}", "Artist") for i in range(4, 8)}

    assert coverage["safe_playlists"] == 1
    assert not any(set(basket) == changed_keys for basket in before)


def test_projection_recall_breakdown_by_family_and_coverage(store):
    # The scalar projection_recall hides where grounding fails. The breakdown must partition trials by
    # genre family, era, and coverage band so the failure modes are locatable.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(15)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(15)]
    for t in A:
        store.tracks.set_track_genre(t, "Techno")
        store.tracks.set_track_year(t, "2015")       # cluster A has a year (coverage band: genre+year)
    for t in B:
        store.tracks.set_track_genre(t, "Folk")      # cluster B has no year (coverage band: genre only)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "PA", 10, "h", 0.0), A)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PB", "PB", 10, "h2", 0.0), B)
    model_support.build_track_model(store, dim=8)

    r = eval_recs.projection_recall(store, k=10)
    bd = r["breakdown"]
    assert genre_map.family("Techno") in bd["by_family"]
    assert genre_map.family("Folk") in bd["by_family"]
    assert sum(b["trials"] for b in bd["by_family"].values()) == r["trials"]   # a partition of trials
    assert set(bd["by_coverage"]) == {"genre+year", "genre"}   # §2 widened the band taxonomy (+audio)
    assert bd["by_coverage"]["genre+year"]["trials"] == 15
    assert bd["by_coverage"]["genre"]["trials"] == 15


@pytest.mark.parametrize('new_width', [4, 7])
def test_projection_recall_pins_reference_during_model_publication(store, monkeypatch, new_width):
    import numpy as np
    from yt_playlist.rec.discover import ContentProjection

    for i in range(40):
        track = store.tracks.upsert_track(f'v{i}', f'Song {i}', f'Artist {i}', None, 180)
        store.tracks.set_track_genre(track, 'House' if i < 20 else 'Jazz')
    keys = [store.tracks.identity_key_for_video(f'v{i}') for i in range(40)]
    rng = np.random.default_rng(3)
    store.rec_model.replace_rec_vectors([(key, rng.normal(size=4).astype(np.float32).tobytes())
                                        for key in keys])
    expected = eval_recs.projection_recall(store)
    assert expected['trials'] == 40
    fit = ContentProjection.fit_from_inputs
    calls = []

    def interrupted(cls, reference, content, audio, **kwargs):
        calls.append(reference[1].shape)
        if len(calls) == 1:
            store.rec_model.replace_rec_vectors([
                (key, rng.normal(size=new_width).astype(np.float32).tobytes()) for key in keys])
        assert reference[1].shape == (40, 4)
        return fit(reference, content, audio, **kwargs)

    monkeypatch.setattr(ContentProjection, 'fit_from_inputs', classmethod(interrupted))
    assert eval_recs.projection_recall(store) == expected
    assert len(calls) == 5


def test_historical_fit_checks_memory_before_training(store, monkeypatch):
    from yt_playlist.rec import embedding_training, model_refresh
    _two_cluster_playlists(store)
    monkeypatch.setattr(embedding_training, 'build_vectors_from_baskets',
                        lambda *a, **kw: pytest.fail('fit exceeded memory admission'))
    with pytest.raises(model_refresh.MemoryBudgetExceeded):
        eval_recs._causal_window(store, 100, 200, 20, 4, 'svd', memory_bytes=1)


def test_projection_target_sample_is_bounded_and_repeatable(store):
    for i in range(40):
        track = store.tracks.upsert_track(f'v{i}', f'Track {i}', 'Artist', None, 180)
        store.tracks.set_track_genre(track, 'House' if i < 20 else 'Jazz')
    _two_cluster_playlists(store)
    model_support.build_track_model(store, dim=4)
    sampled = eval_recs.projection_recall(store, max_targets=7)
    assert sampled['sampled'] and sampled['sample_size'] == 7
    assert sampled['eligible'] > 7
    assert sampled['trials'] + sampled['unscored'] + sampled['insufficient'] == 7
    assert sum(row['eligible'] for row in sampled['breakdown']['by_coverage'].values()) == 7
    assert eval_recs.projection_recall(store, max_targets=7) == sampled
