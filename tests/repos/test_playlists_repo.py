"""DAO suite for PlaylistRepo (playlists, membership, groups, hidden flags)."""


def test_upsert_tracks_change_detection(store):
    iid = store.upsert_identity("me", "c", None, True)
    p = store.playlists.upsert_playlist(iid, "P", "Title", 3, "hash1", 1.0)
    same = store.playlists.upsert_playlist(iid, "P", "Title", 3, "hash1", 2.0)   # unchanged hash
    assert p == same
    assert store.playlists.get_playlist(p).last_changed == 1.0                    # not bumped
    store.playlists.upsert_playlist(iid, "P", "Title", 4, "hash2", 3.0)           # changed hash
    assert store.playlists.get_playlist(p).last_changed == 3.0


def test_set_tracks_dedupes_and_orders(store):
    iid = store.upsert_identity("me", "c", None, True)
    p = store.playlists.upsert_playlist(iid, "P", "P", 0, "h", 0.0)
    a = store.upsert_track("v1", "A", "Ar", "Al", 100)
    b = store.upsert_track("v2", "B", "Ar", "Al", 100)
    store.playlists.set_playlist_tracks(p, [a, b, a])                             # duplicate a dropped
    assert store.playlists.get_playlist_track_ids(p) == [a, b]


def test_set_song_liked_toggles_lm_membership(store):
    iid = store.upsert_identity("me", "c", None, True)
    lm = store.playlists.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 0.0)
    store.upsert_track("v1", "Song", "Artist", "Al", 100)
    store.playlists.set_song_liked(iid, "v1", True)
    assert len(store.playlists.get_playlist_track_ids(lm)) == 1
    store.playlists.set_song_liked(iid, "v1", True)                               # idempotent, no dup
    assert len(store.playlists.get_playlist_track_ids(lm)) == 1
    store.playlists.set_song_liked(iid, "v1", False)
    assert store.playlists.get_playlist_track_ids(lm) == []


def test_remove_playlist_prunes_links_keeps_group(store):
    iid = store.upsert_identity("me", "c", None, True)
    p = store.playlists.upsert_playlist(iid, "PX", "Gone", 0, "h", 0.0)
    store.playlists.set_playlist_group("PX", "Faves")
    store.playlists.remove_playlist(p)
    assert store.playlists.get_playlist(p) is None
    assert store.playlists.get_playlist_groups() == {"PX": "Faves"}               # group survives


def test_hide_and_groups(store):
    store.playlists.hide_playlist("P1")
    assert store.playlists.get_hidden_playlists() == {"P1"}
    store.playlists.unhide_playlist("P1")
    assert store.playlists.get_hidden_playlists() == set()
    store.playlists.set_playlist_group("P1", "Mood")
    store.playlists.set_playlist_group("P1", "")                                  # blank clears
    assert store.playlists.get_playlist_groups() == {}


def test_facade_delegates(store):
    iid = store.upsert_identity("me", "c", None, True)
    p = store.upsert_playlist(iid, "P", "P", 0, "h", 0.0)                         # legacy store.x() call site
    assert store.get_playlist(p).ytm_playlist_id == "P"