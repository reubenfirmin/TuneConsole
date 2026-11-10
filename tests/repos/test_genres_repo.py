"""DAO suite for GenreRepo (genre whitelist + collected genres)."""


def test_whitelist_add_remove_set(store):
    store.genres.add_genre("Jazz"); store.genres.add_genre("Rock")
    assert store.genres.get_genre_whitelist() == ["Jazz", "Rock"]
    store.genres.remove_genre("Jazz")
    assert store.genres.get_genre_whitelist() == ["Rock"]
    store.genres.set_genres(["Pop", "ambient"])
    # Current preferences retain genre identity and display its catalog name.
    assert store.genres.get_genre_whitelist() == ["Ambient", "Pop"]


def test_add_is_case_insensitively_idempotent(store):
    store.genres.add_genre("Jazz"); store.genres.add_genre("jazz")    # one resolved identity
    assert store.genres.get_genre_whitelist() == ["Jazz"]


def test_all_genres_empty_without_track_genres(store):
    assert store.genres.all_genres() == []


def test_catalog_configuration_batches_redirect_reads_and_keeps_current_keys(store):
    from yt_playlist.util import genre_catalog

    store.genres.add_genre('Old custom jazz')
    store.genres.add_genre('Current custom jazz')
    old, current = 'old custom jazz', 'current custom jazz'
    with store.conn:
        store.conn.execute('INSERT INTO genre_redirects VALUES (?,?)', (old, current))
        store.conn.execute('DELETE FROM genre_preferences WHERE genre_key IN (?,?)', (old, current))
    record = {**genre_catalog.catalog()[0]['jazz'], 'key': old, 'name': 'Old custom jazz'}
    family = record['family']
    families = {family: genre_catalog.family_labels()[family]} if family else {}
    queries = []
    store.conn.set_trace_callback(queries.append)
    try:
        with store.genres.repository.transaction():
            store.genres.repository.seed_catalog({old: record},
                {'old custom alias': (old, 'Old custom alias', 'custom')}, families)
            store.genres.repository.configure_preferences([(current, 'custom')], [old, current])
    finally:
        store.conn.set_trace_callback(None)
    assert store.conn.execute('SELECT genre_key FROM genre_alias WHERE label_key=?',
                              ('old custom alias',)).fetchone()[0] == current
    assert store.conn.execute('SELECT genre_key FROM genre_external_id WHERE external_id=?',
                              (record['mbid'],)).fetchone()[0] == current
    assert list(map(tuple, store.conn.execute('SELECT genre_key,enabled FROM genre_preferences '
        'WHERE genre_key IN (?,?)', (old, current)))) == [(current, 1)]
    assert sum(query.startswith('SELECT retired_key,genre_key FROM genre_redirects') for query in queries) == 2
