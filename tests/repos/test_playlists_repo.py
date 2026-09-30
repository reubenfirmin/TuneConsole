"""DAO suite for PlaylistRepo (playlists, membership, groups, hidden flags)."""


def test_creation_date_is_independent_of_sync_and_edit_dates(store):
    iid = store.upsert_identity("me", "c", None, True)
    known = store.upsert_playlist(iid, "NEW", "New", 0, "", 100, created_at=100)
    imported = store.upsert_playlist(iid, "OLD", "Imported", 0, "", 100)
    store.upsert_playlist(iid, "NEW", "Renamed", 2, "changed", 200)
    store.set_playlist_title(known, "Renamed again", 300)
    store.set_playlist_track_count(known, 3, 400)
    assert store.get_playlist(known).created_at == 100
    assert store.get_playlist(imported).created_at is None
    # A real date can be supplied later without resetting it on subsequent syncs.
    store.upsert_playlist(iid, "OLD", "Imported", 0, "", 200, created_at=10)
    store.upsert_playlist(iid, "OLD", "Imported", 0, "", 300, created_at=300)
    assert {p.ytm_playlist_id: p.created_at for p in store.get_playlists()} == {"NEW": 100, "OLD": 10}


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


def test_initial_thumbnail_is_persisted_without_changing_playlist_dates(store):
    iid = store.upsert_identity("me", "c", None, True)
    pid = store.upsert_playlist(iid, "P", "Playlist", 0, "h", 100, thumbnail="", created_at=100)
    store.ensure_playlist_thumbnail(pid)
    assert store.get_playlist(pid).thumbnail is None
    blank = store.upsert_track("blank", "Blank", "Artist", None, None, thumbnail="")
    art = store.upsert_track("art", "Artwork", "Artist", None, None, thumbnail="https://example.com/song.jpg")
    store.set_playlist_tracks(pid, [blank, art])
    before = store.get_playlist(pid)
    store.ensure_playlist_thumbnail(pid)
    store.ensure_playlist_thumbnail(pid)
    after = store.get_playlist(pid)
    assert after.thumbnail == "https://example.com/song.jpg"
    assert (after.first_seen, after.last_seen, after.last_changed, after.created_at) == (
        before.first_seen, before.last_seen, before.last_changed, before.created_at)
    # Missing remote artwork preserves the initial cover; a real remote cover supersedes it.
    store.upsert_playlist(iid, "P", "Playlist", 2, "h", 200, thumbnail="")
    assert store.get_playlist(pid).thumbnail == after.thumbnail
    store.upsert_playlist(iid, "P", "Playlist", 2, "h", 300, thumbnail="https://example.com/playlist.jpg")
    store.ensure_playlist_thumbnail(pid)
    assert store.get_playlist(pid).thumbnail == "https://example.com/playlist.jpg"


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


def test_remove_playlist_prunes_cleanup_dismissals(store):
    iid = store.upsert_identity("me", "c", None, True)
    p = store.playlists.upsert_playlist(iid, "PX", "Gone", 0, "h", 0.0)
    store.ignore_cleanup("PX", "empty", 1.0)
    store.ignore_cleanup("PY", "tiny", 1.0)                    # other playlist, must survive
    store.ignore_merge("sig-px", ["PX", "PY"], 1.0)
    store.ignore_merge("sig-other", ["PY", "PZ"], 1.0)         # doesn't involve PX, must survive
    store.playlists.remove_playlist(p)
    assert store.get_cleanup_ignored() == {"tiny": {"PY"}}
    assert store.get_ignored_merge_sigs() == {"sig-other"}


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
