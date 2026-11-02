"""Read worker-prepared Vibe inputs; missing preparation never runs in requests."""
from yt_playlist.core import json_cache
from yt_playlist.rec import home_vibe, musical_character, rec_params, vibe_pools, vibe_preparation
from yt_playlist.rec.vibe_readiness_index import MAX_INDEX_BYTES, VERSION, compact, prepare, project


def load(store, include, avoid):
    def read():
        if getattr(store, '_recommendation_files', False):
            metadata = store.recommendations.output('home', 'ranking-evidence') or {}
            home = store.recommendations.head('home')
            if home and any((store.recommendations.head('vibe:' + preset) or {}).get('id') != home['id']
                            for preset in home_vibe.PRESETS):
                metadata = {}
        else:
            metadata = (store.rec_proposals.proposal_fields('mode_bundles', ('_meta',)) or {}).get('_meta', {})
        encoded = metadata.get('vibe_readiness')
        value = json_cache.decode(encoded) if isinstance(encoded, str) else None
        if (isinstance(value, dict) and value.get('version') == VERSION
                and value.get('policy') == home_vibe.VERSION
                and value.get('profile_policy') == musical_character.VERSION
                and value.get('inputs_policy') == vibe_preparation.serving_prefix()
                and (not (include or avoid) or (value.get('genre_reserves')
                    and value.get('genre_request') in (None, [sorted(include), sorted(avoid)])))):
            if (not getattr(store, '_recommendation_files', False)
                    or value.get('profile_state') == store.musical_profiles.readiness_fingerprint(
                        item['key'] for item in value['tracks'])):
                return value
        return None
    return store.memoized_read(('vibe-readiness-inputs', VERSION, tuple(include), tuple(avoid)), read)


def ready(store, now, epoch, *, include_genres=(), avoid_genres=()):
    if not getattr(store, '_read_only', False):
        with store.read_view() as reader:
            return ready(reader, now, epoch, include_genres=include_genres, avoid_genres=avoid_genres)
    value = load(store, include_genres, avoid_genres)
    if value is None:
        return False
    signatures = set()
    for preset, cards in _menus(store, value, home_vibe.PRESETS, now, epoch, include_genres, avoid_genres):
        if len(cards) < 4 or any(sum(preset in item['supported'] for item in card['tracks']) < 4 for card in cards):
            return False
        signatures.add(tuple(tuple(item['key'] for item in card['tracks']) for card in cards))
    return len(signatures) > 1


def _menus(store, value, presets, now, epoch, include_genres, avoid_genres):
    if value['policy'] != home_vibe.VERSION or not value['tracks']:
        yield next(iter(presets)), []
        return
    positions = {index for lanes in value['pools'].values() for members in lanes.values() for index in members}
    if include_genres or avoid_genres:
        positions.update(index for lanes in value['genres'].values() for groups in lanes.values()
                         for family, members in groups.items() if family not in avoid_genres
                         and (not include_genres or family in include_genres) for index in members)
    items = {index: value['tracks'][index] for index in sorted(positions)}
    inputs = vibe_pools.eligibility_inputs(store, list(items.values()), now)
    if getattr(store, '_recommendation_files', False):
        # Typed serving resolves catalog corrections before filtering its pools.
        # Keep that same interpretation for the prepared compact index.
        _, owned, discoveries, metadata, *_ = inputs
        items = {index: {**item, 'album': (current := owned.get(item['key']) or discoveries.get(item['key']) or {}).get('album'),
                  'genre': metadata.get(current.get('video_id'), {}).get('genre') or current.get('genre') or item.get('genre')}
                 for index, item in items.items()}
    axes = inputs[-1]
    artist_cap = int(rec_params.get_param(store, 'modes_artist_cap'))
    album_cap = int(rec_params.get_param(store, 'modes_album_cap'))
    for preset in presets:
        def select(lane, seen):
            pool = [items[index] for index in value['pools'].get(preset, {}).get(lane, [])]
            if include_genres or avoid_genres:
                groups = value['genres'].get(preset, {}).get(lane, {})
                pool += [items[index] for family, members in groups.items()
                         if family not in avoid_genres and (not include_genres or family in include_genres)
                         for index in members]
                pool = [item for item in pool if axes.get(item.get('genre'), ('', None))[0] not in avoid_genres
                        and (not include_genres or axes.get(item.get('genre'), ('', None))[0] in include_genres)]
                pool = list({item['key']: item for item in pool}.values())
                pool.sort(key=lambda item: item['order'][preset])
            pool = vibe_pools.eligible(store, pool[:vibe_pools.POOL_LIMIT], now, lane.split(':')[0], seen,
                include_genres=include_genres, avoid_genres=avoid_genres, inputs=inputs)
            pool.sort(key=lambda item: item['order'][preset])
            return vibe_pools.diversify(pool, artist_cap, album_cap)

        meta = value.get('metas', {}).get(preset, value['meta'])
        cards = vibe_pools.assemble_lanes(meta, epoch,
            rec_params.get_param(store, 'comfort_min_pool'), select)
        yield preset, cards
