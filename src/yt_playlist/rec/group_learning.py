"""Conservative shared priors without copying historical parent wins into children."""
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.rec import choice_bandit, group_versions

POLICY = 'hierarchical-context-response-3'


def compile(store, offers, responses, now, head):
    groups = {group['group_id']: group for group in head['groups'] if not group.get('legacy')}
    by_version = {group['version']: group for group in groups.values()}
    links, resolved = {}, {}
    edge_count = 0

    def successors(version):
        nonlocal edge_count
        if version not in links:
            check_stopping()
            if edge_count >= 8192:
                raise WorkBudgetExceeded('Musical response lineage exceeds learning work ceiling')
            edges = store.taste_groups.successors(version, limit=min(512, 8192 - edge_count))
            edge_count += len(edges)
            compatible = [edge for edge in edges if edge['payload'].get('compatible')]
            # A surviving parent owns its historical response even when a large
            # new child also has high overlap with its former leaf definition.
            continuations = [edge for edge in compatible if edge['relation'] == 'continuation']
            links[version] = [edge['target'] for edge in continuations or compatible]
        return links[version]

    def destination(version):
        # A historical parent can continue as today's umbrella. It is never
        # distributed as child-specific wins. Ambiguous paths are excluded.
        if version not in resolved:
            pending, seen, matches = [version], set(), set()
            try:
                while pending and len(seen) < 512:
                    candidate = pending.pop()
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    if candidate in by_version:
                        matches.add(by_version[candidate]['group_id'])
                    else:
                        pending.extend(successors(candidate))
                resolved[version] = next(iter(matches)) if len(matches) == 1 and not pending else None
            except WorkBudgetExceeded:
                resolved[version] = None
        return resolved[version]

    def ancestors(gid):
        while gid is not None:
            yield gid
            parent = by_version.get(groups[gid]['parent_version'])
            gid = parent['group_id'] if parent else None

    excluded = {'unseen': 0, 'future_context': 0, 'missing_context': 0, 'unmapped_definition': 0,
                'legacy_ambiguous': 0}
    menus = {}
    visible, picked = {}, {}
    choices = {(event['opportunity_id'], event['option_id']) for event in responses
               if event['kind'] == 'choice' and event['at'] <= now and event['observed_at'] <= now}
    definitions = {}
    expiry = []
    for observation in choice_bandit.observations(store, offers, responses, now, excluded):
        option, context = observation['option'], observation['context']
        # Only a version actually frozen in the served menu's context is admissible.
        version = option.get('group_version')
        historical = {group.get('version'): group for group in context.get('musical_groups', [])}

        def frozen_definition(version):
            source = historical.get(version) if version else None
            if version not in definitions:
                definitions[version] = (next((definition for definition in context['musical_definitions']
                                             if definition['version'] == version), None)
                                        if 'musical_definitions' in context else
                                        store.taste_groups.definition(version) if version else None)
            definition = definitions[version]
            if (not definition or not source or source.get('group_id') != definition['group_id']
                    or context.get('group_generation') != definition['generation']
                    or definition['created_at'] > context.get('at', observation['offer_at'])):
                return None
            return definition

        definition = frozen_definition(version)
        gid = destination(version) if definition else None
        supported = set(ancestors(gid)) if gid is not None else set()
        # A vanished child can lose its specific mapping while its already
        # supported broad parent continues unchanged. Resolve those frozen
        # ancestors independently; never loosen child compatibility or copy a
        # parent's historical response into today's children.
        seen = {version}
        parent = definition.get('parent_version') if definition else None
        while parent and parent not in seen and len(seen) < 512:
            seen.add(parent)
            definition = frozen_definition(parent)
            if definition is None:
                break
            if (mapped := destination(parent)) is not None:
                supported.update(ancestors(mapped))
            parent = definition.get('parent_version')
        if not supported:
            excluded['legacy_ambiguous' if option.get('mode_id') is not None else 'unmapped_definition'] += 1
            continue
        offer = (observation['opportunity'], option['id'])
        if gid is not None:
            visible.setdefault(gid, set()).add(offer)
            if offer in choices:
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
    if (stats.get('generation') != preview['generation']
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
