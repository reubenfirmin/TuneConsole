"""Windowed mix-context response evidence, compiled off the serving path.

This is an allocation heuristic, not an enjoyment model or a causal ranker comparison. A menu
with a known selection supplies relative choice evidence for visible alternatives. Unseen menus
and missing downstream observations supply no failure. Response strengths are explicit hypotheses.
Correlated menus are capped at one unit per musical group and UTC day.
"""
import hashlib
import json
import math

import numpy as np

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.library.listen_derive import classify_exit
from yt_playlist.rec import rec_params, taste_modes
from yt_playlist.rec.choice_context import learning_context

WEIGHTS = ('choice', 'open', 'start', 'completion', 'promotion', 'early_exit')
POLICY = 'context-response-v1'


def parameters(store):
    return {name: rec_params.get_param(store, 'bandit_reward_' + name) for name in WEIGHTS} | {
        'window_d': rec_params.get_param(store, 'bandit_window_d'),
        'match_threshold': rec_params.get_param(store, 'modes_match_threshold')}


def generation(modes):
    values = [{key: (np.asarray(mode[key]).tolist() if key == 'centroid' else mode[key])
               for key in ('mode_id', 'space', 'centroid', 'rep_keys')}
              for mode in sorted(modes, key=lambda mode: mode['mode_id'])]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _decisions(offers, visible, selections):
    """Keep later deliberate decisions from held menus on their own calendar occasions."""
    for oid, offer in offers.items():
        decisions = selections.get(oid) or [(None, visible.get(oid, set()), offer['at'])]
        for index, (chosen, seen, at) in enumerate(decisions):
            start = offer['at'] if index == 0 else at
            end = decisions[index + 1][2] if index + 1 < len(decisions) else math.inf
            yield oid, offer, chosen, seen, at, start, end


def observations(store, offers, responses, now, excluded):
    """Visibility/time/outcome admission shared by legacy and hierarchical compilers.

    This yields actual served options and their frozen context, without assigning
    them to today's groups. Mapping and historical compatibility remain separate.
    """
    params = parameters(store)
    since = now - params['window_d'] * 86400
    visible, selections, actions, contexts = {}, {}, {}, {}
    for event in responses:
        check_cancellable_work()
        offer = offers.get(event['opportunity_id'])
        if (not offer or offer['format'] != 'mixes' or not since < offer['at'] <= now
                or event['observed_at'] > now or event['at'] > now):
            continue
        oid = offer['id']
        if event['kind'] == 'visible':
            visible.setdefault(oid, set()).update(event['visible'])
        elif event['kind'] == 'choice':
            selections.setdefault(oid, []).append((event['option_id'], set(visible.get(oid, ())), event['at']))
        if event['option_id'] is not None:
            actions.setdefault((oid, event['option_id']), []).append(event)
    for oid, offer, chosen, seen, at, start, end in _decisions(offers, visible, selections):
        if offer['format'] != 'mixes' or not since < offer['at'] <= now:
            continue
        check_cancellable_work()
        if not seen:
            excluded['unseen'] += 1
            continue
        context_id = offer['context']
        if context_id not in contexts:
            try:
                contexts[context_id] = learning_context(store, context_id)
            except ValueError:
                contexts[context_id] = None
        if contexts[context_id] is None:
            excluded['missing_context'] += 1
            continue
        if contexts[context_id].get('at', offer['at']) > offer['at']:
            excluded['future_context'] += 1
            continue
        for option in offer['options']:
            option_id = str(option['id'])
            if option_id not in seen:
                continue
            events = [event for event in actions.get((oid, option_id), []) if start <= event['at'] < end]
            kinds = {event['kind'] for event in events}
            completion = early = False
            for event in events:
                details = event.get('details') or {}
                position, duration = details.get('position'), details.get('duration')
                if event['kind'] != 'experience' or details.get('kind') not in {'track_exit', 'ended', 'bye'}:
                    continue
                if not all(isinstance(value, (int, float)) and math.isfinite(value)
                           for value in (position, duration)) or position < 0:
                    continue
                classification = classify_exit(position, duration)
                completion |= classification == 'completion'
                early |= classification == 'skip' and details['kind'] == 'track_exit'
            selected = chosen == option_id
            positive = ((params['choice'] if selected else 0)
                        + params['open'] * ('open' in kinds)
                        + params['start'] * ('start' in kinds)
                        + params['completion'] * completion
                        + params['promotion'] * ('promote' in kinds))
            negative = params['choice'] * (chosen is not None and not selected) + params['early_exit'] * early
            yield {'opportunity': oid, 'option': option, 'context': contexts[context_id],
                   'context_id': context_id, 'positive': positive, 'negative': negative,
                   'day': int(at // 86400), 'offer_at': offer['at'],
                   'expires_at': offer['at'] + params['window_d'] * 86400}


def compile(store, offers, responses, now):
    params = parameters(store)
    since = now - params['window_d'] * 86400
    current = store.modes.list_modes(active_only=True)
    modes = {mode['mode_id']: mode for mode in current}
    units, compatibility, expirations = {}, {}, []
    excluded = {'unseen': 0, 'unmapped_mode': 0, 'drifted_mode': 0,
                'future_context': 0, 'missing_context': 0}
    for observation in observations(store, offers, responses, now, excluded):
        mid = observation['option'].get('mode_id')
        historical = {mode['mode_id']: mode for mode in observation['context'].get('groups', [])}
        old, live = historical.get(mid), modes.get(mid)
        if old is None or live is None:
            excluded['unmapped_mode'] += 1
            continue
        pair = (observation['context_id'], mid)
        if pair not in compatibility:
            matched, _ = taste_modes.reconcile([old], [live], threshold=params['match_threshold'])
            compatibility[pair] = matched[0]['mode_id'] == mid
        if not compatibility[pair]:
            excluded['drifted_mode'] += 1
            continue
        positive, negative = observation['positive'], observation['negative']
        if positive + negative == 0:
            continue
        unit = units.setdefault((mid, observation['day']), {'positive': 0., 'negative': 0., 'menus': set()})
        unit['positive'] = max(unit['positive'], positive)
        unit['negative'] = max(unit['negative'], negative)
        unit['menus'].add(observation['opportunity'])
        expirations.append(observation['expires_at'])
    rows = {}
    for (mid, day), unit in units.items():
        row = rows.setdefault(mid, {'mode_id': mid, 'label': modes[mid]['label'],
                                   'positive': 0., 'negative': 0., 'days': 0, 'menus': 0})
        scale = max(1., unit['positive'] + unit['negative'])
        row['positive'] += unit['positive'] / scale
        row['negative'] += unit['negative'] / scale
        row['days'] += 1
        row['menus'] += len(unit['menus'])
    for row in rows.values():
        alpha, beta = 1 + row['positive'], 1 + row['negative']
        row['estimate'] = alpha / (alpha + beta)
        rng = np.random.default_rng(row['mode_id'])
        row['low'], row['high'] = map(float, np.quantile(rng.beta(alpha, beta, 8192), [.1, .9]))
    state = {'schema': 1, 'policy': POLICY, 'parameters': params, 'generation': generation(current),
             'rows': list(rows.values()), 'excluded': excluded}
    effective = {'policy': POLICY, 'parameters': params, 'generation': state['generation'],
                 'rows': [{key: row[key] for key in ('mode_id', 'positive', 'negative', 'days')}
                          for row in state['rows']]}
    state['state_version'] = hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()
    return {**state, 'at': now, 'since': since, 'expires_at': min(expirations) if expirations else None}


def stats(store, now):
    """Unknown/stale compiled state returns a neutral prior; never revive legacy pseudo-failures."""
    state = store.rec_proposals.get_proposals('choice_bandit')
    if state is None:
        return None  # Before first shared compilation, legacy diagnostics remain compatible.
    if (state['at'] > now or state['parameters'] != parameters(store)
            or state['generation'] != generation(store.modes.list_modes(active_only=True))
            or state['expires_at'] is not None and now >= state['expires_at']):
        return {}
    return {row['mode_id']: (row['positive'], row['positive'] + row['negative']) for row in state['rows']}
