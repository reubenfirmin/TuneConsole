"""Compose verified compact selections with the ordinary Vibe pool fallback."""
from yt_playlist.rec import vibe_pools, vibe_readiness


def assemble(store, now, preset, epoch=0, *, bundles=None, include_genres=(), avoid_genres=()):
    if bundles is None and getattr(store, '_recommendation_files', False):
        if not getattr(store, '_read_only', False):
            with store.read_view() as reader:
                return assemble(reader, now, preset, epoch,
                                include_genres=include_genres, avoid_genres=avoid_genres)
        if not (include_genres or avoid_genres):
            indexed = _indexed_cards(store, now, preset, epoch)
            if indexed is not None:
                return vibe_pools._finish_cards(store, preset, indexed)
    return vibe_pools.assemble(store, now, preset, epoch, bundles=bundles,
                               include_genres=include_genres, avoid_genres=avoid_genres)


def _indexed_cards(store, now, preset, epoch):
    """Select from verified compact inputs before hydrating the retained tracks."""
    selections = dict(vibe_readiness.menus(store, now, epoch))
    if preset not in selections:
        return None
    cards = selections[preset]
    generation = (store.recommendations.head('vibe:' + preset) or {}).get('id')
    selections = [('', card['pool_lane'], [item['key'] for item in card['tracks']]) for card in cards]
    pages = store.recommendations.selected_candidates('vibe:' + preset, selections, generation=generation)
    result = []
    for card, tracks in zip(cards, pages, strict=True):
        by_key = {item['key']: item for item in tracks}
        if set(by_key) != {item['key'] for item in card['tracks']}:
            return None
        result.append({**card, 'tracks': [{**by_key[item['key']], 'musical': {'_rank_only': True}}
                                         for item in card['tracks']]})
    return result
