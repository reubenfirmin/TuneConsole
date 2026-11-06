"""Read worker-prepared Vibe inputs; missing preparation never runs in requests."""
from functools import lru_cache
import math
from types import MappingProxyType

from yt_playlist.core import json_cache
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import home_vibe, musical_character, rec_params, vibe_pools, vibe_preparation
from yt_playlist.rec.vibe_readiness_index import MAX_INDEX_BYTES, VERSION, compact, prepare, project
from yt_playlist.rec.vibe_support import supported


def _freeze(item):
    if isinstance(item, dict):
        check_cancellable_work()
        return MappingProxyType({key: _freeze(child) for key, child in item.items()})
    if isinstance(item, list):
        return tuple(_freeze(child) for child in item)
    return item


@lru_cache(maxsize=1)
def _prepared_index(encoded):
    """Share one immutable decoded document, never its live readiness decision."""
    if len(encoded.encode()) > MAX_INDEX_BYTES:
        return None
    value = json_cache.decode(encoded)
    if not isinstance(value, dict):
        return None
    return _freeze(value)


def _encoded_index(store):
    if getattr(store, '_recommendation_files', False):
        metadata = store.recommendations.output('home', 'ranking-evidence') or {}
        home = store.recommendations.head('home')
        if home and any((store.recommendations.head('vibe:' + preset) or {}).get('id') != home['id']
                        for preset in home_vibe.PRESETS):
            metadata = {}
    else:
        metadata = (store.rec_proposals.proposal_fields('mode_bundles', ('_meta',)) or {}).get('_meta', {})
    return metadata.get('vibe_readiness')


def _source(store):
    return store.memoized_read(('vibe-readiness-source', VERSION, home_vibe.VERSION),
                               lambda: _encoded_index(store))


def load(store, include, avoid):
    def read():
        encoded = _source(store)
        value = _prepared_index(encoded) if isinstance(encoded, str) else None
        if (value is not None and value.get('version') == VERSION
                and value.get('policy') == home_vibe.VERSION
                and value.get('profile_policy') == musical_character.VERSION
                and value.get('inputs_policy') == vibe_preparation.serving_prefix()
                and (not (include or avoid) or (value.get('genre_reserves')
                    and value.get('genre_request') in (None, (tuple(sorted(include)), tuple(sorted(avoid))))))):
            if not getattr(store, '_recommendation_files', False):
                return value
            tracks = store.memoized_read(('vibe-index-inputs', encoded),
                                        lambda: _current_tracks(store, value))
            return value if tracks is None else MappingProxyType({**value, 'tracks': _freeze(tracks)})
        return None
    return read()


def _current_tracks(store, value):
    """Keep retained candidates while checking their current evidence and order.

    A background profile update does not retire an entire menu publication.
    Relational fits supply the usual fast ranking path; only missing scores
    need the same prepared profile fallback as ordinary Vibe serving.
    """
    state, profiles = store.musical_profiles.readiness_state(item['key'] for item in value['tracks'])
    if state == value.get('profile_state'):
        return None
    def ranked(profile, preset):
        score = profile.get('vibes', {}).get('fits', {}).get(preset, {}).get('score')
        return type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1
    def support(profile, preset):
        vibes = profile['vibes']
        # Each fit can be refreshed separately. Another preset's compatible
        # version cannot qualify this preset's obsolete evidence.
        return supported({'musical': {**profile, 'vibes': {**vibes,
            'inputs_version': vibes['fits'].get(preset, {}).get('inputs_version')}}}, preset)
    supports = {key: [preset for preset in home_vibe.PRESETS if support(profile, preset)]
                for key, profile in profiles.items()}
    fallback = [key for key, profile in profiles.items() if profile['version'] is not None
                and any(not ranked(profile, preset) for preset in home_vibe.PRESETS)]
    profiles.update(musical_character.load(store, fallback))
    tracks = []
    for item in value['tracks']:
        check_cancellable_work()
        current = {'musical': profiles.get(item['key'])}
        order = {}
        for preset in home_vibe.PRESETS:
            rank = home_vibe.order_key(current, preset)
            # Taste belongs to this retained publication; live sound evidence
            # changes the fit, never the publication's taste-score tie break.
            order[preset] = (rank[0], item['order'][preset][1], rank[2])
        tracks.append({**item, 'order': order,
                       'supported': supports.get(item['key'], [])})
    return tracks


def ready(store, now, epoch, *, include_genres=(), avoid_genres=()):
    if not getattr(store, '_read_only', False):
        with store.read_view() as reader:
            return ready(reader, now, epoch, include_genres=include_genres, avoid_genres=avoid_genres)
    return store.memoized_read(('vibe-readiness', VERSION, home_vibe.VERSION,
        musical_character.VERSION, vibe_preparation.serving_prefix(), now, epoch,
        tuple(include_genres), tuple(avoid_genres)),
        lambda: _ready(store, now, epoch, include_genres, avoid_genres))


def _ready(store, now, epoch, include_genres, avoid_genres):
    signatures = set()
    for preset, cards in menus(store, now, epoch, include_genres, avoid_genres):
        if len(cards) < 4 or any(sum(preset in item['supported'] for item in card['tracks']) < 4 for card in cards):
            return False
        signatures.add(tuple(tuple(item['key'] for item in card['tracks']) for card in cards))
    return len(signatures) > 1


def menus(store, now, epoch, include_genres=(), avoid_genres=()):
    """Reuse live selections only within this pinned, read-only input set."""
    def select():
        # Resolve live scores/support inside this memo's dependency capture.
        # A document resolved by the caller would hide those reads from the
        # cached selections and allow old rankings or support to survive edits.
        value = load(store, include_genres, avoid_genres)
        if value is None:
            return []
        # The index owns immutable ordering maps. Completed selections no
        # longer need those maps, and the reader's detached memo owns its rows.
        return [(preset, [{**card, 'tracks': [{key: child for key, child in item.items() if key != 'order'}
                                             for item in card['tracks']]} for card in cards])
                for preset, cards in _menus(store, value, home_vibe.PRESETS, now, epoch, include_genres, avoid_genres)]
    return store.memoized_read(('vibe-selected-cards', VERSION, home_vibe.VERSION, _source(store),
        musical_character.VERSION, vibe_preparation.serving_prefix(), now, epoch,
        tuple(include_genres), tuple(avoid_genres)),
        select)


def _menus(store, value, presets, now, epoch, include_genres, avoid_genres):
    if value['policy'] != home_vibe.VERSION or not value['tracks']:
        return [(next(iter(presets)), [])]
    # Only this rotation's temporal band can be selected, including comfort's
    # live fallback. Keep every candidate in its reserve, but do not resolve
    # catalog/exclusion inputs for the two bands this rotation cannot consume.
    temporal = f'temporal:{epoch % 3}'
    pools = {preset: {lane: members for lane, members in value['pools'].get(preset, {}).items()
                      if not lane.startswith('temporal:') or lane == temporal}
             for preset in presets}
    if not (include_genres or avoid_genres):
        # Resolve the leading candidates together. If a lane cannot fill from
        # them, continue through every remaining prepared candidate below.
        # Truncate before sorting, exactly as the full selection does.
        pools = {preset: {lane: sorted(members[:vibe_pools.POOL_LIMIT],
                            key=lambda index: value['tracks'][index]['order'][preset])
                          for lane, members in lanes.items()}
                 for preset, lanes in pools.items()}
        comfort_minimum = rec_params.get_param(store, 'comfort_min_pool')
        heads = {}
        for preset, lanes in pools.items():
            meta = value.get('metas', {}).get(preset, value['meta'])
            fourth = ('comfort' if meta.get('comfort_pool', 0) >= comfort_minimum
                      else temporal if meta.get('year_cuts') else None)
            # Resolve the lanes the ordinary assembly will actually visit.
            # A thin comfort lane still expands through its complete reserve
            # and temporal fallback below before accepting a selection.
            heads[preset] = {lane: members[:48] for lane, members in lanes.items()
                             if lane in ('wheelhouse', 'explore', 'fresh', fourth)}
        selected, complete, items, inputs = _menus_from_pools(
            store, value, presets, heads, now, epoch, (), (), full_pools=pools)
        if complete:
            return selected
        return _menus_from_pools(store, value, presets, pools, now, epoch, (), (),
                                 resolved=items, prior_inputs=inputs)[0]
    return _menus_from_pools(store, value, presets, pools, now, epoch,
                             include_genres, avoid_genres)[0]


def _menus_from_pools(store, value, presets, pools, now, epoch, include_genres, avoid_genres,
                      *, full_pools=None, resolved=None, prior_inputs=None):
    temporal = f'temporal:{epoch % 3}'
    positions = {index for preset in presets for members in pools.get(preset, {}).values() for index in members}
    if include_genres or avoid_genres:
        positions.update(index for preset in presets for lane, groups in value['genres'].get(preset, {}).items()
                         if not lane.startswith('temporal:') or lane == temporal
                         for family, members in groups.items() if family not in avoid_genres
                         and (not include_genres or family in include_genres) for index in members)
    items = dict(resolved or {})
    pending = {index: value['tracks'][index] for index in sorted(positions - items.keys())}
    inputs = vibe_pools.eligibility_inputs(store, list(pending.values()), now)
    if prior_inputs is not None:
        blocked, owned, discoveries, metadata, weights, maximum, muted, axes = prior_inputs
        new_blocked, new_owned, new_discoveries, new_metadata, _, _, _, new_axes = inputs
        inputs = (blocked | new_blocked, {**owned, **new_owned},
                  {**discoveries, **new_discoveries}, {**metadata, **new_metadata},
                  weights, maximum, muted, {**axes, **new_axes})
    if getattr(store, '_recommendation_files', False):
        # Typed serving resolves catalog corrections before filtering its pools.
        # Keep that same interpretation for the prepared compact index.
        _, owned, discoveries, metadata, *_ = inputs
        pending = {index: {**item, 'album': (current := owned.get(item['key']) or discoveries.get(item['key']) or {}).get('album'),
                  'genre': metadata.get(current.get('video_id'), {}).get('genre') or current.get('genre') or item.get('genre')}
                   for index, item in pending.items()}
    items.update(pending)
    axes = inputs[-1]
    artist_cap = int(rec_params.get_param(store, 'modes_artist_cap'))
    album_cap = int(rec_params.get_param(store, 'modes_album_cap'))
    selected, complete = [], True
    for preset in presets:
        def select(lane, seen):
            nonlocal complete
            members = pools.get(preset, {}).get(lane, [])
            pool = [items[index] for index in members]
            if include_genres or avoid_genres:
                groups = value['genres'].get(preset, {}).get(lane, {})
                pool += [items[index] for family, members in groups.items()
                         if family not in avoid_genres and (not include_genres or family in include_genres)
                         for index in members]
                pool = [item for item in pool if axes.get(item.get('genre'), ('', None))[0] not in avoid_genres
                        and (not include_genres or axes.get(item.get('genre'), ('', None))[0] in include_genres)]
                pool = list({item['key']: item for item in pool}.values())
                pool.sort(key=lambda item: item['order'][preset])
            pool = pool[:vibe_pools.POOL_LIMIT]
            pool.sort(key=lambda item: item['order'][preset])
            pool = vibe_pools.iter_eligible(store, pool, now, lane.split(':')[0], seen,
                include_genres=include_genres, avoid_genres=avoid_genres, inputs=inputs)
            tracks = vibe_pools.diversify(pool, artist_cap, album_cap)
            if (full_pools is not None and len(tracks) < 12
                    and len(members) < len(full_pools.get(preset, {}).get(lane, []))):
                # Even a comfort lane that provisionally falls back must be
                # exhausted before that fallback can be accepted.
                complete = False
            return tracks

        meta = value.get('metas', {}).get(preset, value['meta'])
        cards = vibe_pools.assemble_lanes(meta, epoch,
            rec_params.get_param(store, 'comfort_min_pool'), select)
        selected.append((preset, cards))
    return selected, complete, items, inputs
