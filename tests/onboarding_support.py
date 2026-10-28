"""A complete worker-published offer for onboarding transition checks."""
from tests.conftest import seed_catalog_track
from yt_playlist.rec import home_cards, home_prepared


def publish_personal_mix(store, now=1000):
    tracks = []
    for i in range(4):
        key = f"prepared {i}|artist"
        seed_catalog_track(store, key, f"Prepared {i}", "Artist", video_id=f"prepared-{i}", genre="house")
        tracks.append({"key": key, "title": f"Prepared {i}", "artist": "Artist",
                       "video_id": f"prepared-{i}", "album": "", "thumbnail": None, "genre": "house"})
    proto = home_cards._proto(store, "comfort", "A mix for you", tracks, now)
    proto["recipe"] = {"lane": "comfort"}
    result = home_cards.CardsResult([proto], [{"proto": proto, "pool": None}], None, None,
                                    [], [], home_cards._epoch(store, "cards"), None)
    home_prepared.remember(store, store.rec_proposals.get_proposals("home_prepared"), result, now)
    return tracks


def publish_personal_album(store, now=1000):
    seed_catalog_track(store, 'album track|artist', 'Album track', 'Artist', video_id='album-track')
    store.rec_proposals.put_proposals('album_prepared', {
        'built_at': now, 'context': {}, 'items': [{
            'browse_id': 'first-album', 'title': 'Your next record', 'artist': 'Artist',
            'classification': 'discovery', 'score': .9, 'genre': 'Jazz'}]}, now)
