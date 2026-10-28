"""Assemble real Home offers from prepared graph regions, without fitting on requests."""
import json
import math
import random

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import group_learning, group_serving, home_themes, home_vibe, mode_surfaces, rec_params
from yt_playlist.rec.eligibility import home_exclusions
from yt_playlist.util import genre_map


def inputs(store, now, preparation=None):
    # A Ranker retains its reader; keep it and mutable ranking/filter caches
    # local to this attempt.
    # Full pools contain repeated musical profiles. Copying that cache on every
    # hit costs more than these bounded reads and can exhaust the Home deadline
    # before assembly starts. Completed selections/cards own retry reuse instead.
    result = _inputs(store, now, preparation)
    if result:
        result = {**result, 'vibe_rank': (preparation['vibe_rank'] if preparation is not None
                                        else home_vibe.ranker(store, now)),
                  'filtered': {}, 'ranked': {}}
    return result


def _inputs(store, now, preparation):
    preview = (preparation['group_preview'] if preparation is not None and 'group_preview' in preparation else
               store.rec_proposals.get_proposals(group_serving.SURFACE)
               if group_serving.checkpoint(store) else None)
    result = None
    if preview:
        bundles = (preparation['bundles'] if preparation is not None else
                   json.loads(store.rec_proposals.group_pools_json() or '{}'))
        pools = bundles.get('all', {})
        keys = {item['key'] for pool in pools.values() for item in pool if item.get('key')}
        # Allocation needs only the immutable hierarchy, not loaded memberships.
        allocation = group_learning.current_allocation(store, preview, now)
        blocked = home_exclusions(store, keys, now)
        muted = store.rec_model.muted_artists()
        owned = store.rec_query.group_candidates(keys)
        genres = {key: t['genre'] for key, t in owned.items()}
        weights = store.rec_model.get_weights(now=now)
        maximum = 60 * rec_params.get_param(store, 'max_track_minutes')
        ranked_genres = {t['key']: t.get('genre') or '' for pool in pools.values() for t in pool}
        genre_axes = {genre: (genre_map.family(genre), genre_map.subgenre(genre))
                      for genre in set(genres.values()) | set(ranked_genres.values())}
        current = {}
        for key, track in owned.items():
            genre = genres.get(key) or ranked_genres.get(key) or ''
            family, subgenre = genre_axes[genre]
            duration = track['duration_s']
            if (key not in blocked and track['artist'] not in muted
                    and weights.get(f'genre:{family}', 1) != 0
                    and weights.get(f'genre:{subgenre}', 1) != 0
                    and not (duration and duration > maximum)):
                current[key] = {**track, 'genre': genre}
        pools = {lane: [{**item, **current[item['key']]} for item in pool
                       if item['key'] in current and (not item.get('genre') or not genres.get(item['key'])
                           or genre_axes[item['genre']][0] == genre_axes[genres[item['key']]][0])]
                 for lane, pool in pools.items()}
        result = {'preview': preview, 'pools': pools, 'allocation': allocation,
                  'context': {**preview['context'], 'at': now, 'group_allocation': allocation,
                              'group_choice_policy': group_learning.POLICY,
                              'group_choice_parameters': group_learning.choice_bandit.parameters(store)},
                  'by_version': {g['version']: g for g in preview['groups']},
                  'blocked': blocked, 'muted': muted,
                  'meta': bundles.get('_meta', {})}
    return result


def assemble(store, now, epoch, data, *, include=(), avoid=()):
    """Spend each leaf's allocation once, combining mass at a usable parent as needed."""
    cap_artist = int(rec_params.get_param(store, 'modes_artist_cap'))
    cap_album = int(rec_params.get_param(store, 'modes_album_cap'))
    filter_key = (tuple(sorted(include)), tuple(sorted(avoid)))
    if filter_key not in data['filtered']:
        pools = {lane: home_themes.matching_genres(store, pool, include=include, avoid=avoid)
                 for lane, pool in data['pools'].items()}
        data['filtered'][filter_key] = {lane: {t['key']: t for t in pool
            if t.get('video_id') and t['key'] not in data['blocked'] and t.get('artist') not in data['muted']}
            for lane, pool in pools.items()}
    pools = data['filtered'][filter_key]
    by_version = data['by_version']
    meta = data['meta']
    comfort = meta.get('comfort_pool', 0) >= rec_params.get_param(store, 'comfort_min_pool')
    fourth = 'comfort' if comfort else 'temporal' if meta.get('year_cuts') else None
    lanes = ['wheelhouse', 'explore'] + ([fourth] if fourth else [])
    if comfort and meta.get('year_cuts'):
        lanes.append('temporal')
    vibe = data['vibe_rank']
    rng = random.Random(f"{data['preview']['generation']}:{epoch}:{filter_key}:{vibe.preset}")
    cards, seen, used = [], set(), set()
    for lane in lanes:
        if lane == 'temporal' and any(card['lane'] == 'comfort' for card in cards):
            continue
        check_stopping()
        pool = pools.get(lane, {})
        candidates = {}
        def tracks(group):
            cache_key = (filter_key, vibe.preset, lane, epoch % 3 if lane == 'temporal' else None, group['version'])
            if cache_key not in data['ranked']:
                frame = group['frames'].get(lane, {})
                items = [pool[key] for key in frame.get('keys', []) if key in pool]
                if lane == 'temporal':
                    lo, hi = meta['year_cuts']
                    items = [t for t in items if mode_surfaces._in_band(t.get('year'), epoch % 3, lo, hi)]
                data['ranked'][cache_key] = vibe(items)
            items = [t for t in data['ranked'][cache_key] if t['key'] not in seen]
            # Rotation starts within the first two mixes. Nothing beyond the
            # third mix can affect feasibility, affinity or the selected tracks.
            return mode_surfaces._diversify(items, cap_artist, cap_album,
                                            limit=mode_surfaces.PROTO_SIZE * 3)
        for leaf in data['preview']['groups']:
            if not leaf.get('leaf'):
                continue
            group = leaf
            while group:
                version = group['version']
                if version not in candidates:
                    candidates[version] = tracks(group)
                if group.get('display') and len(candidates[version]) >= mode_surfaces.PROTO_SIZE:
                    break
                group = by_version.get(group['parent_version'])
            if group and group['version'] not in used:
                candidates.setdefault('_weights', {}).setdefault(group['version'], 0.)
                candidates['_weights'][group['version']] += data['allocation'].get(leaf['group_id'], 0.)
        weights = candidates.pop('_weights', {})
        if not weights:
            continue
        # The graph supplies area mass; the current sound preference can select
        # a different group as well as reordering tracks inside it.
        def priority(version):
            fit = (sum(vibe.affinity(t) for t in candidates[version][:mode_surfaces.PROTO_SIZE])
                   / mode_surfaces.PROTO_SIZE)
            weight = weights[version] * (math.exp(6 * (fit - .5)) if vibe.preset else 1)
            return -math.log(max(rng.random(), 1e-12)) / max(weight, 1e-12)
        version = min(weights, key=priority)
        group = by_version[version]
        items = candidates[version]
        # Rotate through a bounded ranked head rather than replaying the same
        # twelve tracks each time this region returns.
        start = (epoch * 3) % max(1, min(len(items), mode_surfaces.PROTO_SIZE * 2))
        items = (items[start:] + items[:start])[:mode_surfaces.PROTO_SIZE]
        cards.append({'lane': lane, 'label': mode_surfaces._CARD_LABELS.get(lane,
                         mode_surfaces._BAND_LABELS[epoch % 3]),
                      'mode_id': None, 'ranker': 'musical-group', 'tracks': items,
                      'group_id': group['group_id'], 'group_version': version,
                      'group_generation': data['preview']['generation'], 'group_name': group['name']})
        seen.update(t['key'] for t in items)
        used.add(version)
    return cards
