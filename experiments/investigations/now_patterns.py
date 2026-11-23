"""Replay recurring NOW hypotheses on a frozen, read-only library."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import platform
from zoneinfo import ZoneInfo

import numpy as np

from yt_playlist.core.evidence_ledger import EvidenceLedger
from yt_playlist.rec import choice_attribution, group_viz, layers
from yt_playlist.repos import group_queries


PROTOCOL = {
    'hour_bin': 4, 'previous_hours': 48, 'entry_minutes': 45,
    'prior_days': 3, 'prior_sessions': 4, 'shrinkage': 12,
    'min_train': 20, 'recency_days': 60, 'holdout_fractions': [.50, .67, .83],
    'start_match_seconds': 120, 'attribution_hours': 6,
    'now_min_events': 3, 'today_half_hours': 4, 'today_gain': .25, 'now_gain': .6,
    'bootstrap_samples': 2000, 'seed': 1314,
}


def episodes(db, account, captured):
    plays = db.execute('''SELECT e.id,e.account_id,e.played_at at,e.identity_key,e.video_id,
        c.timezone, COALESCE(e.is_generated,0)=1 OR EXISTS(
            SELECT 1 FROM quarantined_playlists q JOIN playlist_scopes p ON p.id=q.playlist_scope_id
            WHERE p.provider_playlist_id=e.playlist_ytm_id AND (p.account_id IS NULL OR p.account_id=e.account_id)
        ) generated
        FROM play_event_subjects e LEFT JOIN play_event_calendar c ON c.event_id=e.id
        WHERE e.account_id=? AND EXISTS(SELECT 1 FROM play_event_sources s
          WHERE s.event_id=e.id AND s.source='live')''', (account,))
    playback = db.execute('''SELECT account_id,at,kind,payload FROM player_events
        WHERE account_id=? AND (kind='bye' OR (video_id IS NOT NULL AND kind IN ('tick','state')))''',
        (account,))
    history, active = {}, {}
    for event in sorted(map(dict, [*plays, *playback]), key=layers.playback_order):
        if event['at'] > captured:
            break
        previous = active.get(account)
        session = layers.advance_session(active, event)
        if not session:
            if event.get('kind') == 'bye' and previous and event['at'] < previous.expires_at:
                history[previous.episode]['closed'] = True
            continue
        item = history.setdefault(session.episode, {'start': session.started_at, 'plays': [], 'closed': False})
        item['end'] = session.last_at
        if 'identity_key' in event:
            item['plays'].append(event)
    return [item for item in history.values()
            if item['closed'] or item['end'] + layers.SESSION_IDLE_S <= captured]


def linked_choices(ledger, history, captured):
    offers = {row['id']: row for row in ledger.records('opportunity')
              if max(row['at'], row['observed_at']) <= captured}
    responses = choice_attribution.observed_responses(ledger.records('response'), captured)
    decisions = choice_attribution.actions(offers, responses, PROTOCOL['attribution_hours'] * 3600)
    by_video = defaultdict(list)
    for episode in history:
        for play in episode['plays']:
            if play['video_id']:
                by_video[play['video_id']].append((episode, play))

    linked, audit = [], Counter()
    for offer in offers.values():
        audit[f"{offer['format']}: issued"] += 1
        if offer.get('vibe'):
            audit['vibe: recorded on offer'] += 1
    for decision in decisions:
        offer, action = decision['offer'], decision['choice']
        surface = offer['format']
        audit[f'{surface}: deliberate'] += 1
        if decision['shown']:
            audit[f'{surface}: visible before choice'] += 1
        # An attributed audible start can corroborate the choice once. Prefer exact time
        # proximity and require unambiguous identity, even for old observations without account.
        matches = {}
        for event in decision['outcomes']:
            details = event['details']
            if event['kind'] != 'start' and (event['kind'] != 'experience' or details.get('kind')
                                              or details.get('continuation')):
                continue
            for episode, play in by_video[details.get('video_id')]:
                if (play['at'] >= action['at'] and abs(play['at'] - event['at']) <= PROTOCOL['start_match_seconds']
                        and details.get('account_id', play['account_id']) == play['account_id']):
                    matches[play['id']] = episode, play
        if len(matches) == 1:
            episode, play = next(iter(matches.values()))
            audit[f'{surface}: linked to dated playback'] += 1
            if decision['shown'] and offer.get('vibe'):
                audit['vibe: visible choice with linked playback'] += 1
            linked.append((episode, play, decision))
        elif matches:
            audit[f'{surface}: ambiguous playback'] += 1
    visible = defaultdict(set)
    for row in responses:
        if row['kind'] == 'visible':
            visible[row['opportunity_id']].update(row['visible'])
    audit['offers with visibility'] = len(visible)
    audit['distinct linked sessions'] = len({ep['start'] for ep, _, _ in linked})
    return linked, dict(audit)


def group_data(db, generation):
    rows = db.execute('''SELECT x.external_version,t.external_key,p.external_key FROM generation_groups x
        JOIN group_generations g ON g.id=x.generation_id JOIN group_versions v ON v.id=x.group_version_id
        JOIN taste_groups t ON t.id=x.group_id LEFT JOIN taste_groups p ON p.id=v.parent_group_id
        WHERE g.external_key=? AND v.legacy=0''', (generation,)).fetchall()
    by_version = {version: group for version, group, _ in rows}
    parents = {group: parent for _, group, parent in rows if parent}
    membership = {key: by_version[version] for key, version in group_queries.leaf_members(db, generation).items()
                  if version in by_version}
    return membership, parents


def local_time(at, zone):
    return datetime.fromtimestamp(at, ZoneInfo(zone))


def branch_group(group, parents):
    branch = group
    while group in parents:
        branch = group
        group = parents[group]
    return branch


def replay_rows(history, membership, context_groups, linked, confirmed_zones):
    attributed = {play['id'] for _, play, _ in linked}
    today, previous, rows = {}, None, defaultdict(list)
    coverage = Counter()
    for episode in history:
        if not episode['plays']:
            continue
        coverage['sessions with play'] += 1
        first = episode['plays'][0]
        zone = first['timezone']
        if zone not in confirmed_zones:
            coverage['unconfirmed timezone'] += 1
            continue
        at = episode['start']
        local = local_time(at, zone)
        base = {'start': at, 'hour': local.hour // PROTOCOL['hour_bin'],
                'weekend': local.weekday() >= 5, 'day': local.date().isoformat()}
        prev = (previous[1] if previous and at - previous[0] <= PROTOCOL['previous_hours'] * 3600
                else None)
        current, clean, start_group = {}, True, None
        for play in episode['plays']:
            key, ts = play['identity_key'], play['at']
            if play['generated'] or play['id'] in attributed:
                clean = False
                continue
            day = local_time(ts, zone).date()
            same_day = today.setdefault(day, {})
            if clean and key not in current and ts - at <= 60 * PROTOCOL['entry_minutes']:
                if key in membership:
                    prefix = len(current.keys() & membership.keys())
                    when = at if not current else max(current.values())
                    day_prefix = {k: t for k, t in same_day.items()
                                  if t <= when and (current or t < when)}
                    eligible = (not current and prefix == 0) or (prefix in (1, 3) and ts > when)
                    if local_time(when, zone).date() == day and eligible:
                        ordinary, _, _ = group_viz.reading(when, current, membership, PROTOCOL['now_min_events'])
                        if ordinary:
                            gain = PROTOCOL['now_gain']
                        else:
                            ordinary, _, _ = group_viz.reading(when, day_prefix, membership,
                                PROTOCOL['now_min_events'], half=PROTOCOL['today_half_hours'])
                            gain = PROTOCOL['today_gain']
                        first_group = next((membership[k] for k in current if k in membership), None)
                        rows[prefix].append({**base, 'at': when, 'target_at': ts, 'target': membership[key],
                            'previous': prev, 'prefix': context_groups.get(first_group),
                            'ordinary': ordinary, 'gain': gain})
                        if prefix == 0:
                            start_group = membership[key]
                            coverage['classified clean starts'] += 1
                            if prev is not None:
                                coverage['classified starts with previous clean session'] += 1
                elif not current:
                    coverage['unclassified clean first play'] += 1
                current[key] = ts
            same_day[key] = ts
        if start_group:
            previous = episode['end'], context_groups[start_group]
        if first['generated'] or first['id'] in attributed:
            coverage['generated or linked first play'] += 1
    return rows, dict(coverage)


def fit(train, fields, evaluation_at, target_index):
    grouped = defaultdict(list)
    for row in train:
        if all(row.get(field) is not None for field in fields):
            grouped[tuple(row[field] for field in fields)].append(row)
    outputs = {}
    for key, rows in grouped.items():
        if fields and (len(rows) < PROTOCOL['prior_sessions']
                       or len({row['day'] for row in rows}) < PROTOCOL['prior_days']):
            continue
        counts = np.zeros(len(target_index))
        for row in rows:
            age = (evaluation_at - row['target_at']) / 86400
            counts[target_index[row['target']]] += 2 ** (-age / PROTOCOL['recency_days'])
        outputs[key] = counts
    return outputs


def project(row, fields, fitted, backoff):
    key = tuple(row.get(field) for field in fields)
    counts = fitted[fields].get(key)
    if counts is None:
        return backoff, False
    strength = PROTOCOL['shrinkage']
    return (counts + strength * backoff) / (counts.sum() + strength), True


def evaluate(train, test, universe, *, conditional=False):
    if len(train) < PROTOCOL['min_train'] or not test:
        return [], {'train': len(train), 'test': len(test), 'status': 'insufficient replay support'}
    targets = {group: index for index, group in enumerate(sorted(universe))}
    fields = [('hour',), ('hour', 'weekend'), ('previous',), ('hour', 'previous'),
              ('hour', 'weekend', 'previous'), ('prefix',), ('hour', 'prefix')]
    if conditional:
        fields = [('hour',), ('hour', 'weekend'), ('surface',), ('hour', 'surface'), ('hour', 'vibe')]
    fit_at = min(row['at'] for row in test)
    fitted = {field: fit(train, field, fit_at, targets) for field in [(), *fields]}
    uniform = np.full(len(targets), 1 / len(targets))
    marginal, _ = project({}, (), fitted, uniform)
    result = []
    for row in test:
        hour, hour_known = project(row, ('hour',), fitted, marginal)
        calendar, calendar_known = project(row, ('hour', 'weekend'), fitted, hour)
        variants = {'frequency': (marginal, True), 'hour': (hour, hour_known),
                    'hour+weekend': (calendar, calendar_known)}
        for field in fields[2:]:
            parent = calendar if field == ('hour', 'weekend', 'previous') else hour if 'hour' in field else marginal
            variants['+'.join(field)] = project(row, field, fitted, parent)
        if conditional:
            mask = np.array([targets[group] for group in row['visible']])
            variants['visible_uniform'] = (uniform, True)
            ordinary_weight = 1
        else:
            mask = None
            ordinary_weight = np.ones(len(targets))
            for group, weight in row['ordinary'].items():
                ordinary_weight[targets[group]] += row['gain'] * weight
            variants['ordinary'] = (marginal, bool(row['ordinary']))
        target = targets[row['target']]
        losses, brier, supported = {}, {}, {}
        for name, (mass, found) in variants.items():
            # The incumbent replay uses its exact NOW/TODAY group multipliers on a
            # common training-frequency baseline. Visible menus use a uniform floor.
            mass = mass * ordinary_weight
            available = mass if mask is None else mass[mask]
            total = available.sum()
            probability = mass[target] / total
            losses[name] = -math.log(probability)
            brier[name] = float((available @ available) / total**2 - 2 * probability + 1)
            supported[name] = found
        result.append({'day': row['day'], 'loss': losses, 'brier': brier, 'support': supported})
    return result, {'train': len(train), 'test': len(test), 'status': 'scored'}


def summary(rows, benchmark='ordinary'):
    if not rows:
        return {}
    by_week = defaultdict(list)
    for index, row in enumerate(rows):
        by_week[datetime.fromisoformat(row['day']).date().toordinal() // 7].append(index)
    rng = np.random.default_rng(PROTOCOL['seed'])
    weeks = list(by_week.values())
    sampled = rng.integers(len(weeks), size=(PROTOCOL['bootstrap_samples'], len(weeks)))
    report = {}
    for name in rows[0]['loss']:
        loss = np.array([row['loss'][name] for row in rows])
        delta = loss - [row['loss'][benchmark] for row in rows]
        week_sum = np.array([delta[indices].sum() for indices in weeks])
        week_n = np.array([len(indices) for indices in weeks])
        replicates = week_sum[sampled].sum(axis=1) / week_n[sampled].sum(axis=1)
        report[name] = {'log_loss': round(float(loss.mean()), 4), 'delta': round(float(delta.mean()), 4),
                        'brier': round(float(np.mean([row['brier'][name] for row in rows])), 4),
                        'ci95': [round(float(n), 4) for n in np.quantile(replicates, [.025, .975])]
                        if len(weeks) >= 4 else None,
                        'supported': sum(row['support'][name] for row in rows)}
        common = np.array([row['support'][name] and row['support']['hour'] for row in rows])
        if common.any():
            hour_loss = np.array([row['loss']['hour'] for row in rows])
            report[name]['common_with_hour'] = int(common.sum())
            report[name]['delta_to_hour_on_common'] = round(float((loss - hour_loss)[common].mean()), 4)
    return {'sessions': len(rows), 'days': len({row['day'] for row in rows}), 'week_blocks': len(weeks),
            'scores': report}


def split(rows, begins, ends):
    train = [row for row in rows if max(row['target_at'], row['start']) < begins]
    test = [row for row in rows if begins <= row['start'] < ends and row['target_at'] < ends]
    return train, test


def retrospective(rows, universe, *, conditional=False):
    first_by_day = {}
    for row in sorted(rows, key=lambda row: row['start']):
        first_by_day.setdefault(row['day'], row['start'])
    days = list(first_by_day.values())
    benchmark = 'visible_uniform' if conditional else 'ordinary'
    edges = sorted({days[int(len(days) * fraction)] for fraction in PROTOCOL['holdout_fractions']}) if days else []
    predictions, folds = [], []
    for begin, end in zip(edges, [*edges[1:], float('inf')]):
        train, test = split(rows, begin, end)
        scored, counts = evaluate(train, test, universe, conditional=conditional)
        folds.append({**counts, 'from_day': next(row['day'] for row in rows if row['start'] == begin),
                      'scores': summary(scored, benchmark)})
        predictions.extend(scored)
    return {'folds': folds, 'held_out': summary(predictions, benchmark)}


def choice_rows(linked, membership, responses, zones, *, project_group=None):
    groups = set(membership.values())
    visible = defaultdict(list)
    for row in responses:
        if row['kind'] == 'visible':
            visible[row['opportunity_id']].append(row)
    rows, seen, exclusions = [], set(), Counter()
    for episode, play, decision in sorted(linked, key=lambda item: item[2]['choice']['at']):
        offer, action = decision['offer'], decision['choice']
        at = max(offer['at'], offer['observed_at'])
        zone = play['timezone']
        if not decision['shown'] or episode['start'] in seen or zone not in zones:
            exclusions['no verified visibility, duplicate session or unknown calendar'] += 1
            continue
        if offer.get('context_at', at) > at or at > action['at']:
            exclusions['future prepared context'] += 1
            continue
        shown = {option for row in visible[offer['id']] if max(row['at'], row['observed_at']) <= action['at']
                 for option in row['visible']}
        options = {}
        for option in offer['options']:
            identity = str(option['id'])
            if identity not in shown:
                continue
            keys = [*(option.get('keys') or ()),
                    *(track.get('key') for track in option.get('tracks') or () if isinstance(track, dict))]
            mapped = {membership[key] for key in keys if key in membership}
            group = option.get('group_id')
            if project_group is not None:
                group = project_group.get(group)
            if group in groups:
                options[identity] = group
            elif len(mapped) == 1:
                options[identity] = next(iter(mapped))
        target = membership.get(play['identity_key'])
        if options.keys() != shown:
            exclusions['unresolved visible alternatives'] += 1
            continue
        if len(set(options.values())) < 2:
            exclusions['fewer than two visible groups'] += 1
            continue
        if options.get(action['option_id']) != target:
            exclusions['played group does not confirm selected group'] += 1
            continue
        local = local_time(at, zone)
        rows.append({'start': at, 'at': at, 'day': local.date().isoformat(),
                     'target_at': max(play['at'], action['observed_at']), 'target': target,
                     'hour': local.hour // PROTOCOL['hour_bin'], 'weekend': local.weekday() >= 5,
                     'visible': sorted(set(options.values())), 'surface': offer['format'], 'vibe': offer.get('vibe')})
        seen.add(episode['start'])
    return rows, dict(exclusions)


def choice_result(linked, membership, responses, zones, project_group=None):
    cohort, exclusions = choice_rows(linked, membership, responses, zones, project_group=project_group)
    return {'eligible': len(cohort), 'by_surface': dict(Counter(r['surface'] for r in cohort)),
            'exclusions': exclusions, 'results': retrospective(cohort, set(membership.values()), conditional=True)}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + '\n')


def main():
    import sqlite3

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--captured', required=True, type=float)
    parser.add_argument('--database', default='/library/state.db', type=Path)
    parser.add_argument('--output', default='/reports', type=Path)
    parser.add_argument('--hour-bin', type=int, choices=(4, 6), default=PROTOCOL['hour_bin'])
    args = parser.parse_args()
    PROTOCOL['hour_bin'] = args.hour_bin
    with closing(sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        accounts = db.execute('''SELECT p.account_id,count(*) n FROM play_events p
            JOIN play_event_sources s ON s.event_id=p.id AND s.source='live'
            GROUP BY p.account_id ORDER BY n DESC,p.account_id''').fetchall()
        account = accounts[0]['account_id']
        zones = {row[0] for row in db.execute('SELECT timezone FROM listening_calendars WHERE account_id=? AND confirmed=1', (account,))}
        generations = list(db.execute('''SELECT g.external_key,p.published_at
            FROM group_generations g JOIN group_publications p ON p.generation_id=g.id ORDER BY p.published_at'''))
        head = db.execute('SELECT g.external_key FROM group_head h JOIN group_generations g ON g.id=h.generation_id').fetchone()[0]
        ledger = EvidenceLedger(args.database.with_name(args.database.name + '.evidence'), 0)
        history = episodes(db, account, args.captured)
        linked, audit = linked_choices(ledger, history, args.captured)
        responses = choice_attribution.observed_responses(ledger.records('response'), args.captured)

        source = Path(__file__).read_bytes()
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'source.py').write_bytes(source)
        dependencies = {module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                        for module in (choice_attribution, group_queries, group_viz, layers)}
        write_json(args.output / 'protocol.json', {**PROTOCOL, 'baseline': args.baseline, 'captured': args.captured,
                   'head': head, 'publications': [list(row) for row in generations],
                   'source_sha256': hashlib.sha256(source).hexdigest(), 'dependencies': dependencies,
                   'python': platform.python_version(), 'numpy': np.__version__})
        result = {'live_plays_by_account': [row['n'] for row in accounts], 'confirmed_zones': sorted(zones),
                  'sessions_reconstructed': len(history), 'choice_audit': audit,
                  'mood_events': db.execute('SELECT count(*) FROM mood_events').fetchone()[0]}
        strict = []
        for i, (generation, published_at) in enumerate(generations):
            members, parents = group_data(db, generation)
            if not members:
                continue
            branches = {group: branch_group(group, parents) for group in set(members.values())}
            labels, coverage = replay_rows(history, members, branches, linked, zones)
            universe = set(members.values())
            if generation == head:
                result['head_coverage'] = coverage
                result['retrospective'] = {prefix: retrospective(rows, universe) for prefix, rows in labels.items()}
                broad_members = {key: branches[group] for key, group in members.items()}
                broad_groups = {group: group for group in branches.values()}
                broad, _ = replay_rows(history, broad_members, broad_groups, linked, zones)
                result['group_counts'] = {'leaves': len(universe), 'branches': len(broad_groups)}
                result['broad_retrospective'] = {prefix: retrospective(rows, broad_groups)
                                                 for prefix, rows in broad.items()}
                result['choice_replay'] = choice_result(linked, members, responses, zones)
                result['broad_choice_replay'] = choice_result(linked, broad_members, responses, zones,
                    {group: branch_group(group, parents) for group in set(parents) | set(branches.values())})
            end = generations[i + 1][1] if i + 1 < len(generations) else args.captured
            fold = {'generation': generation, 'published_at': published_at, 'end': end, 'stages': {}}
            for prefix, rows in labels.items():
                train, test = split(rows, published_at, end)
                scored, counts = evaluate(train, test, universe)
                fold['stages'][prefix] = {**counts, 'scores': summary(scored)}
            strict.append(fold)
        result['point_in_time'] = strict
        write_json(args.output / 'results.json', result)
        print(json.dumps({'head_coverage': result.get('head_coverage'), 'choice_audit': audit,
                          'choice_replay': {k: v for k, v in result.get('choice_replay', {}).items() if k != 'results'},
                          'retrospective': {str(k): v['held_out'].get('sessions', 0)
                                            for k, v in result.get('retrospective', {}).items()}}, indent=2))


if __name__ == '__main__':
    main()
