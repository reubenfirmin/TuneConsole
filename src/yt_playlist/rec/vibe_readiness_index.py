"""Build the compact readiness index without importing its serving readers."""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import home_vibe, musical_character, vibe_preparation
from yt_playlist.rec.vibe_pool_contract import POOL_LIMIT
from yt_playlist.rec.vibe_support import supported

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
        for family, item in selected[:POOL_LIMIT]:
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
    for item in value['tracks']:
        check_cancellable_work()
        item['order'] = {preset: home_vibe.order_key(item, preset) for preset in home_vibe.PRESETS}
        item['supported'] = [preset for preset in home_vibe.PRESETS if supported(item, preset)]
        item.pop('musical', None)
        item.pop('score', None)
    return value


def prepare(bundle, *, include_genres=True, genre_request=None):
    """Attach to the existing bundle publication, with no separate lifecycle."""
    return compact(project(bundle, include_genres=include_genres, genre_request=genre_request))
