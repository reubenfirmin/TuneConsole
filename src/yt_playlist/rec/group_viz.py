"""Taste Model readings over the same immutable graph definitions Home serves."""
from copy import deepcopy
from threading import Lock
from types import MappingProxyType

import numpy as np

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.library import listening_composition as layers
from yt_playlist.rec import choice_bandit, rec_params


class PublishedGroupGraph:
    """Keep one decoded publication per library, shared by its readers."""

    def __init__(self):
        self._current = None
        self._memberships = None
        self._lock = Lock()

    def memberships(self, store, generation):
        key = _graph_key(store, generation)
        if store.conn.in_transaction and not getattr(store, '_read_only', False):
            return store.taste_groups.recording_groups(generation)
        current = self._memberships
        if current is None or current[0] != key:
            memberships = store.taste_groups.recording_groups(generation)
            current = key, MappingProxyType({recording: tuple(versions)
                                             for recording, versions in memberships.items()})
            self._memberships = current
        return current[1]

    def read(self, store):
        generation = store.taste_groups.head_generation()
        if generation is None:
            return None
        read_only = getattr(store, '_read_only', False)
        in_transaction = store.conn.in_transaction
        if in_transaction and not read_only:
            return _load_graph(store, generation)
        key = _graph_key(store, generation)
        current = self._current
        if current is not None and current[0] == key:
            return deepcopy(current[1])
        # A pinned reader may see an old head. A caller holding the writer lock
        # must never wait for a cache fill that uses an admitted reader.
        if in_transaction or getattr(store._lock, 'held_by_current_thread', False):
            return _load_graph(store, generation)
        if not read_only:
            with store.read_view() as reader:
                return self.read(reader)

        while not self._lock.acquire(timeout=.05):
            check_cancellable_work()
        try:
            # Check again after waiting for a concurrent fill or publication.
            generation = store.taste_groups.head_generation()
            if generation is None:
                return None
            key = _graph_key(store, generation)
            if self._current is None or self._current[0] != key:
                self._current = key, _load_graph(store, generation)
            graph = self._current[1]
        finally:
            self._lock.release()
        return deepcopy(graph)


def _graph_key(store, generation):
    source = getattr(store, '_evidence_source', store)
    return getattr(source, '_database_generation', 0), generation


def prepared(store):
    cache = getattr(store, '_published_group_graph', None)
    return (cache.read(store) if cache is not None
            else _load_graph(store, store.taste_groups.head_generation()))


def recording_groups(store, generation):
    cache = getattr(store, '_published_group_graph', None)
    return (cache.memberships(store, generation) if cache is not None
            else store.taste_groups.recording_groups(generation))


def _load_graph(store, generation):
    if generation is None:
        return None
    groups = [group for group in store.taste_groups.definitions(generation) if not group.get('legacy')]
    return {'generation': generation, 'groups': groups} if groups else None


def reading(now, recent, assignments, minimum, *, half=None):
    mapped = {key: at for key, at in recent.items() if key in assignments}
    counts = {}
    if len(mapped) >= minimum:
        for key, at in mapped.items():
            group = assignments[key]
            weight = 2 ** (-(now - at) / (half * 3600)) if half else 1
            counts[group] = counts.get(group, 0) + weight
    total = sum(counts.values())
    return {group: mass / total for group, mass in counts.items()}, len(mapped), len(recent)


def mode_layers(store, now, preview, *, observed=False):
    half = rec_params.get_param(store, 'session_halflife_h')
    minimum = int(rec_params.get_param(store, 'now_min_events'))
    generation = preview['generation']
    plays = layers.observed_layer_plays(store, now) if observed else layers.layer_plays(store, now)
    return layer_facts(now, preview, store.taste_groups.leaf_members(generation) if generation else {},
        plays, half=half, minimum=minimum)


def layer_facts(now, preview, members, plays, *, half, minimum):
    by_version = {g['version']: g for g in preview['groups']}
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
    current, now_n, now_total = reading(now, plays.current, assignments, minimum)
    session, session_n, session_total = reading(now, plays.today, assignments, minimum, half=half)
    def segments(shares):
        return [{**m, 'share': shares[m['mode_id']], 'color_idx': i}
                for i, m in enumerate(modes) if shares.get(m['mode_id'])]
    total = sum(sizes.values())
    return {'adaptive': True, 'modes': modes, 'styles_current': True, 'min_events': minimum,
            'now': {'segments': segments(current), 'n': now_n},
            'session': {'segments': segments(session), 'n': session_n, 'halflife_h': half},
            'permanent': {'segments': segments({gid: n / total for gid, n in sizes.items()}), 'n': total},
            'evidence': {'now': {'mapped': now_n, 'total': now_total,
                                 'coverage': now_n / now_total if now_total else 0.},
                         'session': {'mapped': session_n, 'total': session_total,
                                     'coverage': session_n / session_total if session_total else 0.}}}


def choices(store, now, preview):
    state = store.rec_proposals.get_proposals('group_choice_bandit') or {}
    return choice_facts(now, preview, state, parameters=choice_bandit.parameters(store),
                        window=rec_params.get_param(store, 'bandit_window_d'))


def choice_facts(now, preview, state, *, parameters, window):
    if (state.get('generation') != preview['generation']
            or state.get('parameters') != parameters
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
            'window_days': window}
