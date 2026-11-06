"""Worker-prepared cross-genre opportunities, before ordinary mode truncation.

The existing ranked full-depth pools supply taste scores and eligible catalog
coverage. Controls only assemble these bounded prepared opportunities.
"""
from collections import Counter
from dataclasses import dataclass
import math
from types import MappingProxyType

from yt_playlist.core import json_cache
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.rec import home_vibe, musical_character, musical_profiles, radio_intensity, rec_params
from yt_playlist.rec.eligibility import home_exclusions
from yt_playlist.rec.vibe_pool_contract import GENRE_RESERVE, POOL_LIMIT
from yt_playlist.util.matching import normalize

_LABELS = {'wheelhouse': 'More in your wheelhouse', 'explore': 'From your catalog',
           'fresh': 'Fresh songs', 'comfort': 'Comfort listening'}


@dataclass(frozen=True)
class _Catalog:
    """Immutable scalar SQL rows shared by dependency-tracked readers."""
    groups: tuple

    def __post_init__(self):
        object.__setattr__(self, 'groups', tuple(
            MappingProxyType({key: MappingProxyType(dict(row)) for key, row in group.items()})
            for group in self.groups))

    def __deepcopy__(self, memo):
        # The query projections contain only SQLite scalars. Neither the maps
        # nor their rows can change, so detached readers can share their value.
        return self


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
    # Every preset/lane sees the same taxonomy on this preparation snapshot.
    # Resolve each exact provider label once without retaining it across builds.
    axes = store.genres.feature_axes({item.get('genre') for item in unique.values()})
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
                check_cancellable_work()
                genre = item.get('genre')
                family = axes.get(genre, ('', None))[0]
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
    return _stored_pools(store, preset, [lane], include_genres, avoid_genres)[lane]


def _stored_pools(store, preset, lanes, include_genres, avoid_genres, *, ranking_only=False):
    """Share catalog and musical evidence across the requested lanes."""
    if not getattr(store, '_read_only', False):
        with store.read_view() as reader:
            return _stored_pools(reader, preset, lanes, include_genres, avoid_genres, ranking_only=ranking_only)
    pools = {lane: {} for lane in lanes}
    surface = 'vibe:' + preset
    head = store.recommendations.head(surface)
    if head is None:
        return {lane: [] for lane in lanes}
    generation = head['id']
    framing = store.recommendations.framing(surface, generation=generation) or {}
    if framing.get('vibe_version') != home_vibe.VERSION:
        return {lane: [] for lane in lanes}
    pending = []
    for lane in lanes:
        modes = ['']
        if include_genres or avoid_genres:
            modes.extend(row['mode_key'] for row in store.recommendations.selections(surface, lane, generation=generation)
                         if row['mode_key'] and row['mode_key'] not in avoid_genres
                         and (not include_genres or row['mode_key'] in include_genres))
        pending.extend((mode, lane, -1) for mode in modes)
    profiles = {}
    while pending:
        check_cancellable_work()
        selected, pending = pending[:16], pending[16:]
        pages = store.recommendations.candidate_pages(surface, selected, generation=generation, limit=POOL_LIMIT)
        keys = list(dict.fromkeys(item['key'] for page in pages for item in page['tracks']
                                 if item['key'] not in profiles))
        scores = store.vibe_predictions.fit_scores(keys, preset) if ranking_only else {}
        scores = {key: score for key, score in scores.items()
                  if type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1}
        profiles.update(musical_character.load(store, [key for key in keys if key not in scores]))
        profiles.update({key: {'_rank_only': True, 'vibes': {'fits': {preset: {'score': score}}}}
                         for key, score in scores.items()})
        axes = (store.genres.feature_axes(item.get('genre') for page in pages for item in page['tracks'])
                if include_genres or avoid_genres else {})
        for (mode, lane, _), page in zip(selected, pages):
            pool = pools[lane]
            for item in page['tracks']:
                family = axes.get(item.get('genre'), ('', None))[0]
                if family in avoid_genres or include_genres and family not in include_genres:
                    continue
                pool[item['key']] = {**item, 'musical': profiles.get(item['key'])}
            # Filters can reject a full page. Continue through this selected
            # pool while retaining only the best bounded result in memory.
            if len(pool) > POOL_LIMIT:
                pools[lane] = {item['key']: item for item in sorted(pool.values(), key=lambda item: home_vibe.order_key(item, preset))[:POOL_LIMIT]}
            if not page['exhausted'] and (include_genres or avoid_genres):
                pending.append((mode, lane, page['after']))
        retained = {key for pool in pools.values() for key in pool}
        profiles = {key: value for key, value in profiles.items() if key in retained}
    result = {}
    for lane, pool in pools.items():
        rows = list(pool.values())
        if include_genres or avoid_genres:
            rows.sort(key=lambda item: home_vibe.order_key(item, preset))
        result[lane] = rows[:POOL_LIMIT]
    return result


def eligibility_inputs(store, items, now, *, include_discoveries=True, hydrated=False):
    """Resolve live exclusions once for a set of already prepared candidates."""
    keys = list(dict.fromkeys(item['key'] for item in items))
    blocked = home_exclusions(store, keys, now)
    if hydrated:
        # candidate_pages already resolved current membership and upload metadata
        # on this pinned reader. Reuse it rather than querying each owner again.
        owned = {item['key']: item for item in items if not item['_radio_fresh']}
        discoveries = {item['key']: item for item in items if item['_radio_fresh']}
        metadata = {item['video_id']: item for item in items}
    else:
        def catalog():
            # These projections consume catalog fields, never enrichment's
            # scheduling timestamps. Metadata and membership changes still
            # invalidate the result through their real owner revisions.
            with column_group_reads(store, 'recordings', 'catalog'):
                owned = {key: {**row, 'album': row.get('album') or ''}
                         for key, row in store.rec_query.group_candidates(keys).items()}
                # Fresh lanes always reject an owned recording, and other
                # lanes use its owned entry. Do not hydrate that recording's
                # unused discovery alternative a second time.
                discoveries = (store.discovery.candidate_tracks_by_keys([key for key in keys if key not in owned])
                               if include_discoveries else {})
                owned_videos = {row['video_id'] for row in owned.values()}
                metadata = store.rec_eligibility.offer_metadata(
                    [item['video_id'] for item in discoveries.values()
                     if item['video_id'] not in owned_videos])
                # The owned-entry query already resolved these same catalog
                # fields. Do not resolve its uploads again through catalog_tracks.
                metadata.update({row['video_id']: {'video_id': row['video_id'], 'genre': row['genre'],
                                                   'duration': row['duration_s']} for row in owned.values()})
            return _Catalog((owned, discoveries, metadata))
        # Catalog fields do not depend on the request clock. A tracked reader
        # can reuse them only while their actual SQL dependencies are unchanged;
        # feedback, expiring exclusions and weights are always resolved below.
        owned, discoveries, metadata = store.memoized_read(
            ('vibe-eligibility-catalog', tuple(keys), include_discoveries), catalog).groups
    weights = store.rec_model.get_weights(now=now)
    maximum = 60 * rec_params.get_param(store, 'max_track_minutes')
    muted = {normalize(artist) for artist in store.rec_model.muted_artists()}
    axes = store.genres.feature_axes([*(item.get('genre') for item in items),
        *(item.get('genre') for item in metadata.values()),
        *(item.get('genre') for item in [*owned.values(), *discoveries.values()])])
    return blocked, owned, discoveries, metadata, weights, maximum, muted, axes


def eligible(store, items, now, lane, excluded=(), *, include_genres=(), avoid_genres=(), inputs=None):
    return list(iter_eligible(store, items, now, lane, excluded,
                             include_genres=include_genres, avoid_genres=avoid_genres, inputs=inputs))


def iter_eligible(store, items, now, lane, excluded=(), *, include_genres=(), avoid_genres=(), inputs=None):
    """Yield live eligible candidates; ordered consumers can stop after filling their menu."""
    blocked, owned, discoveries, metadata, weights, maximum, muted, axes = (
        inputs if inputs is not None else eligibility_inputs(store, items, now, include_discoveries=lane == 'fresh'))
    blocked = blocked | set(excluded)
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
        yield {**item, **{key: current[key] for key in ('title', 'artist', 'video_id', 'thumbnail')
                         if key in current}}


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
    selected = None
    if bundles is None and getattr(store, '_recommendation_files', False):
        if not getattr(store, '_read_only', False):
            with store.read_view() as reader:
                return assemble(reader, now, preset, epoch, include_genres=include_genres, avoid_genres=avoid_genres)
        meta = store.recommendations.framing('vibe:' + preset) or {}
        meta = {**meta, 'comfort_pool': meta.get('comfort_pool') or 0,
                'year_cuts': ([meta['year_low'], meta['year_high']]
                                     if meta.get('year_low') is not None else None)}
        lanes = list(_LABELS)
        if meta.get('year_cuts'):
            lanes.append(f'temporal:{epoch % 3}')
        selected = _stored_pools(store, preset, lanes, include_genres, avoid_genres, ranking_only=True)
        inputs = eligibility_inputs(store, [item for pool in selected.values() for item in pool], now, hydrated=True)
        artist_cap = int(rec_params.get_param(store, 'modes_artist_cap'))
        album_cap = int(rec_params.get_param(store, 'modes_album_cap'))
    else:
        bundles = bundles if bundles is not None else store.rec_proposals.get_proposals('mode_bundles') or {}
        meta = bundles.get('_meta', {})
    def select(lane, seen):
        if selected is not None:
            pool = eligible(store, selected[lane], now, lane.split(':')[0], seen,
                            include_genres=include_genres, avoid_genres=avoid_genres, inputs=inputs)
            pool.sort(key=lambda item: home_vibe.order_key(item, preset))
            return diversify(pool, artist_cap, album_cap)
        return pick(store, now, preset, lane, seen, bundles=bundles,
                    include_genres=include_genres, avoid_genres=avoid_genres)
    cards = assemble_lanes(meta, epoch, rec_params.get_param(store, 'comfort_min_pool'), select)
    return _finish_cards(store, preset, cards)


def _finish_cards(store, preset, cards):
    profiles = musical_character.load(store, list(dict.fromkeys(item['key'] for card in cards for item in card['tracks']
        if (item.get('musical') or {}).get('_rank_only'))))
    for card in cards:
        lane = card.pop('pool_lane')
        card['tracks'] = [{**item, 'musical': profiles[item['key']]} if item['key'] in profiles else item
                          for item in card['tracks']]
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
