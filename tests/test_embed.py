import numpy as np
import pytest

from yt_playlist.rec import content_model, embed, surfaces
from tests import model_support
from yt_playlist.util.matching import identity_key


def _two_clusters(store):
    """Two disjoint 6-track clusters (A in playlist PA, B in PB). identity_key = 'a0|ab' etc."""
    iid = store.identities.upsert_identity("main", "cred", None, True)
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(6)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(6)]
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "PA", 6, "h", 0.0), A)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PB", "PB", 6, "h2", 0.0), B)
    return iid, A, B


def test_embedding_neighbors_stay_in_cluster(store):
    _two_clusters(store)
    n = model_support.build_track_model(store, dim=4)
    assert n == 12
    nbrs = embed.neighbors(store, "a0|ab", topn=4)
    assert nbrs, "expected neighbours once vectors are built"
    assert all(k.endswith("|ab") for k, _ in nbrs)   # A-cluster only, never B


def test_neighbors_empty_before_build(store):
    _two_clusters(store)
    assert embed.neighbors(store, "a0|ab") == []      # no vectors yet -> no neighbours


def test_neighbors_for_unmodeled_uses_artist_proxy(store):
    # A generated (quarantined) track by artist 'AB' gets no vector of its own, but 'songs like this'
    # should still work, proxied through the AB tracks that ARE in the model.
    iid, A, B = _two_clusters(store)
    g = store.tracks.upsert_track("gnew", "GNEW", "AB", None, None)
    gpl = store.playlists.upsert_playlist(iid, "PG", "Gen", 1, "h2", 0.0)
    store.playlists.set_playlist_tracks(gpl, [g])
    store.playlists.set_playlist_group("PG", "Generated")
    store.playlists.mark_generated_playlist('PG')
    model_support.build_track_model(store, dim=4)

    seed = identity_key("GNEW", "AB")
    assert embed.neighbors(store, seed) == []                   # quarantined: no vector of its own
    nbrs = embed.neighbors_for_unmodeled(store, seed, topn=4)
    assert nbrs and all(k.endswith("|ab") for k, _ in nbrs)     # proxied to the artist's A-cluster


def _credited_track(store, video, title, *credits):
    store.tracks.upsert_track(video, title, ", ".join(name for name, _ in credits), None, None,
                             credits=[{"name": name, "id": aid} for name, aid in credits])
    return store.tracks.identity_key_for_video(video)


def _fixed_vectors(store, rows):
    store.rec_model.replace_rec_vectors([
        (key, np.asarray(vector, dtype=np.float32).tobytes()) for key, vector in rows])


@pytest.mark.parametrize("own_modeled", [False, True])
def test_unmodeled_proxy_keeps_verified_homonyms_separate(store, own_modeled):
    _credited_track(store, "seed", "Same song", ("Same", "artist-a"))
    other = _credited_track(store, "other", "Same song", ("Same", "artist-b"))
    seed = store.tracks.identity_key_for_video("seed")
    assert seed != other  # even disambiguated recording keys retain the same display suffix
    rows = [(other, [0, 1])]
    if own_modeled:
        own = _credited_track(store, "own", "Own song", ("Same", "artist-a"))
        rows.append((own, [1, 0]))
    _fixed_vectors(store, rows)
    before = store.rec_model.get_rec_vectors()

    neighbors = embed.neighbors_for_unmodeled(store, seed)

    if own_modeled:
        assert neighbors[0] == (own, pytest.approx(1))
        assert dict(neighbors)[other] == pytest.approx(0)
    else:
        assert neighbors == []
    assert store.rec_model.get_rec_vectors() == before


def test_unmodeled_proxy_follows_identified_artist_aliases(store):
    modeled = _credited_track(store, "modeled", "Older song", ("Old spelling", "artist-a"))
    seed = _credited_track(store, "seed", "New song", ("New spelling", "artist-a"))
    _fixed_vectors(store, [(modeled, [1, 0])])

    assert embed.neighbors_for_unmodeled(store, seed) == [(modeled, pytest.approx(1))]
    assert embed.neighbors_for_unmodeled(store, modeled) == []  # modeled seeds use normal retrieval
    assert seed not in dict(store.rec_model.get_rec_vectors())


def test_unmodeled_proxy_counts_each_recording_once_across_all_seed_credits(store):
    first = _credited_track(store, "first", "First solo", ("First", "a"))
    second = _credited_track(store, "second", "Second solo", ("Second", "b"))
    duet = _credited_track(store, "duet", "Duet", ("First", "a"), ("Second", "b"))
    seed = _credited_track(store, "seed", "New duet", ("Second alias", "b"), ("First", "a"))
    unrelated = _credited_track(store, "other", "Unrelated", ("Other", "c"))
    _fixed_vectors(store, [(first, [1, 0]), (second, [0, 1]), (duet, [0, 1]),
                           (unrelated, [.6, .8])])

    scores = dict(embed.neighbors_for_unmodeled(store, seed))

    # Any shared canonical credit qualifies; the duet contributes one row, not one per credit.
    assert scores == pytest.approx({first: 1 / np.sqrt(5), second: 2 / np.sqrt(5),
                                   duet: 2 / np.sqrt(5), unrelated: 2.2 / np.sqrt(5)})
    assert seed not in scores


def test_unmodeled_proxy_does_not_guess_an_unresolved_seeds_credits(store):
    modeled = _credited_track(store, "modeled", "Known song", ("Same", "artist-a"))
    _fixed_vectors(store, [(modeled, [1, 0])])

    assert embed.neighbors_for_unmodeled(store, "unknown recording|same") == []


def test_for_you_uses_taste_neighbourhood_when_built(store):
    iid, A, _ = _two_clusters(store)
    store.history.record_history_plays(iid, 1.0, ["a0|ab", "a1|ab"], source_timezone="UTC")
    model_support.build_track_model(store, dim=4)
    items = surfaces.for_you(store, now=1000.0)
    assert any(i.reason == "In your taste neighbourhood" for i in items)


def test_complete_playlist_uses_same_artist_and_genre_not_track_embedding(store):
    iid, A, B = _two_clusters(store)
    for tid in A:
        store.tracks.set_track_genre(tid, "Techno")
    for tid in B:
        store.tracks.set_track_genre(tid, "Rock")
    target = store.playlists.upsert_playlist(iid, "PT", "Target", 2, "h3", 0.0)
    store.playlists.set_playlist_tracks(target, [A[0], A[1]])             # seed with two A tracks
    model_support.build_track_model(store, dim=4)
    items = surfaces.complete_playlist(store, target, limit=4)
    assert items
    assert all(i.artist == "AB" for i in items)                # same artist + genre; B is ineligible


# --- content-space fingerprint -------------------------------------------------------------------
# Taste-mode centroids are persisted vectors in this space; the fingerprint is how they detect that
# the space they were built in no longer exists (see rec/taste_modes.reconcile).

def test_fingerprint_is_stable_for_the_same_space():
    m = {"cat": {"rock": 0, "pop": 1}, "cont": [["bpm", 120.0, 10.0]]}
    assert content_model.content_model_fingerprint(m) == content_model.content_model_fingerprint(dict(m))


def test_fingerprint_changes_when_a_token_is_added():
    a = {"cat": {"rock": 0, "pop": 1}, "cont": []}
    b = {"cat": {"rock": 0, "pop": 1, "jazz": 2}, "cont": []}
    assert content_model.content_model_fingerprint(a) != content_model.content_model_fingerprint(b)


def test_fingerprint_changes_when_columns_are_reordered():
    """Same dimension, different basis: the case a shape check cannot catch."""
    a = {"cat": {"rock": 0, "pop": 1}, "cont": []}
    b = {"cat": {"rock": 1, "pop": 0}, "cont": []}
    assert content_model.content_model_fingerprint(a) != content_model.content_model_fingerprint(b)


def test_fingerprint_changes_when_a_continuous_feature_drops_out():
    a = {"cat": {}, "cont": [["bpm", 120.0, 10.0], ["energy", 0.5, 0.1]]}
    b = {"cat": {}, "cont": [["bpm", 120.0, 10.0]]}
    assert content_model.content_model_fingerprint(a) != content_model.content_model_fingerprint(b)


def test_fingerprint_ignores_zscore_drift():
    """mu/sd move on every rebuild; treating that as a new space would retire all modes each pass."""
    a = {"cat": {"rock": 0}, "cont": [["bpm", 120.0, 10.0]]}
    b = {"cat": {"rock": 0}, "cont": [["bpm", 118.4, 11.2]]}
    assert content_model.content_model_fingerprint(a) == content_model.content_model_fingerprint(b)
