import pytest


def test_genre_merge_retries_after_discovery_commits_and_taxonomy_write_fails(store, monkeypatch):
    store.genres.add_genre('Source style')
    store.genres.add_genre('Target style')
    for origin, now in [('genre:Source style', 1), ('genre:Target style', 2)]:
        store.discovery.upsert_discovered_track('song|artist', 'video', 'Song', 'Artist', None, None,
                                                None, None, origin, now)
    assert store.conn.execute('SELECT count(*) FROM discovery_origins').fetchone()[0] == 2
    repository = store.genres.repository
    original = repository.apply_merge
    merge_origins = store.discovery.merge_genre_origins
    def checked_origins(previous, target):
        assert not store.conn.in_transaction
        merge_origins(previous, target)
        assert not store.conn.in_transaction
    def failed_taxonomy(*args, **kwargs):
        assert store.conn.in_transaction
        original(*args, **kwargs)
        raise RuntimeError('taxonomy interrupted')
    with monkeypatch.context() as patch:
        patch.setattr(store.discovery, 'merge_genre_origins', checked_origins)
        patch.setattr(repository, 'apply_merge', failed_taxonomy)
        with pytest.raises(RuntimeError, match='taxonomy interrupted'):
            store.genres.merge_genres('source style', 'target style', now=10)
    assert not store.conn.in_transaction
    assert not store.conn.execute('SELECT 1 FROM genre_redirects').fetchone()
    assert not store.conn.execute('SELECT 1 FROM genre_merge_preferences').fetchone()
    assert list(store.conn.execute('SELECT genre_key FROM discovery_origins'))[0][0] == 'target style'
    assert store.conn.execute('SELECT count(*) FROM discovery_sources').fetchone()[0] == 1
    store.genres.merge_genres('source style', 'target style', now=11)
    assert store.conn.execute('SELECT retired_key,genre_key FROM genre_redirects').fetchone()[:] == (
        'source style', 'target style')
    assert store.conn.execute('SELECT count(*) FROM genre_merge_preferences').fetchone()[0] == 2
    assert store.conn.execute('SELECT first_found_at,last_found_at FROM discovery_sources').fetchone()[:] == (1, 2)
