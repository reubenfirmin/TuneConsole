"""Serving assignments and outcome rules for the production model comparison.

Whole menus and continuing Radio episodes retain their assignment. New windows
share a randomized UTC-day assignment, so correlated tracks/branches cannot
inflate the decision's sample size. Decisions happen once at a fixed horizon;
successive attempts spend a summable error budget. Raw evidence remains in the
shared RecLedgerRepo, with compact lifecycle state in rec_proposals.
Worker preparation and lifecycle transitions live in comparison_preparation;
serving and evidence capture never import that orchestration.
"""
import hashlib
import math
import zlib
from collections import defaultdict

import numpy as np

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical

DAY = 86400
SURFACE = 'model_live_comparison'
MONITOR = 'model_listening_monitor'
FORMATS = ('mixes', 'albums', 'radio')
POLICY = {'version': 'choice-episode-day-v1', 'horizon_days': 28,
          'followup_hours': 24, 'min_days': 7, 'min_observed': 20,
          'min_coverage': .7, 'useful_effect': .02, 'format_margin': .1,
          'min_format_days': 3, 'retry_days': 7, 'family_alpha': .05}


def state(store):
    return store.rec_proposals.get_proposals(SURFACE) or {}


def constraints(store):
    """Standing intent is shared by both arms; a change ends the current epoch."""
    with store._lock:
        values = [(row[0], row[1]) for row in store.conn.execute(
            "SELECT key,value FROM settings WHERE key LIKE 'rec_param:%' "
            "OR key IN ('album_feedback','rec_cooc_weighting') ORDER BY key")]
        standing = [tuple(row) for row in store.conn.execute(
            'SELECT axis,value FROM rec_lean ORDER BY axis')]
    return hashlib.sha256(canonical([values, standing])).hexdigest()


def assignment(store, format, window, now, *, existing=None):
    """Read small published state only; never fit, scan evidence or draw twice."""
    current = state(store)
    if existing:
        return existing
    if (store.settings.get_setting('rec_selection_paused') == '1'
            or current.get('phase') != 'observing' or now >= current['exposure_until']
            or now < current['started_at'] or constraints(store) != current['constraints']):
        return None
    day = int(now // DAY)
    coin = hashlib.sha256(canonical([current['seed'], day])).digest()[0] < 128
    return {'run_id': current['run_id'], 'arm': 'challenger' if coin else 'incumbent',
            'probability': .5, 'unit': str(day), 'format': format, 'window': str(window),
            'policy': current['policy']['version'], 'generation': current['generation'],
            'at': now, 'controlled': True}


def prepared(store, format, window, now, *, existing=None, tuning=None):
    """Use a paired projection only under the tuning it was prepared against."""
    selected = assignment(store, format, window, now, existing=existing)
    if not selected:
        return None, None
    payload = store.rec_proposals.get_proposals(pair_surface(selected['run_id'], selected['arm'], format))
    if not payload or payload.get('run_id') != selected['run_id']:
        return None, None
    from yt_playlist.rec import comparison_storage
    try:
        data = comparison_storage.decode(payload)
    except (ValueError, KeyError, TypeError, zlib.error):
        return None, None
    if tuning is not None and data.get('tuning') != tuning:
        # Missing stamps from older preparations are also ineligible. The normal
        # tailored menu remains available without a controlled attribution.
        return None, None
    return data, selected


def pair_surface(run_id, arm, format):
    return f'model_pair:{run_id}:{arm}:{format}'


def monitoring_assignment(store, format, now):
    plan = store.rec_proposals.get_proposals(MONITOR) or {}
    if (store.settings.get_setting('rec_selection_paused') == '1'
            or not plan or not plan['started_at'] <= now < plan['exposure_until']):
        return None
    from yt_playlist.rec import model_setup
    if plan.get('setup') and model_setup.current_setup(store) != plan['setup']:
        return None
    return {'run_id': plan['run_id'], 'arm': 'challenger', 'unit': str(int(now // DAY)),
            'format': format, 'generation': plan['generation'], 'controlled': False, 'probability': 1.}


def _interval(old, new, alpha):
    """Bounded-mean difference interval over days, never generated track counts."""
    difference = float(np.mean(new) - np.mean(old)) if old and new else None
    if len(old) < 2 or len(new) < 2:
        return {'difference': difference, 'interval': None}
    # Hoeffding's bound for a difference of two [0,1] means. It also
    # preserves uncertainty when every observed day has the same outcome.
    radius = math.sqrt(.5 * math.log(2 / alpha) * (1 / len(old) + 1 / len(new)))
    return {'difference': difference, 'interval': [max(-1., difference - radius), min(1., difference + radius)]}


def measure(store, current, now):
    """Recompute delayed/corrected outcomes from stable source IDs at one cutoff."""
    from yt_playlist.library.listen_derive import classify_exit
    ledger = store.rec_ledger.evidence()
    events = [event for event in ledger.records('response') if event['observed_at'] <= now]
    withdrawn = {event['details'].get('action_id') for event in events if event['kind'] == 'withdraw'}
    by_offer = defaultdict(list)
    for event in events:
        if event['id'] not in withdrawn and event['kind'] != 'withdraw':
            by_offer[event['opportunity_id']].append(event)
    blocks, bounds = defaultdict(list), defaultdict(list)
    counts, source_ids = defaultdict(lambda: defaultdict(int)), []
    for offer in ledger.records('opportunity'):
        check_cancellable_work()
        monitoring = current.get('mode') == 'monitor'
        assigned = offer.get('monitor' if monitoring else 'comparison') or {}
        if (assigned.get('run_id') != current['run_id'] or assigned.get('controlled') is not (not monitoring) or assigned.get('censored')
                or assigned.get('probability') != (1. if monitoring else .5) or assigned.get('arm') not in ('incumbent', 'challenger')
                or assigned.get('generation') != current['generation']
                or not current['started_at'] <= offer['at'] < current['exposure_until']
                or offer['observed_at'] > now):
            continue
        format, arm = offer['format'], assigned['arm']
        row = counts[(format, arm)]
        row['offered'] += 1
        responses = sorted(by_offer[offer['id']], key=lambda event: (event['at'], event['observed_at'],
            {'visible': 0, 'choice': 1, 'start': 2, 'experience': 3}.get(event['kind'], 4), event['id']))
        visible, chosen, started, endings, closed = set(), set(), set(), [], False
        for event in responses:
            if not offer['at'] <= event['at'] <= offer['at'] + current['policy']['followup_hours'] * 3600:
                continue
            kind, option = event['kind'], event['option_id']
            if kind == 'visible':
                visible.update(event['visible'])
            elif kind == 'choice' and option in visible:
                chosen.add(option)
            elif kind == 'start' and option in chosen:
                started.add(option)
            elif kind == 'close':
                closed = bool(visible)
            elif kind == 'experience' and option in started:
                details = event.get('details') or {}
                position, duration = details.get('position'), details.get('duration')
                if (details.get('kind') in ('track_exit', 'ended', 'bye')
                        and all(isinstance(value, (int, float)) and math.isfinite(value)
                                for value in (position, duration)) and position >= 0):
                    outcome = classify_exit(position, duration)
                    if outcome == 'completion':
                        endings.append(1.)
                    elif outcome in ('skip', 'bounce') and details['kind'] == 'track_exit':
                        endings.append(0.)
        if not visible:
            continue
        row['visible'] += 1
        row['choices'] += bool(chosen)
        row['starts'] += bool(started)
        # All arms receive the full declared window. Observation loss stays
        # censored, including starts without a known ending and closed tabs.
        if now < offer['at'] + current['policy']['followup_hours'] * 3600:
            row['pending'] += 1
            continue
        row['mature'] += 1
        block = (arm, assigned['unit'], format, offer['episode_id'])
        source_ids.append(offer['id'])
        if endings:
            value = max(endings)
        elif not chosen and closed:
            value = 0.
        else:
            row['unknown'] += 1
            bounds[block].append((0., 1.))
            continue
        bounds[block].append((value, value))
        row['observed'] += 1
        row['qualified'] += int(value > 0)
        # Collapse correlated decisions in one episode before averaging the
        # randomized day. Each format also retains a separate readout.
        blocks[block].append(value)
    days = defaultdict(list)
    for (arm, day, format, episode), values in blocks.items():
        days[(arm, day, format)].append(max(values))
    samples = defaultdict(list)
    combined = defaultdict(list)
    for (arm, day, format), values in days.items():
        mean = sum(values) / len(values)
        samples[(format, arm)].append(mean)
        combined[(arm, day)].append(mean)
    overall = {arm: [sum(values) / len(values) for (a, day), values in combined.items() if a == arm]
               for arm in ('incumbent', 'challenger')}
    # Observation coverage alone cannot rule out selective missingness. Keep
    # unknown outcomes unknown, and also bound the decision if every missing
    # challenger outcome failed and every missing incumbent outcome succeeded.
    bounded_days = defaultdict(list)
    for (arm, day, format, episode), values in bounds.items():
        bounded_days[(arm, day, format)].append(tuple(max(v[i] for v in values) for i in (0, 1)))
    bounded_samples, bounded_combined = defaultdict(list), defaultdict(list)
    for (arm, day, format), values in bounded_days.items():
        mean = tuple(sum(v[i] for v in values) / len(values) for i in (0, 1))
        bounded_samples[(format, arm)].append(mean)
        bounded_combined[(arm, day)].append(mean)
    bounded_overall = {arm: [tuple(sum(v[i] for v in values) / len(values) for i in (0, 1))
        for (a, day), values in bounded_combined.items() if a == arm] for arm in ('incumbent', 'challenger')}
    alpha = current['alpha']
    def sensitivity(samples, level):
        old, new = samples['incumbent'], samples['challenger']
        worst = _interval([v[1] for v in old], [v[0] for v in new], level)
        best = _interval([v[0] for v in old], [v[1] for v in new], level)
        return {'difference_bounds': [worst['difference'], best['difference']],
                'interval': [worst['interval'][0], best['interval'][1]] if worst['interval'] else None}
    result = {'at': now, 'source_ids': sorted(source_ids), 'formats': {},
              'alpha': alpha, 'unit': 'randomized UTC day', 'primary': 'visible choice with observed qualified listening',
              'overall': {**_interval(overall['incumbent'], overall['challenger'], alpha),
                          'missing_outcome_bounds': sensitivity(bounded_overall, alpha),
                          'samples': overall,
                          'days': {arm: len(values) for arm, values in overall.items()}}}
    for format in FORMATS:
        result['formats'][format] = {'arms': {arm: dict(counts[(format, arm)])
            for arm in ('incumbent', 'challenger')},
            **_interval(samples[(format, 'incumbent')], samples[(format, 'challenger')], alpha / len(FORMATS)),
            'missing_outcome_bounds': sensitivity({arm: bounded_samples[(format, arm)]
                for arm in ('incumbent', 'challenger')}, alpha / len(FORMATS)),
            'samples': {arm: samples[(format, arm)] for arm in ('incumbent', 'challenger')},
            'days': {arm: len(samples[(format, arm)]) for arm in ('incumbent', 'challenger')}}
    return result


def decision(current, report, now):
    """A fixed-horizon decision with explicit abstention and format guardrails."""
    policy, overall = current['policy'], report['overall']
    if now < current['decision_at']:
        return 'observing', 'Collecting outcomes until the declared decision date.'
    if any(n < policy['min_days'] for n in overall['days'].values()):
        return 'retained', 'Too few independent observed days; current model retained.'
    for format, row in report['formats'].items():
        arms = row['arms']
        mature = sum(arm.get('mature', 0) for arm in arms.values())
        if not mature:
            continue
        for arm in arms.values():
            if (arm.get('mature', 0) and arm.get('observed', 0) / arm['mature'] < policy['min_coverage']):
                return 'retained', f'{format.title()} observation coverage was too low.'
        if any(n < policy['min_format_days'] for n in row['days'].values()):
            return 'retained', f'{format.title()} needs comparable observed days in both setups.'
        interval = row['missing_outcome_bounds']['interval']
        if interval is None or interval[0] < -policy['format_margin']:
            return 'retained', f'A {format} regression could not be excluded.'
    for arm in ('incumbent', 'challenger'):
        observed = sum(row['arms'][arm].get('observed', 0) for row in report['formats'].values())
        if observed < policy['min_observed']:
            return 'retained', 'Too few attributable outcomes; current model retained.'
    bounded = overall['missing_outcome_bounds']
    if (bounded['interval'] and bounded['interval'][0] > 0
            and bounded['difference_bounds'][0] >= policy['useful_effect']):
        return 'applied', 'Controlled choice and listening outcomes supported the challenger.'
    return 'retained', 'No supported useful improvement; current model retained.'
