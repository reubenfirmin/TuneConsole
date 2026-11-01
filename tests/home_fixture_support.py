"""Small published Home menus for tests of consumers, not preparation."""
from tests.conftest import seed_catalog_track
from yt_playlist.rec import home_cards, home_prepared


def publish_menu(store, now=1000, *, prefix='prepared', count=4, tracks_per_card=4,
                 surface='home_prepared', modeled=False):
    """Publish real subjects through the cache writer without running ranking.

    Different prefixes own disjoint tracks. Preparation, readiness and ranking
    tests should continue to build their own real inputs.
    """
    protos = []
    for index, lane in enumerate(('comfort', 'wheelhouse', 'explore', 'temporal')[:count]):
        tracks = []
        for offset in range(tracks_per_card):
            number = index * tracks_per_card + offset
            title, video = f'{prefix.title()} {number}', f'{prefix}-{number}'
            key = f'{prefix} {number}|artist'
            seed_catalog_track(store, key, title, 'Artist', video_id=video, genre='house')
            tracks.append({'key': key, 'title': title, 'artist': 'Artist', 'video_id': video,
                           'album': '', 'thumbnail': None, 'genre': 'house'})
        proto = home_cards._proto(store, lane, f'A {lane} mix for you', tracks, now)
        proto['recipe'] = {'lane': lane}
        if modeled:
            proto['mode_id'] = None
        protos.append(proto)
    result = home_cards.CardsResult(protos, [{'proto': proto, 'pool': None} for proto in protos],
                                   None, None, [], [], home_cards._epoch(store, 'cards'), None)
    previous = store.rec_proposals.get_proposals('home_prepared') if surface == 'home_tuned' else None
    home_prepared.remember(store, previous, result, now)
    payload = store.rec_proposals.get_proposals('home_tuned')
    assert home_prepared._valid(payload)
    if surface != 'home_tuned':
        store.rec_proposals.put_proposals(surface, payload, now)
        store.rec_proposals.put_proposals('home_tuned', {}, now)
        store.rec_proposals.put_proposals('home_choice', {}, now)
    return payload
