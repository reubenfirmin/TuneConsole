"""Assemble real Home offers from prepared graph regions, without fitting on requests."""
from itertools import islice
import math
import random

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import artist_variety, group_learning, group_serving, group_viz, home_vibe, mode_surfaces, rec_params


def inputs(store, now, preparation=None):
    head = (preparation['group_head'] if preparation is not None and 'group_head' in preparation
            else group_viz.prepared(store))
    if not head:
        return None
    vibe_rank = preparation['vibe_rank'] if preparation is not None else home_vibe.ranker(store, now)
    if vibe_rank.preset:
        # Vibe assembly reads ranks and group membership directly. The saved
        # preview owns the recommendation generation, but its candidate lists are unused.
        generations = (store.rec_proposals.preview_recommendations(head['generation'], group_serving.SCHEMA)
                       if group_serving.enabled(store) else [])
        preview = _selection_preview(head, generations[0]) if len(generations) == 1 else None
    else:
        preview = (preparation['group_preview'] if preparation is not None and 'group_preview' in preparation else
                   group_serving.matching_preview(store, head['generation']))
    generations = (preview or {}).get('recommendation_generations', [])
    uses_preview = bool(preview and preview['generation'] == head['generation'] and len(generations) == 1)
    if not uses_preview:
        preview = _published_fallback(store, head)
        if preview is None:
            return None
        generations = preview['recommendation_generations']
    allocation = group_learning.mix_allocation(store, preview, now)
    return {'preview': preview, 'uses_preview': uses_preview,
            'recommendation_generation': generations[0], 'allocation': allocation,
            'context': {**preview['context'], 'at': now, 'group_allocation': allocation,
                        'group_choice_policy': group_learning.POLICY,
                        'group_choice_parameters': group_learning.choice_bandit.parameters(store)},
            'by_version': {group['version']: group for group in preview['groups']},
            'meta': mode_surfaces.bundle_metadata(store, generation=generations[0]),
            'vibe_rank': vibe_rank}


def _published_fallback(store, head):
    """Assemble over published membership while the Home preview catches up."""
    publication = store.recommendations.head('home')
    if publication is None:
        return None
    return _selection_preview(head, publication['id'])


def _selection_preview(head, generation):
    context = {'schema': 1, 'group_generation': head['generation'],
               'musical_groups': [{key: group.get(key) for key in
                   ('group_id', 'version', 'parent_version', 'name', 'size')} for group in head['groups']]}
    return {**head, 'context': context, 'recommendation_generations': [generation]}


def assemble(store, now, epoch, data, *, include=(), avoid=(), mix_candidates=None):
    """Spend each leaf's allocation once, combining mass at a usable parent as needed."""
    from yt_playlist.rec import stored_groups
    with stored_groups.selection(store, data['recommendation_generation'], now) as selected:
        return _assemble(store, epoch, data, include, avoid, selected, mix_candidates)


def library_lanes(store, meta):
    comfort = meta.get('comfort_pool', 0) >= rec_params.get_param(store, 'comfort_min_pool')
    fourth = 'comfort' if comfort else 'temporal' if meta.get('year_cuts') else None
    lanes = ['wheelhouse', 'explore'] + ([fourth] if fourth else [])
    if comfort and meta.get('year_cuts'):
        lanes.append('temporal')
    return lanes


def _assemble(store, epoch, data, include, avoid, selected, mix_candidates):
    cap_artist = int(rec_params.get_param(store, 'modes_artist_cap'))
    cap_album = int(rec_params.get_param(store, 'modes_album_cap'))
    filter_key = (tuple(sorted(include)), tuple(sorted(avoid)))
    by_version = data['by_version']
    meta = data['meta']
    lanes = library_lanes(store, meta)
    vibe = data['vibe_rank']
    group_generation = data['preview']['generation']
    rng = random.Random(f"{group_generation}:{epoch}:{filter_key}:{vibe.preset}")
    defer_temporal = selected.preset and lanes[-2:] == ['comfort', 'temporal']
    initial_lanes = lanes[:-1] if defer_temporal else lanes
    ranks, memberships = selected.load_lanes(group_generation, initial_lanes, mix_candidates, data.get('memberships'))
    use_matches = bool(selected.preset and ranks)
    if selected.preset and not use_matches:
        ranks, memberships = selected.load_lanes(group_generation, initial_lanes, memberships=memberships or None,
                                                  matching_vibe=False)
    eligibility = selected.lane_eligibility(ranks)
    cards, seen, used = [], set(), set()
    pending_display = []
    for lane in lanes:
        if lane == 'temporal' and any(card['lane'] == 'comfort' for card in cards):
            continue
        check_stopping()
        lane_ranks, lane_eligibility, lane_uses_matches = ranks, eligibility, use_matches
        if lane == 'temporal' and defer_temporal:
            lane_ranks, memberships = selected.load_lanes(group_generation, [lane], memberships=memberships or None)
            lane_eligibility = selected.lane_eligibility(lane_ranks)
            lane_uses_matches = True
        groups = selected.lane_groups(lane_ranks, memberships, lane, epoch, include, avoid, seen, meta,
                                      eligibility=lane_eligibility)
        fallback = {}

        def choose_tracks(version):
            if groups is None:
                if version not in fallback:
                    fallback[version] = selected.tracks(version, lane, epoch, include, avoid, seen,
                                                        cap_artist, cap_album, meta)
                return fallback[version]
            return artist_variety.select(groups[version], cap_artist, cap_album,
                                        limit=mode_surfaces.PROTO_SIZE * 3)

        weights = _group_weights(data, used, choose_tracks if groups is None else groups.get,
                                 distinct=bool(selected.preset))
        if not weights and lane_uses_matches and groups is not None:
            # Sparse scores or active filters may leave no group with a full mix.
            lane_ranks, memberships = selected.load_lanes(group_generation, [lane], memberships=memberships or None,
                                                          matching_vibe=False)
            groups = selected.lane_groups(lane_ranks, memberships, lane, epoch, include, avoid, seen, meta,
                                          eligibility=selected.lane_eligibility(lane_ranks))
            weights = _group_weights(data, used, groups.get, distinct=True)
        if not weights:
            continue
        if selected.preset and groups is not None:
            version, items = _vibe_group(groups, weights, rng, selected, cap_artist, cap_album)
        else:
            def priority(version):
                weight = weights[version]
                if vibe.preset:
                    weight *= _vibe_weight(choose_tracks(version), vibe.affinity)
                return -math.log(max(rng.random(), 1e-12)) / max(weight, 1e-12)

            version = min(weights, key=priority)
            items = choose_tracks(version)
        group = by_version[version]
        # Rotate through a bounded ranked head rather than replaying the same
        # twelve tracks each time this region returns.
        start = (epoch * 3) % max(1, min(len(items), mode_surfaces.PROTO_SIZE * 2))
        rotated = items[start:] + items[:start]
        if selected.preset:
            rotated = artist_variety.distinct_edits(rotated)
        size = mode_surfaces.PROTO_SIZE
        items = list(islice(rotated, size))
        if selected.preset and groups is not None and len(items) < size:
            items = artist_variety.select(groups[version], cap_artist, cap_album,
                                          limit=size, minimum=size, initial=items, unique_edits=True)
        if not selected.preset:
            selected.default_affinity(items)
            if mix_candidates is None:
                selected.reader.recommendations.explain_candidates(selected.source(lane), items)
        cards.append({'lane': lane, 'label': mode_surfaces._CARD_LABELS.get(lane,
                         mode_surfaces._BAND_LABELS[epoch % 3]),
                      'mode_id': None, 'ranker': 'musical-group', 'tracks': items,
                      'group_id': group['group_id'], 'group_version': version,
                      'group_generation': data['preview']['generation'], 'group_name': group['name']})
        if selected.preset and groups is not None:
            pending_display.append(cards[-1])
        seen.update(t['key'] for t in items)
        used.add(version)
    if pending_display:
        selected.hydrate(pending_display)
        cards = [card for card in cards if len(card['tracks']) >= 4]
    return cards


def _group_weights(data, used, members, *, distinct=False):
    weights = {}
    full = {}
    for leaf in data['preview']['groups']:
        if not leaf.get('leaf'):
            continue
        group = leaf
        while group:
            version = group['version']
            if group.get('display'):
                if version not in full:
                    pool = members(version) or ()
                    if distinct and len(pool) >= mode_surfaces.PROTO_SIZE:
                        pool = list(islice(artist_variety.distinct_edits(pool), mode_surfaces.PROTO_SIZE))
                    # Variety caps can relax to fill a mix. Edit uniqueness still applies.
                    full[version] = len(pool) >= mode_surfaces.PROTO_SIZE
                if full[version]:
                    break
            group = data['by_version'].get(group['parent_version'])
        if group and version not in used:
            weights[version] = weights.get(version, 0.) + data['allocation'].get(leaf['group_id'], 0.)
    return weights


def _vibe_weight(items, affinity):
    size = mode_surfaces.PROTO_SIZE
    fit = sum(affinity(track) for track in items[:size]) / size
    return math.exp(6 * (fit - .5))


def _vibe_group(grouped, weights, rng, selected, artist_cap, album_cap):
    # Keep the same random draw and tie position for every feasible group.
    # A perfect fit bounds the best possible score before sorting its tracks.
    bounds = []
    maximum = math.exp(3)
    for position, (version, weight) in enumerate(weights.items()):
        draw = -math.log(max(rng.random(), 1e-12))
        bounds.append((draw / max(weight * maximum, 1e-12), position, version, weight, draw))
    best = (math.inf, math.inf)
    winner = None
    for lower, position, version, weight, draw in sorted(bounds):
        check_stopping()
        if (lower, position) >= best:
            break
        items = selected.vibe_tracks(grouped[version], artist_cap, album_cap)
        factor = _vibe_weight(items, lambda track: selected.affinities[track['key']])
        score = (draw / max(weight * factor, 1e-12), position)
        if score < best:
            best, winner = score, (version, items)
    return winner
