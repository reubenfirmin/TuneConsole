"""Annotation-only, noncausal listener diagnostics for #638."""
from collections import Counter, defaultdict
import csv
import io
import re
import zipfile

import numpy as np
from scipy.special import expit, logit

from ..datasets.dataset_leads import questionnaire_sheets
from ..training.external_training import folds

SEED = 63830
ACTIVITIES = ['daily_routines', 'emotional', 'entertainment', 'intellectual', 'live_music',
              'music_listening', 'on_the_move', 'physical', 'social']
METHODS = ('track', 'generic_listener', 'specific_listener')


def cal_responses(rows, base, ref):
    records = []
    for i, row in enumerate(rows):
        for path in sorted((base / 'annotations').glob(row['id'].removeprefix('cal500:') + '_[0-9][0-9].txt')):
            p = dict(re.findall(r'^([^=]+?) = "([^"]*)"', ref(path).read_text(), re.M))
            records.append({'recording': i, 'reading': int(p.get('Usage-Reading') == 'Yes'),
                'studying': int(p.get('Usage-Studying') == 'Yes'), 'like': p.get('Song-Like'),
                'heard': p.get('Song-Heard_Before'), 'uid': p.get('uid')})
    return records


def moods_input(archive):
    with zipfile.ZipFile(archive) as z:
        def table(name):
            return list(csv.DictReader(io.StringIO(z.read('moods_and_activities_in_music/' + name).decode('utf-8-sig'))))
        raw, tracks = table('tags.csv'), {r['trackid']: r for r in table('tracks.csv')}
    pairs = defaultdict(list)
    for row in raw:
        values = [row['activity.' + k] for k in ACTIVITIES]
        if any(v not in ('0', '1') for v in values):
            raise ValueError('Missing/unknown activity judgment')
        pairs[(row['userid'], row['trackid'])].append(np.array(values, dtype=float))
    identities = sorted(pairs)
    users = sorted({p[0] for p in identities})
    trackids = sorted({p[1] for p in identities})
    artists = sorted({tracks[t]['artistid'] for t in trackids})
    if not all(artists):
        raise ValueError('Missing artist identity')
    ui, ti, ai = ({k: i for i, k in enumerate(v)} for v in (users, trackids, artists))
    d = {'user': np.array([ui[u] for u, t in identities]), 'track': np.array([ti[t] for u, t in identities]),
         'artist': np.array([ai[tracks[t]['artistid']] for u, t in identities]),
         'activities': np.array([np.mean(pairs[p], axis=0) for p in identities]),
         'response_count': np.array([len(pairs[p]) for p in identities])}
    for name, key in (('artist', 'artist'), ('listener', 'user')):
        d['fold_' + name] = folds(d[key], count=5, seed=SEED)
    d['fold_pair'] = folds(np.arange(len(identities)), count=5, seed=SEED)
    audit = {'raw_rows': len(raw), 'unique_pairs': len(pairs), 'repeat_rows': len(raw) - len(pairs),
             'users': len(users), 'tracks': len(trackids), 'artists': len(artists),
             'activity_order': ACTIVITIES, 'intellectual_positive_raw': sum(int(r['activity.intellectual']) for r in raw),
             'intellectual_fractional_pairs': int(np.count_nonzero((d['activities'][:, 3] > 0) & (d['activities'][:, 3] < 1))),
             'meaning': 'Listened-example intellectual endorsement; broader than Reading/Studying, no verified audio join.'}
    return d, audit


def helpstudy(path):
    sheets = questionnaire_sheets(path)
    values = next(iter(sheets.values()))
    header, *body = values
    rows = [dict(zip(header, r)) for r in body if any(str(v).strip() for v in r)]
    keys = ('StudyEffectPos_concentrate', 'StudyEffectNeg_concentrate', 'StudyEffectNeg_distract')
    known = [r for r in rows if all(str(r.get(k, '')).strip() for k in keys)]
    return {'episodes': len(rows), 'participants': len({r['ParticipantID'] for r in rows}),
            'episodes_by_participant_counts': sorted(Counter(r['ParticipantID'] for r in rows).values()),
            'fields': {k: dict(Counter(str(r.get(k, '')) for r in rows)) for k in ('Studying', 'StudyMusic', 'StudyTask', *keys)},
            'complete_outcomes': len(known), 'complete_participants': len({r['ParticipantID'] for r in known}),
            'complete_tasks': dict(Counter(str(r.get('StudyTask', '')) for r in known)),
            'decision': 'Support audit only; no fitted task/person interaction, no blank negatives, no song outcomes.'}


def associations(records, groups, draws=2000):
    rng = np.random.default_rng(SEED)
    result = {}
    for context in ('like', 'heard'):
        buckets = defaultdict(list)
        for r in records:
            if r[context] in ('Yes', 'No'):
                buckets[r['recording']].append(r)
        mixed = {i: rows for i, rows in buckets.items() if {r[context] for r in rows} == {'Yes', 'No'}}
        ids = list(mixed)
        labels = [np.array([r[context] == 'Yes' for r in mixed[i]]) for i in ids]
        outcomes = [np.array([[r['reading'], r['studying']] for r in mixed[i]]) for i in ids]
        diffs = np.array([y[l].mean(0) - y[~l].mean(0) for l, y in zip(labels, outcomes)])
        unique = sorted(set(groups[ids]))
        blocks = [np.flatnonzero(groups[ids] == g) for g in unique]
        observed = diffs.mean(0)
        boot, null = [], []
        for _ in range(draws):
            sample = np.concatenate([blocks[j] for j in rng.integers(len(blocks), size=len(blocks))])
            boot.append(diffs[sample].mean(0))
            shuffled = [rng.permutation(l) for l in labels]
            null.append(np.mean([y[l].mean(0) - y[~l].mean(0) for l, y in zip(shuffled, outcomes)], axis=0))
        for c, task in enumerate(('reading', 'studying')):
            result[context + '__' + task] = {'recordings': len(ids), 'components': len(blocks),
                'responses': sum(map(len, outcomes)), 'difference_yes_minus_no': float(observed[c]),
                'conditional_95_percent_interval': np.quantile(np.array(boot)[:, c], [.025, .975]).tolist(),
                'permutation_p': float((1 + np.count_nonzero(np.abs(np.array(null)[:, c]) >= abs(observed[c]))) / (draws + 1))}
    ordered = sorted(result, key=lambda k: result[k]['permutation_p'])
    previous = 0.
    for i, k in enumerate(ordered):
        previous = max(previous, min(1., (len(ordered) - i) * result[k]['permutation_p']))
        result[k]['holm_p'] = previous
    uids = Counter(r['uid'] for r in records if r['uid'])
    return {'comparisons': result, 'responses': len(records), 'uid_responses': sum(uids.values()),
            'unique_uids': len(uids), 'uid_response_counts': sorted(uids.values()),
            'context_counts': {k: dict(Counter(r[k] for r in records)) for k in ('like', 'heard')},
            'limit': 'Within-song observational associations; incomplete listener IDs leave unknown cross-song dependence.'}


def smooth(values, index, train, size, alpha):
    prior = float(values[train].mean())
    counts = np.bincount(index[train], minlength=size)
    sums = np.bincount(index[train], weights=values[train], minlength=size)
    return (sums + alpha * prior) / (counts + alpha), counts, prior


def probabilities(d, assignment, alpha=20, user=None):
    user = d['user'] if user is None else user
    y, generic = d['activities'][:, 3], np.delete(d['activities'], 3, axis=1).mean(1)
    nusers, ntracks = int(user.max()) + 1, int(d['track'].max()) + 1
    out, support = np.empty((len(y), 3)), np.empty(len(y), dtype=int)
    for fold in range(5):
        train, valid = assignment != fold, assignment == fold
        track, _, prior = smooth(y, d['track'], train, ntracks, alpha)
        specific, counts, _ = smooth(y, user, train, nusers, alpha)
        general, _, gp = smooth(generic, user, train, nusers, alpha)
        base = track[d['track'][valid]]
        logits = logit(np.clip(base, 1e-8, 1 - 1e-8))
        out[valid, 0] = base
        for c, estimate, center in ((1, general, gp), (2, specific, prior)):
            adjustment = logit(np.clip(estimate[user[valid]], 1e-8, 1 - 1e-8)) - logit(np.clip(center, 1e-8, 1 - 1e-8))
            out[valid, c] = expit(logits + adjustment)
        support[valid] = counts[user[valid]]
    return out, support


def error_report(y, prediction, support):
    result = {}
    for name, mask in (('all', np.ones(len(y), dtype=bool)), ('warm_ge5', support >= 5), ('cold_zero', support == 0)):
        result[name] = {'rows': int(mask.sum()), 'mse': dict(zip(METHODS, ((prediction[mask] - y[mask, None]) ** 2).mean(0).tolist())) if mask.any() else None}
    return result


def crossed_intervals(d, prediction, draws=1000):
    losses = (prediction - d['activities'][:, 3, None]) ** 2
    rng, samples = np.random.default_rng(SEED), []
    for _ in range(draws):
        weight = rng.poisson(1, int(d['user'].max()) + 1)[d['user']] * rng.poisson(1, int(d['artist'].max()) + 1)[d['artist']]
        samples.append(np.average(losses, axis=0, weights=weight))
    result = {}
    for a, b in ((1, 0), (2, 1), (2, 0)):
        result[METHODS[a] + '__vs__' + METHODS[b]] = {
            'mse_difference': float((losses[:, a] - losses[:, b]).mean()),
            'conditional_95_percent_interval': np.quantile(np.array(samples)[:, a] - np.array(samples)[:, b], [.025, .975]).tolist()}
    return result


def shuffled_control(d, observed, draws=199):
    rng = np.random.default_rng(SEED)
    order = np.argsort(d['track'], kind='stable')
    boundaries = np.flatnonzero(np.diff(d['track'][order])) + 1
    blocks = np.split(order, boundaries)
    y, null = d['activities'][:, 3], []
    for _ in range(draws):
        user = d['user'].copy()
        for block in blocks:
            user[block] = rng.permutation(user[block])
        pred, _ = probabilities(d, d['fold_artist'], user=user)
        loss = ((pred - y[:, None]) ** 2).mean(0)
        null.append([loss[0] - loss[1], loss[1] - loss[2]])
    loss = ((observed - y[:, None]) ** 2).mean(0)
    gain = np.array([loss[0] - loss[1], loss[1] - loss[2]])
    return {'draws': draws, 'contrasts': ['generic_vs_track', 'specific_vs_generic'], 'observed_mse_gain': gain.tolist(),
            'null_95_percent_interval': np.quantile(null, [.025, .975], axis=0).T.tolist(),
            'one_sided_p': ((1 + np.count_nonzero(np.asarray(null) >= gain, axis=0)) / (draws + 1)).tolist(),
            'null_gains': null}


def evaluate_moods(d):
    reports, arrays = {}, {}
    for scheme in ('artist', 'listener', 'pair'):
        reports[scheme] = {}
        for alpha in (20, 1, 5, 100):
            prediction, support = probabilities(d, d['fold_' + scheme], alpha)
            reports[scheme][str(alpha)] = error_report(d['activities'][:, 3], prediction, support)
            arrays[f'{scheme}_{alpha}'] = prediction
            arrays[f'{scheme}_support'] = support
    reports['crossed_intervals'] = crossed_intervals(d, arrays['artist_20'])
    reports['shuffled_control'] = shuffled_control(d, arrays['artist_20'])
    return reports, arrays
