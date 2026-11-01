"""Conservative shared priors without copying historical parent wins into children."""
from yt_playlist.rec import choice_bandit, group_versions

POLICY = 'theme-raw-genres-current-hierarchy-4'


def compile(store, offers, responses, now, head):
    groups = {group['group_id']: group for group in head['groups'] if not group.get('legacy')}
    by_version = {group['version']: group for group in groups.values()}
    def ancestors(gid):
        seen = set()
        while gid is not None and gid not in seen:
            seen.add(gid)
            yield gid
            parent = by_version.get(groups[gid]['parent_version'])
            gid = parent['group_id'] if parent else None

    def destination(option):
        # Current descriptive raw genres identify a broad area. When several
        # leaves are indistinguishable, only their common ancestor is supported.
        # No historical versions, lineage, member tracks or vectors are read.
        genres = {label.strip().casefold() for label in option['genres']}
        matches = [group['group_id'] for group in groups.values() if group.get('leaf') and
                   genres & {entry[0].strip().casefold() for entry in group.get('genres', [])}]
        if not matches:
            return None
        paths = [list(ancestors(gid)) for gid in matches]
        common = set(paths[0]).intersection(*(set(path) for path in paths[1:]))
        return next((gid for gid in paths[0] if gid in common), None)

    excluded = {'unseen': 0, 'future_context': 0, 'missing_context': 0, 'unmapped_definition': 0,
                'legacy_ambiguous': 0}
    menus = {}
    visible, picked = {}, {}
    expiry = []
    for observation in choice_bandit.observations(store, offers, responses, now, excluded):
        option = observation['option']
        version = observation['context_id']  # Broad descriptor identity, not a group version.
        gid = destination(option)
        supported = set(ancestors(gid)) if gid is not None else set()
        if not supported:
            excluded['legacy_ambiguous' if option.get('mode_id') is not None else 'unmapped_definition'] += 1
            continue
        offer = (observation['opportunity'], option['id'])
        if gid is not None:
            visible.setdefault(gid, set()).add(offer)
            if observation['selected']:
                picked.setdefault(gid, set()).add(offer)
        expiry.append(observation['expires_at'])
        if observation['positive'] + observation['negative'] == 0:
            continue
        for ancestor in sorted(supported):
            key = (ancestor, observation['day'], observation['opportunity'])
            unit = menus.setdefault(key, {'positive': 0., 'negative': 0., 'versions': set()})
            unit['positive'] = max(unit['positive'], observation['positive'])
            unit['negative'] = max(unit['negative'], observation['negative'])
            unit['versions'].add(version)
    days = {}
    for (gid, day, oid), unit in menus.items():
        # Choosing one child is support for its broad area, not a simultaneous
        # rejection of that area because another sibling was visible.
        positive, negative = unit['positive'], unit['negative']
        if len(unit['versions']) > 1 and positive:
            negative = 0.
        record = days.setdefault((gid, day), {'positive': 0., 'negative': 0., 'menus': set()})
        record['positive'] = max(record['positive'], positive)
        record['negative'] = max(record['negative'], negative)
        record['menus'].add(oid)
    rows = {}
    for gid, group in groups.items():
        own = [unit for (identity, _day), unit in days.items() if identity == gid]
        all_menus = set().union(*(unit['menus'] for unit in own)) if own else set()
        positive = sum(unit['positive'] / max(1., unit['positive'] + unit['negative']) for unit in own)
        negative = sum(unit['negative'] / max(1., unit['positive'] + unit['negative']) for unit in own)
        parent = by_version.get(group['parent_version'])
        # Remove shared source opportunities before forming a bounded parent prior.
        shared = [unit for (identity, _day), unit in days.items()
                  if parent and identity == parent['group_id'] and not unit['menus'] & all_menus]
        sp = sum(unit['positive'] / max(1., unit['positive'] + unit['negative']) for unit in shared)
        sn = sum(unit['negative'] / max(1., unit['positive'] + unit['negative']) for unit in shared)
        strength = min(1., sp + sn)
        alpha = 1 + positive + (strength * sp / (sp + sn) if sp + sn else 0)
        beta = 1 + negative + (strength * sn / (sp + sn) if sp + sn else 0)
        rows[gid] = {'group_id': gid, 'version': group['version'], 'positive': positive, 'negative': negative,
                     'offered': len(visible.get(gid, ())), 'picked': len(picked.get(gid, ())),
                     'days': len(own), 'menus': sorted(all_menus), 'shared_strength': strength,
                     'shared_from': parent['version'] if parent and shared else None,
                     'alpha': alpha, 'beta': beta, 'estimate': alpha / (alpha + beta)}
    params = choice_bandit.parameters(store)
    return {'schema': 1, 'policy': POLICY, 'generation': head['generation'], 'parameters': params,
            'at': now, 'expires_at': min(expiry) if expiry else None, 'rows': list(rows.values()),
            'offered': sum(len(items) for items in visible.values()),
            'picked': sum(len(items) for items in picked.values()),
            'excluded': excluded, 'state_version': group_versions.fingerprint([
                POLICY, head['generation'], params, list(rows.values())])}


def current_allocation(store, preview, now):
    """The effective serving input, excluding report timestamps and diagnostics."""
    stats = store.rec_proposals.get_proposals('group_choice_bandit') or {}
    if (stats.get('policy') != POLICY or stats.get('generation') != preview['generation']
            or stats.get('parameters') != choice_bandit.parameters(store)
            or stats.get('at', now) > now
            or stats.get('expires_at') is not None and now >= stats['expires_at']):
        stats = {}
    return allocation({'groups': preview['groups']}, stats)


def allocation(head, stats=None):
    """Conserve musical-area mass while refining within each area; no independent sibling tickets."""
    groups = {group['group_id']: group for group in head['groups'] if not group.get('legacy')}
    by_version = {group['version']: group for group in groups.values()}
    children = {}
    for group in groups.values():
        parent = by_version.get(group['parent_version'])
        children.setdefault(parent['group_id'] if parent else None, []).append(group)
    estimates = {row['group_id']: row['estimate'] for row in (stats or {}).get('rows', [])}
    weights = {}

    def visit(parent, mass):
        siblings = children.get(parent, [])
        if not siblings:
            if parent is not None:
                weights[parent] = mass
            return
        raw = {g['group_id']: g['size'] * estimates.get(g['group_id'], .5) for g in siblings}
        total = sum(raw.values())
        for group in siblings:
            visit(group['group_id'], mass * raw[group['group_id']] / total if total else 0)

    visit(None, 1.)
    return weights
