"""Completion shares canonical artist scores and credits across ranking and expansion."""
from yt_playlist.rec import artist_model, surfaces


def test_completion_ranks_once_without_public_names_or_repeated_credit_reads(store, monkeypatch):
    iid = store.identities.upsert_identity("main", "bridge", None, True)
    entries = []
    for video, title, name, provider in [
        ("seed", "Anchor", "Seed", "seed"),
        ("one", "First", "Twins", "one"),
        ("two", "Second", "Twins", "two"),
    ]:
        tid = store.tracks.upsert_track(video, title, name, None, 200,
                                       credits=[{"name": name, "id": provider}])
        store.tracks.set_track_genre(tid, "Techno")
        entries.append(tid)
    library = store.playlists.upsert_playlist(iid, "LIB", "Library", 3, "", 1)
    store.playlists.set_playlist_tracks(library, entries)
    target = store.playlists.upsert_playlist(iid, "TARGET", "Target", 1, "", 1)
    store.playlists.set_playlist_tracks(target, entries[:1])
    artists = [store.artists.key(store.artists.track_credits(tid)[0]["artist_id"]) for tid in entries]
    assert artists[1] != artists[2]
    ranks, reads = [], []
    track_keys = type(store.artists).track_keys

    def rank(_store, seeds):
        ranks.append(seeds)
        return {artists[0]: 1.0, artists[1]: 0.8, artists[2]: 0.6}

    def credits(repo, **kwargs):
        reads.append(kwargs)
        return track_keys(repo, **kwargs)

    def public(*args, **kwargs):
        raise AssertionError("Internal completion must retain canonical artist keys")

    monkeypatch.setattr(artist_model, "_blended_set_scores", rank)
    monkeypatch.setattr(type(store.artists), "track_keys", credits)
    monkeypatch.setattr(type(store.artists), "public_keys", public)
    suggestions = surfaces.complete_playlist(store, target, limit=4, now=1)
    assert [item.video_id for item in suggestions] == ["one", "two"]
    assert [item.score for item in suggestions] == [0.8, 0.6]
    assert ranks == [{artists[0]}] and reads == [{}]


def test_shared_artist_scores_expand_owned_and_discovered_tracks_without_reranking(store, monkeypatch):
    iid = store.identities.upsert_identity("main", "bridge", None, True)
    tid = store.tracks.upsert_track("owned", "Owned song", "Related", None, 200)
    library = store.playlists.upsert_playlist(iid, "LIB", "Library", 1, "", 1)
    store.playlists.set_playlist_tracks(library, [tid])
    store.discovery.upsert_discovered_track("new song|related", "new", "New song", "Related", "",
                                            None, "Techno", None, "src", 1)
    scores = {store.artists.key("Related"): 0.7}
    refs = store.artists.track_keys()

    def unexpected(*args, **kwargs):
        raise AssertionError("The caller already supplied artist scores and credits")

    monkeypatch.setattr(artist_model, "related_artist_scores", unexpected)
    monkeypatch.setattr(type(store.artists), "track_keys", unexpected)
    candidates = artist_model.artist_track_candidates(store, [], scores=scores, artist_refs=refs)
    assert {row["video_id"] for row in candidates} == {"owned", "new"}
    assert all(row["artist_score"] == 0.7 for row in candidates)
    owned = artist_model.artist_track_candidates(
        store, [], scores=scores, artist_refs=refs, include_out_of_corpus=False)
    assert [row["video_id"] for row in owned] == ["owned"]
