"""Taste Model readings over the same immutable graph definitions Home serves."""
import numpy as np

from yt_playlist.rec import choice_bandit, group_serving, layers, rec_params


def prepared(store):
    return (store.rec_proposals.get_proposals(group_serving.SURFACE)
            if group_serving.checkpoint(store) else None)


def mode_layers(store, now, preview):
    by_version = {g['version']: g for g in preview['groups']}
    members = store.taste_groups.leaf_members(preview['generation'])
    targets = {}
    sizes = {}
    for version in set(members.values()):
        group = by_version.get(version)
        while group and not group.get('display'):
            group = by_version.get(group['parent_version'])
        targets[version] = group['group_id'] if group else None
    assignments = {key: targets[version] for key, version in members.items() if targets[version]}
    for gid in assignments.values():
        sizes[gid] = sizes.get(gid, 0) + 1
    modes = [{'mode_id': g['group_id'], 'label': g['name'], 'size': sizes[g['group_id']]}
             for g in preview['groups'] if g['group_id'] in sizes]
    gp = rec_params.get_param
    window = gp(store, 'now_window_h')
    half = gp(store, 'session_halflife_h')
    minimum = int(gp(store, 'now_min_events'))
    latest = {}
    for event in layers._plays(store, now - max(24, window) * 3600):
        key, at = event['identity_key'], event['played_at']
        if at <= now:
            latest[key] = max(latest.get(key, at), at)
    def reading(hours, decay=False):
        recent = {key: at for key, at in latest.items() if at >= now - hours * 3600}
        mapped = {key: at for key, at in recent.items() if key in assignments}
        counts = {}
        if len(mapped) >= minimum:
            for key, at in mapped.items():
                gid = assignments[key]
                counts[gid] = counts.get(gid, 0) + (2 ** (-(now - at) / (half * 3600)) if decay else 1)
        total = sum(counts.values())
        return {gid: mass / total for gid, mass in counts.items()}, len(mapped), len(recent)
    current, now_n, now_total = reading(window)
    session, session_n, session_total = reading(24, True)
    def segments(shares):
        return [{**m, 'share': shares[m['mode_id']], 'color_idx': i}
                for i, m in enumerate(modes) if shares.get(m['mode_id'])]
    total = sum(sizes.values())
    return {'adaptive': True, 'modes': modes, 'styles_current': True, 'min_events': minimum,
            'now': {'segments': segments(current), 'n': now_n, 'window_h': window},
            'session': {'segments': segments(session), 'n': session_n, 'halflife_h': half},
            'permanent': {'segments': segments({gid: n / total for gid, n in sizes.items()}), 'n': total},
            'evidence': {'now': {'mapped': now_n, 'total': now_total},
                         'session': {'mapped': session_n, 'total': session_total}}}


def choices(store, now, preview):
    state = store.rec_proposals.get_proposals('group_choice_bandit') or {}
    if (state.get('generation') != preview['generation']
            or state.get('parameters') != choice_bandit.parameters(store)
            or state.get('at', now) > now
            or state.get('expires_at') is not None and now >= state['expires_at']):
        state = {}
    stats = {r['group_id']: r for r in state.get('rows', [])}
    rows = []
    for color, group in enumerate(g for g in preview['groups'] if g.get('display')):
        gid = group['group_id']
        row = stats.get(gid, {})
        alpha, beta = row.get('alpha', 1), row.get('beta', 1)
        offered, picked = row.get('offered', 0), row.get('picked', 0)
        low, high = (np.quantile(np.random.default_rng(int(gid[:8], 16)).beta(alpha, beta, 4096), [.1, .9])
                     if offered else (.1, .9))
        rows.append({'mode_id': gid, 'label': group['name'], 'color_idx': color,
                     'offered': offered, 'picked': picked, 'has_evidence': bool(offered),
                     'alpha': alpha, 'beta': beta, 'estimate': alpha / (alpha + beta),
                     'low': float(low), 'high': float(high),
                     'profile': {'title': group['name'], 'size': group['size'], 'current': True,
                                 'genres': [], 'eras': [], 'sound': []}})
    strongest = sorted((r for r in rows if r['picked']), key=lambda r: -r['low'])[:3]
    return {'adaptive': True, 'rows': rows, 'strongest': strongest, 'contrast': None,
            'offered': state.get('offered', 0), 'picked': state.get('picked', 0),
            'window_days': rec_params.get_param(store, 'bandit_window_d')}
