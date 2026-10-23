"""Batch display keys retain identity semantics without rescanning aliases per artist."""
from yt_playlist.core import artists


def test_bulk_public_keys_preserve_alias_ambiguity_redirects_and_missing_ids(store, monkeypatch):
    ids = [store.artists.ensure_artist(f"Artist {i}") for i in range(40)]
    first = store.artists.ensure_artist("Echo", provider="youtube", provider_id="first")
    second = store.artists.ensure_artist("Echo", provider="youtube", provider_id="second")
    retired = store.artists.ensure_artist("Retired")
    target = store.artists.ensure_artist("Artist 0", provider="youtube", provider_id="canonical")
    with store.conn:
        store.conn.execute("INSERT INTO artist_redirects VALUES (?,?,?,?)",
                           (retired, target, "test", 1))
        store.conn.execute("INSERT INTO artist_aliases VALUES (?,?,?)",
                           (retired, "ARTIST 0", "test"))
    ids.extend([first, second, retired, target, 999999])
    expected = {aid: artists.public_key(store.conn, aid) for aid in ids}
    alias_count = store.conn.execute("SELECT COUNT(*) FROM artist_aliases").fetchone()[0]
    calls = []
    normalize = artists.normalize

    def counted(value):
        calls.append(value)
        return normalize(value)

    monkeypatch.setattr(artists, "normalize", counted)
    assert artists.public_keys(store.conn, ids) == expected
    assert expected[first] == artists.token(first)
    assert expected[second] == artists.token(second)
    assert expected[retired] == expected[target]
    assert len(calls) <= alias_count + len(ids)


def test_library_artist_batch_keeps_homonyms_distinct(store):
    store.tracks.upsert_track("first", "One", "Echo", None, None,
                             credits=[{"name": "Echo", "id": "first-artist"}])
    store.tracks.upsert_track("second", "Two", "Echo", None, None,
                             credits=[{"name": "Echo", "id": "second-artist"}])
    store.tracks.upsert_track("third", "Three", "Unique", None, None)
    ids = [r[0] for r in store.conn.execute("SELECT DISTINCT artist_id FROM track_artists")]
    assert store.rec_query.library_artists() == {
        artists.public_key(store.conn, aid) for aid in ids}
