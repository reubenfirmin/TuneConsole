"""DAO suite for ChartsRepo distribution queries that back the ticker charts:
listen_distribution (plays per category in a time window) and corpus_distribution
(library composition per category), across genre / year / album / playlist dimensions.
"""

NOW = 11*86400.0+43200


def _seed(store):
    """Three songs, two playlists, two recent days and one ~100d old.

    techno appears twice in the corpus (Alpha + Gamma); house once (Beta).
    Recent days: Alpha on both, Beta on one. Old day: Gamma.
    """
    iid = store.identities.upsert_identity("me", "c", None, True)
    a = store.tracks.upsert_track("v1", "Alpha", "Artist A", "Album A", 200, album_browse_id="album-a")
    b = store.tracks.upsert_track("v2", "Beta", "Artist B", "Album B", 200, album_browse_id="album-b")
    g = store.tracks.upsert_track("v3", "Gamma", "Artist A", "Album A", 200, album_browse_id="album-a")
    store.tracks.set_track_enrichment(a, "techno", "1995")   # 1990s
    store.tracks.set_track_enrichment(b, "house", "2005")    # 2000s
    store.tracks.set_track_enrichment(g, "techno", "2015")   # 2010s
    p1 = store.playlists.upsert_playlist(iid, "P1", "Mix", 0, "h", 0.0)
    p2 = store.playlists.upsert_playlist(iid, "P2", "Chill", 0, "h", 0.0)
    store.playlists.set_playlist_tracks(p1, [a, b])
    store.playlists.set_playlist_tracks(p2, [g])

    def key(tid):
        return store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (tid,)).fetchone()["k"]

    ka, kb, kg = key(a), key(b), key(g)
    store.history.record_history_plays(iid, NOW, [ka, ka, kb], source_timezone="UTC")
    store.history.record_history_plays(iid, NOW-86400, [ka], source_timezone="UTC")
    store.history.record_history_plays(iid, NOW - 100 * 86400, [kg], source_timezone="UTC")
    return iid


# --- listen_distribution -------------------------------------------------------

def test_listen_distribution_genre_windowed_excludes_old(store):
    _seed(store)
    since = NOW - 7 * 86400
    assert store.charts.listen_distribution("genre", since=since) == {"techno": 2, "house": 1}


def test_listen_distribution_genre_alltime_includes_old(store):
    _seed(store)
    assert store.charts.listen_distribution("genre", since=None) == {"techno": 3, "house": 1}


def test_album_browse_ids_maps_release_key_to_browse(store):
    store.identities.upsert_identity("me", "c", None, True)
    store.tracks.upsert_track("v1", "Song", "Art", "Cool Album", 200, album_browse_id="MPREb_X")
    store.tracks.upsert_track("v2", "Single", "Art", "Loose Single", 200)   # no browse -> excluded
    ids = store.charts.album_browse_ids()
    assert len(ids) == 1
    key = next(iter(ids))
    assert key.startswith('@album:')
    assert ids[key] == 'MPREb_X'
    assert store.charts.album_labels()[key] == 'Cool Album'


def test_history_bounds_spans_earliest_to_latest(store):
    _seed(store)   # snapshots at NOW and NOW - 100d
    lo, hi = store.charts.history_bounds()
    assert lo == NOW - 100 * 86400
    assert hi == NOW


def test_history_bounds_empty_when_no_history(store):
    store.identities.upsert_identity("me", "c", None, True)
    assert store.charts.history_bounds() == (None, None)


def test_listen_distribution_respects_until_bound(store):
    _seed(store)
    # disjoint period (8d..365d ago): excludes the NOW snapshot, keeps the ~100d-old one (Gamma).
    out = store.charts.listen_distribution("genre", since=NOW - 365 * 86400, until=NOW - 7 * 86400)
    assert out == {"techno": 1}


def test_listen_distribution_year_buckets_by_decade(store):
    _seed(store)
    assert store.charts.listen_distribution("year", since=None) == {"1990": 2, "2000": 1, "2010": 1}


def test_listen_distribution_album(store):
    _seed(store)
    labels = store.charts.album_labels()
    assert {labels[k]:v for k,v in store.charts.listen_distribution("album", since=None).items()} == {"Album A": 3, "Album B": 1}


def test_listen_distribution_playlist_counts_per_membership(store):
    _seed(store)
    # Recent: Alpha x2 + Beta x1 all in Mix -> 3. Old: Gamma x1 in Chill -> 1.
    assert store.charts.listen_distribution("playlist", since=None) == {"Mix": 3, "Chill": 1}


# --- corpus_distribution -------------------------------------------------------

def test_listen_distribution_artist(store):
    _seed(store)   # Alpha/Gamma = Artist A, Beta = Artist B; recent plays Alpha x2, Beta x1
    assert store.charts.listen_distribution("artist", since=None) == {"Artist A": 3, "Artist B": 1}


def test_corpus_distribution_artist_counts_distinct_songs(store):
    _seed(store)
    assert store.charts.corpus_distribution("artist") == {"Artist A": 2, "Artist B": 1}


def test_corpus_distribution_genre_counts_distinct_songs(store):
    _seed(store)
    assert store.charts.corpus_distribution("genre") == {"techno": 2, "house": 1}


def test_corpus_distribution_year_decades(store):
    _seed(store)
    assert store.charts.corpus_distribution("year") == {"1990": 1, "2000": 1, "2010": 1}


def test_corpus_distribution_album(store):
    _seed(store)
    labels = store.charts.album_labels()
    assert {labels[k]:v for k,v in store.charts.corpus_distribution("album").items()} == {"Album A": 2, "Album B": 1}


def test_corpus_distribution_playlist_distinct_tracks(store):
    _seed(store)
    assert store.charts.corpus_distribution("playlist") == {"Mix": 2, "Chill": 1}


def test_distributions_skip_untagged(store):
    """A song with no genre/year is excluded from those dimensions (not bucketed as '')."""
    iid = store.identities.upsert_identity("me", "c", None, True)
    t = store.tracks.upsert_track("v1", "Solo", "Artist", "Alb", 200)   # no enrichment
    k = store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (t,)).fetchone()["k"]
    store.history.record_history_plays(iid, NOW, [k], source_timezone="UTC")
    assert store.charts.corpus_distribution("genre") == {}
    assert store.charts.listen_distribution("genre", since=None) == {}
    assert store.charts.corpus_distribution("year") == {}
