"""Prepare versioned musical interests and mix candidates from production pools."""
import json

from yt_playlist.core.activity import work_budget, WorkBudgetExceeded, check_stopping
from yt_playlist.core.work_diagnostics import maintenance_job
from yt_playlist.rec import group_learning, group_preparation, mode_surfaces, rec_params
from yt_playlist.rec.eligibility import home_exclusions

SURFACE = 'adaptive_group_preview'
SCHEMA = 2
PHASE = 'rec: adaptive mix shadow'


def enabled(store):
    return store.settings.get_setting('rec_group_serving') != 'legacy'


def checkpoint(store):
    prepared = store.rec_proposals.group_checkpoint() if enabled(store) else None
    return prepared if prepared and prepared['schema'] == SCHEMA and prepared['mix_regions'] else None


def policy(store):
    return 'adaptive' if checkpoint(store) else 'legacy'


def calculate(store, head, now):
    groups = [group for group in head['groups'] if not group.get('legacy')]
    by_version = {group['version']: group for group in groups}
    bundles = store.rec_proposals.get_proposals('mode_bundles') or {}
    pools = bundles.get('all', {})
    keys = {item['key'] for pool in pools.values() for item in pool if item.get('key')}
    blocked = home_exclusions(store, keys, now)
    muted = store.rec_model.muted_artists()
    cap_artist = int(rec_params.get_param(store, 'modes_artist_cap'))
    cap_album = int(rec_params.get_param(store, 'modes_album_cap'))
    stats = store.rec_proposals.get_proposals('group_choice_bandit') or {}
    if (stats.get('generation') != head['generation'] or stats.get('parameters') != group_learning.choice_bandit.parameters(store)
            or stats.get('at', now) > now or stats.get('expires_at') is not None and now >= stats['expires_at']):
        stats = {}
    allocation = group_learning.allocation(head, stats)
    prepared = {}
    for group in groups:
        check_stopping()
        members = set(group.get('members', {}))
        frames = {}
        for frame, pool in pools.items():
            eligible, seen = [], set()
            for item in pool:
                key = item.get('key')
                if key in members and key not in blocked and key not in seen and item.get('artist') not in muted:
                    eligible.append(item)
                    seen.add(key)
            diverse = mode_surfaces._diversify(eligible, cap_artist, cap_album)
            frames[frame] = {'eligible': len(diverse), 'full_mix': len(diverse) >= mode_surfaces.PROTO_SIZE,
                             # Keep inspectable identities, not repeated copies of
                             # every track's model/provenance payload at each parent.
                             'tracks': [{field: item.get(field) for field in ('key', 'title', 'artist')}
                                        for item in diverse[:mode_surfaces.PROTO_SIZE]],
                             'keys': [item['key'] for item in eligible]}
        prepared[group['group_id']] = {key: group.get(key) for key in
            ('group_id', 'version', 'parent_version', 'name', 'subtitle', 'size', 'leaf', 'display')}
        prepared[group['group_id']].update(frames=frames, allocation=allocation.get(group['group_id'], 0.))
    for group in groups:
        if not group.get('leaf'):
            continue
        record = prepared[group['group_id']]
        for frame, candidates in record['frames'].items():
            fallback = None
            parent = by_version.get(group['parent_version'])
            while (not candidates['full_mix'] or not group.get('display')) and parent:
                parent_mix = prepared[parent['group_id']]['frames'].get(frame, {})
                if parent.get('display') and parent_mix.get('full_mix'):
                    fallback = parent['version']
                    break
                parent = by_version.get(parent['parent_version'])
            candidates['fallback_version'] = fallback
    menus = {}
    for frame in pools:
        weights = {}
        for group in groups:
            if not group.get('leaf'):
                continue
            candidates = prepared[group['group_id']]['frames'][frame]
            version = (group['version'] if group.get('display') and candidates['full_mix']
                       else candidates['fallback_version'])
            if version:
                weights[version] = weights.get(version, 0.) + allocation.get(group['group_id'], 0.)
        total = sum(weights.values())
        menus[frame] = [{'group_version': version, 'group_id': by_version[version]['group_id'],
                         'name': by_version[version]['name'], 'probability': weight / total}
                        for version, weight in weights.items()] if total else []
    context = {'schema': 1, 'at': now, 'grouping_policy': head['policy'], 'group_generation': head['generation'],
               'musical_groups': [{key: group.get(key) for key in
                   ('group_id', 'version', 'parent_version', 'name', 'size')} for group in groups]}
    return {'schema': SCHEMA, 'at': now, 'generation': head['generation'], 'shadow': not enabled(store),
            'serving_policy': 'adaptive' if enabled(store) else 'legacy',
            'groups': list(prepared.values()), 'context': context,
            'counts': head['payload']['counts'], 'menus': menus}


def prepare(store, now, *, seconds=10):
    try:
        with maintenance_job(PHASE), work_budget(seconds), store.read_snapshot() as snapshot:
            if snapshot.store.settings.get_setting('rec_group_shadow') == '0':
                store.maintenance_diagnostics.resolve_wait(PHASE, 'disabled')
                return None
            head = snapshot.store.taste_groups.head(members=True)
            if head is None:
                store.maintenance_diagnostics.record('deferred', phase=PHASE, reasons=('group-head',))
                return None
            result = calculate(snapshot.store, head, now)
            encoded = json.dumps(result)
            check_stopping()
            if snapshot.publish(lambda: store.rec_proposals.publish_prepared_home(encoded, now, surface=SURFACE)):
                store.maintenance_diagnostics.record('completed', phase=PHASE)
                return result
    except WorkBudgetExceeded:
        store.maintenance_diagnostics.record('deferred', phase=PHASE,
                                             reasons=('group-preview-budget',))
    return None


def view(store):
    """Retain a complete preview until its replacement is ready, without mixing generations."""
    preview = store.rec_proposals.get_proposals(SURFACE)
    generation = store.taste_groups.head_generation()
    if not preview or not generation:
        return None
    visible = []
    by_version = {group['version']: group for group in preview['groups']}
    for group in preview['groups']:
        if not group.get('display'):
            continue
        parent = by_version.get(group['parent_version'])
        seen = {group['version']}
        while parent and parent['version'] not in seen and not parent.get('display'):
            seen.add(parent['version'])
            parent = by_version.get(parent['parent_version'])
        if parent and parent['version'] in seen:
            parent = None
        visible.append({**group, 'parent_name': parent['name'] if parent else None,
                        'mix_ready': any(frame['full_mix'] for frame in group['frames'].values())})
    return {**preview, 'groups': visible, 'updating': preview['generation'] != generation}


def progress(store):
    return {**group_preparation.status(store), **store.taste_groups.preparation_progress(),
            'enabled': store.settings.get_setting('rec_group_shadow') != '0'}
