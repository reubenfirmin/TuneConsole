"""Read worker-prepared Vibe inputs; missing preparation never runs in requests."""
from yt_playlist.core import json_cache
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import home_vibe, musical_character, rec_params, vibe_pools, vibe_preparation

VERSION = 2
# Leave room for the publication's other ranking evidence in its 1 MiB output.
MAX_INDEX_BYTES = 768 * 1024


def project(bundle, *, include_genres=True, genre_request=None):
    """Project worker inputs into bounded, shared readiness candidates."""
    prepared = (bundle or {}).get('_vibes') or {}
    tracks, positions = [], {}

    def members(items):
        result = []
        for item in items:
            profile = item.get('musical') or {}
            identity = (item['key'], item.get('artist'), item.get('album'), item.get('genre'),
                        item.get('score'), profile.get('digest', profile.get('profile')))
            if identity not in positions:
                positions[identity] = len(tracks)
                tracks.append({field: item[field] for field in
                    ('key', 'artist', 'album', 'genre', 'score', 'musical') if field in item})
            result.append(positions[identity])
        return result

    pools = {preset: {lane: members(items) for lane, items in lanes.items()}
             for preset, lanes in prepared.get('pools', {}).items()}
    def genre_members(groups, preset):
        if genre_request is None:
            return {family: members(items) for family, items in groups.items()}
        include, avoid = genre_request
        selected = [(family, item) for family, items in groups.items()
                    if family not in avoid and (not include or family in include) for item in items]
        selected.sort(key=lambda pair: home_vibe.order_key(pair[1], preset))
        bounded = {}
        for family, item in selected[:vibe_pools.POOL_LIMIT]:
            bounded.setdefault(family, []).append(item)
        return {family: members(items) for family, items in bounded.items()}

    genres = {preset: {lane: genre_members(groups, preset)
                      for lane, groups in lanes.items()}
              for preset, lanes in prepared.get('genres', {}).items()} if include_genres else {}
    meta = (bundle or {}).get('_meta') or {}
    return {'version': VERSION, 'policy': prepared.get('version'),
            'profile_policy': musical_character.VERSION, 'inputs_policy': vibe_preparation.serving_prefix(),
            'genre_reserves': include_genres,
            'genre_request': [sorted(part) for part in genre_request] if genre_request is not None else None,
            'tracks': tracks,
            'pools': pools, 'genres': genres,
            'meta': {key: meta[key] for key in ('comfort_pool', 'year_cuts') if key in meta}}


def compact(value):
    from yt_playlist.rec.growth_readiness import _supported
    for item in value['tracks']:
        check_cancellable_work()
        item['order'] = {preset: home_vibe.order_key(item, preset) for preset in home_vibe.PRESETS}
        item['supported'] = [preset for preset in home_vibe.PRESETS if _supported(item, preset)]
        item.pop('musical', None)
        item.pop('score', None)
    return value


def prepare(bundle, *, include_genres=True, genre_request=None):
    """Attach to the existing bundle publication, with no separate lifecycle."""
    return compact(project(bundle, include_genres=include_genres, genre_request=genre_request))


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
