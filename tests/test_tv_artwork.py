"""Artist photography precedes the last-resort video thumbnail."""
from tests.conftest import FakeClient
from yt_playlist.rec import tv_artwork
from yt_playlist.util.matching import search_squash


def test_existing_artist_photo_and_independent_alternatives_are_cached(store, monkeypatch):
    store.discovery.upsert_discovered_artist('Artist', 1, [], [], 'https://img/artist.jpg', 1)
    client = FakeClient(search_results=[
        {'artist': 'Different artist', 'thumbnails': [{'url': 'https://img/wrong.jpg'}]},
        {'artist': 'Artist', 'resultType': 'artist', 'thumbnails': [{'url': 'https://img/provider.jpg'}]}])
    monkeypatch.setattr(tv_artwork.wikipedia, 'fetch_artwork', lambda artist: {'url': 'https://img/wiki.jpg'})
    assert not tv_artwork.prepare(store, client, ['Artist'], 1)
    images, cached = tv_artwork.images(store, 'Artist')
    assert images == ['https://img/artist.jpg', 'https://img/provider.jpg', 'https://img/wiki.jpg']
    assert cached and cached['retry_at'] == 86401
    assert not tv_artwork.prepare(store, client, ['Artist'], 2)


def test_failed_sources_have_a_bounded_retry_and_do_not_discard_good_photos(store, monkeypatch):
    store.discovery.upsert_discovered_artist('Artist', 1, [], [], 'https://img/artist.jpg', 1)
    def offline(*args, **kwargs):
        raise ConnectionError('offline')
    client = FakeClient()
    monkeypatch.setattr(client, 'search', offline)
    monkeypatch.setattr(tv_artwork.wikipedia, 'fetch_artwork', offline)
    assert not tv_artwork.prepare(store, client, ['Artist'], 1)
    images, cached = tv_artwork.images(store, 'Artist')
    assert images == ['https://img/artist.jpg'] and cached['retry_at'] == 61
    # Only one artist per slice, and never unbounded retained image metadata.
    store.rec_proposals.put_proposals(tv_artwork.SURFACE, {
        search_squash(f'Artist {i}'): {'images': [], 'retry_at': 1} for i in range(60)}, 1)
    assert tv_artwork.prepare(store, client, ['One', 'Two'], 62)
    assert len(store.rec_proposals.get_proposals(tv_artwork.SURFACE)) == 60


def test_saved_artist_requests_are_prepared_outside_current_video_pool(store, monkeypatch):
    store.discovery.upsert_discovered_artist('Saved artist', 1, [], [], None, 1)
    assert not tv_artwork.request(store, 'Unknown artist', 1)
    assert tv_artwork.request(store, 'Saved artist', 1)
    client = FakeClient(search_results=[{'artist': 'Saved artist', 'resultType': 'artist',
                                       'thumbnails': [{'url': 'https://img/saved.jpg'}]}])
    monkeypatch.setattr(tv_artwork.wikipedia, 'fetch_artwork', lambda name: None)
    assert not tv_artwork.prepare(store, client, [], 2)
    assert tv_artwork.images(store, 'Saved artist')[0] == ['https://img/saved.jpg']
    assert not store.rec_proposals.get_proposals(tv_artwork.REQUESTS)
