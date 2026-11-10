"""A complete worker-published offer for onboarding transition checks."""
from tests.conftest import seed_catalog_track
from tests.home_fixture_support import publish_menu


def finish_partial_import(store, now=1000):
    """One verified playlist with unrelated continuation failures (#559)."""
    from yt_playlist.library.sync_outcomes import finish_attempt

    return finish_attempt(store, {
        'status': 'partial', 'accounts': {'1': {
            'label': 'Main', 'status': 'partial', 'resources': {
                'playlist_listing': {'status': 'complete', 'entries': [
                    {'playlistId': 'PL_READY', 'title': 'Liked Music'},
                    {'playlistId': 'PL_RETRY', 'title': 'Alternative Mix'}]},
                'playlists': {'status': 'partial', 'items': {
                    'PL_READY': {'status': 'complete'},
                    'PL_RETRY': {'status': 'failed', 'message': 'Incomplete continuation page'}}},
            }}}, 'resources': {},
    }, now)


def publish_personal_mix(store, now=1000, count=4, *, tracks_per_card=4):
    payload = publish_menu(store, now, count=count, tracks_per_card=tracks_per_card, surface='home_tuned')
    return [track for proto in payload['menus'][0]['protos'] for track in proto['tracks']]


def publish_personal_album(store, now=1000, count=1):
    seed_catalog_track(store, 'album track|artist', 'Album track', 'Artist', video_id='album-track')
    store.rec_proposals.put_proposals('album_prepared', {
        'built_at': now, 'context': {}, 'items': [{
            'browse_id': 'first-album' if index == 0 else f'next-album-{index}',
            'title': 'Your next record' if index == 0 else f'Another record {index}',
            'artist': 'Artist' if index == 0 else f'Another artist {index}',
            'classification': 'discovery', 'score': .9, 'genre': 'Jazz'} for index in range(count)]}, now)
