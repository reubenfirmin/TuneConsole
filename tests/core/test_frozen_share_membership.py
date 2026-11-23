"""The last cached share retains exact provider order independently of tracklists."""
import pytest

from yt_playlist.core import album_tracklists, albums


def test_exact_share_and_album_fallback_preserve_order_and_empty_captures(store):
    with store.conn:
        album = albums.observe(store.conn, 'release', title='Release', artist='Artist')
        for browse in ('alias', 'no-share'):
            store.conn.execute(
                "INSERT INTO album_provider_ids(provider,provider_id,album_id,is_primary,equivalence_digest,verified_at) "
                "VALUES ('youtube',?,?,0,?,1)", (browse, album, 'a' * 64))
        album_tracklists.freeze(store.conn, album, 'release', 'Exact', ['one', 'two', 'one'])
        album_tracklists.freeze(store.conn, album, 'alias', 'Empty', [])

    assert album_tracklists.cached_share(store.conn, album, 'release') == {
        'title': 'Exact', 'video_ids': ['one', 'two', 'one']}
    assert album_tracklists.cached_share(store.conn, album, 'no-share') == {
        'title': 'Empty', 'video_ids': []}
    with pytest.raises(RuntimeError, match='abort'):
        with store.conn:
            album_tracklists.freeze(store.conn, album, 'release', 'Replacement', ['new'])
            raise RuntimeError('abort')
    assert album_tracklists.cached_share(store.conn, album, 'release')['video_ids'] == ['one', 'two', 'one']
    assert not store.conn.execute('SELECT 1 FROM album_tracklists').fetchone()
