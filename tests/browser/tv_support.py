"""Prepared video rows shared by TV browser journeys."""
from tests.test_tv_stream import video
from yt_playlist.rec import tv, tv_artwork
from yt_playlist.util.matching import search_squash

MODEL = 'Alpine.$data(document.querySelector(".tv-home"))'


def publish_pool(store, live_app, count):
    items = [{**video(i), 'thumbnail': live_app + '/static/favicon.svg'} for i in range(count)]
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'continuous', 'at': 1, 'attempted': [], 'items': items}, 1)
    store.rec_proposals.put_proposals(tv_artwork.SURFACE, {
        search_squash(item['artist']): {'images': [live_app + '/tv-artist.svg'], 'retry_at': 86401}
        for item in items}, 1)
