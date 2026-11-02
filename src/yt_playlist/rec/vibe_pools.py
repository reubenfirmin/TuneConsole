"""Worker-prepared cross-genre opportunities, before ordinary mode truncation.

The existing ranked full-depth pools supply taste scores and eligible catalog
coverage. Controls only assemble these bounded prepared opportunities.
"""
from collections import Counter

from yt_playlist.core import json_cache
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import home_vibe, musical_character, musical_profiles, radio_intensity, rec_params
from yt_playlist.rec.eligibility import home_exclusions
from yt_playlist.rec.vibe_pool_contract import GENRE_RESERVE, POOL_LIMIT
from yt_playlist.util import genre_map
from yt_playlist.util.matching import normalize

_LABELS = {'wheelhouse': 'More in your wheelhouse', 'explore': 'From your catalog',
           'fresh': 'Fresh songs', 'comfort': 'Comfort listening'}


def _reserve(items, preset, limit=POOL_LIMIT):
    """Cap after fit, reserving room beyond one prolific artist's discography."""
    artists, result = Counter(), []
    def priority(item):
        check_cancellable_work()
        return home_vibe.order_key(item, preset)
    for item in sorted(items, key=priority):
        check_cancellable_work()
        artist = normalize(item.get('artist') or '')
        if artist and artists[artist] >= 8:
            continue
        artists[artist] += 1
        result.append(item)
        if len(result) == limit:
            break
    return result


def prepare(store, bundles, now, policies):
    """Use the complete prepared inputs, never the forty-track mode heads."""
    pools = bundles.get('all', {})
    unique = {item['key']: item for mid, bucket in bundles.items() if not mid.startswith('_')
              for items in bucket.values() for item in items if item.get('key')}
    profiles = musical_character.load(store, list(unique))
    for mid, bucket in bundles.items():
        if mid.startswith('_'):
            continue
        for lane, items in bucket.items():
            bucket[lane] = [{**item, 'musical': profiles.get(item['key'])} for item in items]
    pools = bundles.get('all', {})
    library = {item['key']: item for lane in ('explore', 'wheelhouse') for item in pools.get(lane, [])}
    menus, genre_menus = {}, {}
    cuts = bundles.get('_meta', {}).get('year_cuts')
    band_pools = {}
    if cuts:
        for band in range(3):
            band_pools[f'temporal:{band}'] = [item for item in pools.get('temporal', [])
                if item.get('year') and (0 if int(item['year']) <= cuts[0]
                                        else 1 if int(item['year']) <= cuts[1] else 2) == band]
    for preset in home_vibe.PRESETS:
        menus[preset] = {}
        genre_menus[preset] = {}
        for lane in (*_LABELS, *band_pools):
            candidates = {item['key']: item for item in band_pools.get(lane, pools.get(lane, []))}
            if lane in ('wheelhouse', 'explore'):
                # A strong musical match remains reachable when ordinary taste
                # neighborhoods did not admit it to this particular lane.
                candidates = {**{key: {**item, 'score': None} for key, item in library.items()}, **candidates}
            menus[preset][lane] = policies.stamp(_reserve(list(candidates.values()), preset),
                                                 f'{home_vibe.VERSION}:{preset}:{lane}')
            families = {}
            for item in candidates.values():
                family = genre_map.family(item.get('genre'),conn=store.conn)
                if family:
                    families.setdefault(family, []).append(item)
            genre_menus[preset][lane] = {family: policies.stamp(_reserve(items, preset, GENRE_RESERVE),
                f'{home_vibe.VERSION}:{preset}:{lane}:{family}') for family, items in families.items()}
    bundles['_vibes'] = {'version': home_vibe.VERSION, 'profile_version': musical_character.VERSION,
                         'pools': menus, 'genres': genre_menus}
    if bundles.get('_radio'):
        radio_intensity.prepare(store, bundles['_radio'], profiles, unique)
    bundles.setdefault('_meta', {})['musical_generation'] = musical_profiles.digest(
        sorted((key, (profile or {}).get('digest')) for key, profile in profiles.items()))
    from yt_playlist.rec import mix_tuning, vibe_readiness_index
    request = store.rec_proposals.get_proposals('home_genre_request') or {}
    filters = ((request.get('include_genres', ()), request.get('avoid_genres', ()))
               if mix_tuning.genre_active(store, request) else ((), ()))
    # Publish ordinary pools plus bounded reserves for the current controls.
    # Requests never reconstruct missing readiness from full musical profiles.
    readiness = vibe_readiness_index.prepare(bundles, include_genres=any(filters), genre_request=filters)
    readiness['profile_state'] = store.musical_profiles.readiness_fingerprint(
        item['key'] for item in readiness['tracks'])
    encoded = json_cache.encode(readiness)
    bundles['_meta'].pop('vibe_readiness', None)
    if len(encoded.encode()) <= vibe_readiness_index.MAX_INDEX_BYTES:
        bundles['_meta']['vibe_readiness'] = encoded
    return bundles


def candidates(store, preset, lane, *, bundles=None, include_genres=(), avoid_genres=()):
    if bundles is None and getattr(store, '_recommendation_files', False):
        return _stored_candidates(store, preset, lane, include_genres, avoid_genres)
    if bundles is None:
        filters = {'include_genres': include_genres, 'avoid_genres': avoid_genres}

        def read():
            encoded = store.rec_proposals._proposal_json('mode_bundles')

            def select(value):
                prepared = (value or {}).get('_vibes') or {}
                if prepared.get('version') != home_vibe.VERSION:
                    return {}
                genres = prepared.get('genres', {}).get(preset, {}).get(lane, {})
                genres = {family: items for family, items in genres.items()
                          if (include_genres or avoid_genres) and family not in avoid_genres
                          and (not include_genres or family in include_genres)}
                return {'_vibes': {'version': prepared.get('version'),
                    'pools': {preset: {lane: prepared.get('pools', {}).get(preset, {}).get(lane, [])}},
                    'genres': {preset: {lane: genres}}}}

            # Restore musical evidence only for this preset/lane, rather than
            # every Home and Radio copy. Reuse the bounded result across retries;
            # eligibility is still checked against the current snapshot below.
            selected = (json_cache.decode(encoded, keys={'_vibes'}, project=select)
                        if encoded is not None else {})
            return candidates(store, preset, lane, bundles=selected, **filters)

        return store.memoized_read(('vibe-pool-candidates', home_vibe.VERSION, preset, lane,
                                    tuple(sorted(include_genres)), tuple(sorted(avoid_genres))), read)
    prepared = bundles.get('_vibes') or {}
    if prepared.get('version') != home_vibe.VERSION:
        return []
    pool = list(prepared.get('pools', {}).get(preset, {}).get(lane, []))
    if include_genres or avoid_genres:
        groups = prepared.get('genres', {}).get(preset, {}).get(lane, {})
        pool += [item for family, items in groups.items() if family not in avoid_genres
                 and (not include_genres or family in include_genres) for item in items]
        axes = store.genres.feature_axes(item.get('genre') for item in pool)
        pool = [item for item in pool if axes.get(item.get('genre'), ('', None))[0] not in avoid_genres
                and (not include_genres or axes.get(item.get('genre'), ('', None))[0] in include_genres)]
        pool = list({item['key']: item for item in pool}.values())
        pool.sort(key=lambda item: home_vibe.order_key(item, preset))
    return pool[:POOL_LIMIT]


def _stored_candidates(store, preset, lane, include_genres, avoid_genres):
    """Read selected bounded vibe pools without opening the old mode bundle."""
    if not getattr(store, '_read_only', False):
        with store.read_view() as reader:
            return _stored_candidates(reader, preset, lane, include_genres, avoid_genres)
    surface = 'vibe:' + preset
    head = store.recommendations.head(surface)
    if head is None:
        return []
    generation = head['id']
    framing = store.recommendations.framing(surface, generation=generation) or {}
    if framing.get('vibe_version') != home_vibe.VERSION:
        return []
    modes = ['']
    if include_genres or avoid_genres:
        modes.extend(row['mode_key'] for row in store.recommendations.selections(surface, lane, generation=generation)
                     if row['mode_key'] and row['mode_key'] not in avoid_genres
                     and (not include_genres or row['mode_key'] in include_genres))
    pool = {}
    for mode in modes:
        after = -1
        while True:
            check_cancellable_work()
            page = store.recommendations.candidate_page(surface, mode, lane, generation=generation,
                                                         after=after, limit=POOL_LIMIT)
            profiles = musical_character.load(store, [item['key'] for item in page['tracks']])
            axes = (store.genres.feature_axes(item.get('genre') for item in page['tracks'])
                    if include_genres or avoid_genres else {})
            for item in page['tracks']:
                family = axes.get(item.get('genre'), ('', None))[0]
                if family in avoid_genres or include_genres and family not in include_genres:
                    continue
                pool[item['key']] = {**item, 'musical': profiles.get(item['key'])}
            # Filters can reject a full page. Continue through this selected
            # pool while retaining only the best bounded result in memory.
            if len(pool) > POOL_LIMIT:
                pool = {item['key']: item for item in sorted(pool.values(), key=lambda item: home_vibe.order_key(item, preset))[:POOL_LIMIT]}
            if page['exhausted'] or not (include_genres or avoid_genres):
                break
            after = page['after']
    result = list(pool.values())
    if include_genres or avoid_genres:
        result.sort(key=lambda item: home_vibe.order_key(item, preset))
    return result[:POOL_LIMIT]


def eligibility_inputs(store, items, now, *, include_discoveries=True):
    """Resolve live exclusions once for a set of already prepared candidates."""
    keys = [item['key'] for item in items]
    blocked = home_exclusions(store, keys, now)
    owned = store.rec_query.tracks_by_keys(keys)
    discoveries = (store.discovery.discovered_tracks_by_keys(keys, available_only=True)
                   if include_discoveries else {})
    metadata = store.rec_eligibility.offer_metadata([item['video_id'] for item in [*owned.values(), *discoveries.values()]])
    weights = store.rec_model.get_weights(now=now)
    maximum = 60 * rec_params.get_param(store, 'max_track_minutes')
    muted = {normalize(artist) for artist in store.rec_model.muted_artists()}
    axes = store.genres.feature_axes([*(item.get('genre') for item in items),
        *(item.get('genre') for item in metadata.values()),
        *(item.get('genre') for item in [*owned.values(), *discoveries.values()])])
    return blocked, owned, discoveries, metadata, weights, maximum, muted, axes


def eligible(store, items, now, lane, excluded=(), *, include_genres=(), avoid_genres=(), inputs=None):
    blocked, owned, discoveries, metadata, weights, maximum, muted, axes = (
        inputs if inputs is not None else eligibility_inputs(store, items, now, include_discoveries=lane == 'fresh'))
    blocked = blocked | set(excluded)
    result = []
    for item in items:
        current = (discoveries if lane == 'fresh' else owned).get(item['key'])
        if (not current or item['key'] in blocked or (lane == 'fresh' and item['key'] in owned)
                or normalize(current.get('artist') or '') in muted):
            continue
        details = metadata.get(current['video_id'], {})
        genre = details.get('genre') or current.get('genre') or item.get('genre') or ''
        family, subgenre = axes.get(genre, ('', None))
        if (weights.get(f'genre:{family}', 1) == 0
                or weights.get(f'genre:{subgenre}', 1) == 0
                or family in avoid_genres
                or (include_genres and family not in include_genres)
                or (details.get('duration') and details['duration'] > maximum)
                or (item.get('genre') and family != axes[item['genre']][0])):
            continue
        result.append({**item, **{key: current[key] for key in ('title', 'artist', 'video_id', 'thumbnail')
                                  if key in current}})
    return result


def diversify(items, artist_cap, album_cap, *, size=12):
    artists, albums, result = Counter(), Counter(), []
    for item in items:
        artist, album = normalize(item.get('artist') or ''), normalize(item.get('album') or '')
        if (artist and artists[artist] >= artist_cap) or (album and albums[(artist, album)] >= album_cap):
            continue
        artists[artist] += 1
        albums[(artist, album)] += 1
        result.append(item)
        if len(result) == size:
            break
    return result


def pick(store, now, preset, lane, excluded=(), *, bundles=None, size=12,
         include_genres=(), avoid_genres=()):
    filters = {'include_genres': include_genres, 'avoid_genres': avoid_genres}
    pool = eligible(store, candidates(store, preset, lane, bundles=bundles, **filters),
                    now, lane.split(':')[0], excluded, **filters)
    # Explicit genre restrictions apply before fit; automatic style heads do not.
    pool.sort(key=lambda item: home_vibe.order_key(item, preset))
    return diversify(pool, int(rec_params.get_param(store, 'modes_artist_cap')),
                     int(rec_params.get_param(store, 'modes_album_cap')), size=size)


def assemble(store, now, preset, epoch=0, *, bundles=None, include_genres=(), avoid_genres=()):
    if bundles is None and getattr(store, '_recommendation_files', False):
        if not getattr(store, '_read_only', False):
            with store.read_view() as reader:
                return assemble(reader, now, preset, epoch, include_genres=include_genres, avoid_genres=avoid_genres)
        meta = store.recommendations.framing('vibe:' + preset) or {}
        meta = {**meta, 'comfort_pool': meta.get('comfort_pool') or 0,
                'year_cuts': ([meta['year_low'], meta['year_high']]
                                     if meta.get('year_low') is not None else None)}
    else:
        bundles = bundles if bundles is not None else store.rec_proposals.get_proposals('mode_bundles') or {}
        meta = bundles.get('_meta', {})
    def select(lane, seen):
        return pick(store, now, preset, lane, seen, bundles=bundles,
                    include_genres=include_genres, avoid_genres=avoid_genres)
    cards = assemble_lanes(meta, epoch, rec_params.get_param(store, 'comfort_min_pool'), select)
    for card in cards:
        lane = card.pop('pool_lane')
        card['tracks'] = [{**item, 'serving_context': {**item.get('serving_context', {}),
            'ordering_ranker': home_vibe.VERSION, 'source_ranker': lane, 'vibe': preset,
            'vibe_affinity': home_vibe.affinity(item, preset), 'taste_score': home_vibe.taste_score(item),
            **musical_character.provenance(item)}} for item in card['tracks']]
    return cards


def assemble_lanes(meta, epoch, comfort_minimum, select):
    """Shared lane/fallback selection for serving and the compact readiness view."""
    lanes = dict(_LABELS)
    if meta.get('comfort_pool', 0) < comfort_minimum:
        del lanes['comfort']
        if meta.get('year_cuts'):
            lanes[f'temporal:{epoch % 3}'] = ('Throwback', 'Time Flies', 'Recent Picks')[epoch % 3]
    cards, seen = [], set()
    for lane, label in lanes.items():
        tracks = select(lane, seen)
        if lane == 'comfort' and len(tracks) < 4 and meta.get('year_cuts'):
            lane = f'temporal:{epoch % 3}'
            label = ('Throwback', 'Time Flies', 'Recent Picks')[epoch % 3]
            tracks = select(lane, seen)
        if len(tracks) < 4:
            continue
        cards.append({'lane': lane.split(':')[0], 'label': label, 'mode_id': None,
                      'ranker': home_vibe.VERSION, 'pool_lane': lane, 'tracks': tracks})
        seen.update(item['key'] for item in tracks)
    return cards
