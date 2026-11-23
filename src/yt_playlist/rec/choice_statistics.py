"""Compile and replay detached listening observations without application acquisition."""
import hashlib

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core import mix_choices
from yt_playlist.core.choice_capture_contract import FORMATS
from yt_playlist.rec import choice_attribution, choice_summary, home_format_rules, permanent_taste
from yt_playlist.rec.choice_learning import interest_key

INTEREST_POLICY = 'listener-layer-contexts-v6'


def compile_facts(all_offers, all_responses, now, *, halflife, attribution, album_ids,
                  timezone=None, calendars=None, playback_episodes=None, listener_support=None):
    offers = {row['id']: row for row in all_offers if row['observed_at'] <= now and row['at'] <= now}
    responses = sorted((r for r in all_responses if r['observed_at'] <= now and r['at'] <= now),
                       key=lambda r: (r['at'], r['kind'] != 'visible', r['id']))
    eligible = choice_attribution.observed_responses(responses, now)
    rows = {format: dict(offered=0, visible=0, opens=0, choices=0, predicted_choices=0,
        top_choice_hits=0, reciprocal_rank=0., starts=0, saves=0, returns=0, promotions=0,
        experience=0, independent_episodes=0) for format in FORMATS}
    seen, episodes, stages = {}, {f: set() for f in FORMATS}, set()
    for offer in offers.values():
        rows[offer['format']]['offered'] += 1
    for event in eligible:
        check_cancellable_work()
        offer = offers.get(event['opportunity_id'])
        if not offer or event['at'] < offer['at']:
            continue
        row, oid = rows[offer['format']], offer['id']
        if event['kind'] == 'visible':
            if oid not in seen:
                row['visible'] += 1
            seen.setdefault(oid, set()).update(event['visible'])
        elif event['kind'] == 'choice':
            row['choices'] += 1
            episodes[offer['format']].add(offer['episode_id'])
            prediction = [identity for identity in offer.get('prediction') or [] if identity in seen.get(oid, set())]
            if event['option_id'] in prediction:
                row['predicted_choices'] += 1
                rank = prediction.index(event['option_id']) + 1
                row['top_choice_hits'] += int(rank == 1)
                row['reciprocal_rank'] += 1 / rank
        elif event['kind'] in {'open', 'start', 'save', 'promote', 'return', 'experience'}:
            key = oid, event['kind'], event['option_id']
            if key not in stages:
                field = {'open': 'opens', 'start': 'starts', 'save': 'saves',
                         'promote': 'promotions', 'return': 'returns', 'experience': 'experience'}
                row[field[event['kind']]] += 1
                stages.add(key)
    actions = choice_attribution.actions(offers, eligible, attribution)
    provenance = [choice_attribution.provenance(action) for action in actions]
    calendars = calendars or {}
    choice_days = {action['choice']['id']: choice_attribution.calendar_day(
        action['choice']['at'], facts, timezone, calendars) for action, facts in zip(actions, provenance)}
    outcomes = choice_summary.summarize(actions, FORMATS, choice_days)
    for format, row in rows.items():
        row.update(outcomes[format])
        row['returns'] += row['return_days']
        row['independent_episodes'] = len(episodes[format])
        row['top_choice_rate'] = row['top_choice_hits'] / row['predicted_choices'] if row['predicted_choices'] else None
        row['mean_reciprocal_rank'] = row['reciprocal_rank'] / row['predicted_choices'] if row['predicted_choices'] else None
    watermark = hashlib.sha256(canonical([sorted(offers), [r['id'] for r in responses]])).hexdigest()
    report = dict(schema=1, at=now, watermark=watermark, formats=rows, status='observational',
        promotion_supported=False, source_opportunities=len(offers), source_responses=len(responses),
        home_format=home_format_rules.compile_usage(offers, eligible, now, halflife))
    moments = [(row.get('choice_account_id'), row.get('choice_start_at')) for row in provenance]
    playback = playback_episodes({(account, at) for account, at in moments
                                 if account is not None and at is not None}) if playback_episodes else {}
    dated = [(action, facts, playback.get(moment, action['offer']['episode_id']))
             for action, facts, moment in zip(actions, provenance, moments)]
    compiled = _interests(dated, album_ids, timezone, calendars)
    if listener_support is not None:
        listener_support.update(_facet_support(dated, timezone, calendars))
    contexts = sorted(compiled, key=lambda row: -row['at'])[:128]
    projection = dict(schema=3, policy=INTEREST_POLICY, at=now, watermark=watermark,
                      contexts_total=len(compiled), contexts=contexts)
    album_days = choice_summary.attributed_album_track_days(actions, choice_days, album_ids=album_ids)
    projection['album_track_days'] = {r['option_id']: album_days[r['option_id']]
        for r in contexts if r['format'] == 'albums' and r['option_id'] in album_days}
    projection['interest_version'] = hashlib.sha256(canonical([
        INTEREST_POLICY, contexts, projection['album_track_days']])).hexdigest()
    return report, projection, offers, eligible


def _interests(decisions, album_ids, timezone, calendars):
    groups, episode_groups = {}, {}
    for decision, provenance, episode in decisions:
        offer, option, event = (decision[key] for key in ('offer', 'option', 'choice'))
        format = offer['format']
        details = event.get('details') or {}
        tracks = details.get('tracks', option.get('tracks')) or []
        keys = (details['keys'] if 'keys' in details else
                option.get('keys') if 'tracks' not in details and option.get('keys') else
                [track['key'] for track in tracks if track.get('key')])
        option_id = album_ids[event['option_id']] if format == 'albums' else event['option_id']
        interest = dict(format=format, option_id=option_id, mode_id=option.get('mode_id'),
            intent=option.get('intent'), keys=sorted(set(keys))[:100], feature=option.get('feature'),
            basis=option.get('basis'), at=event['at'], episode=offer['episode_id'], opportunity_id=offer['id'],
            **provenance)
        if format == 'mixes':
            interest.update(keys=[], feature=None, mode_id=None, basis=mix_choices.PRECISION,
                descriptor_id=option.get('descriptor_id') if option.get('genres_complete') else None)
        identity = interest_key(interest)
        group = groups.setdefault(identity, {'episodes': {}, 'units': {}})
        amount = choice_attribution.strength(decision)
        prior, strength = group['episodes'].get(episode, (interest, 0.))
        if episode != interest['episode']:
            prior = interest
        if 'choice_start_at' not in prior and 'choice_start_at' in interest:
            prior = {**prior, 'choice_start_at': interest['choice_start_at']}
            if 'choice_account_id' not in prior and 'choice_account_id' in interest:
                prior['choice_account_id'] = interest['choice_account_id']
        group['episodes'][episode] = prior, max(strength, amount)
        day = choice_attribution.calendar_day(prior['at'], prior, timezone, calendars)
        permanent_taste.add_support(group['units'], day, episode, amount)
        episode_groups.setdefault(episode, set()).add(identity)
    rows = []
    for group in groups.values():
        episodes = group['episodes']
        latest_episode = max(episodes, key=lambda key: episodes[key][0]['at'])
        latest, strength = episodes[latest_episode]
        rows.append({**latest, 'days': len(group['units']), 'episodes': len(episodes),
            'sustained': permanent_taste.reading(group['units'])['weight'] > 1.,
            'episode_support': strength / len(episode_groups[latest_episode])})
    return rows


def _facet_support(decisions, timezone, calendars):
    mixes, tracks = {}, {}
    for decision, provenance, episode in decisions:
        offer, option, event = (decision[key] for key in ('offer', 'option', 'choice'))
        day = choice_attribution.calendar_day(event['at'], provenance, timezone, calendars)
        strength = choice_attribution.strength(decision)
        if (descriptor := option.get('descriptor_id')) and offer['format'] == 'mixes' and option.get('genres_complete'):
            permanent_taste.add_support(mixes.setdefault(str(descriptor), {}), day, episode, strength)
            continue
        # A track needs a deliberate target and its own verified onset.
        details = event.get('details') or {}
        lineup = details.get('tracks', option.get('tracks')) or []
        if not lineup:
            continue
        video = (details.get('video_id') if offer['format'] == 'radio' else None) or lineup[0].get('video_id')
        keys = {track['key'] for track in lineup if track.get('video_id') == video and track.get('key')}
        if video and len(keys) == 1 and any(
                outcome['kind'] == 'start' and outcome['details'].get('video_id') == video
                and not outcome['details'].get('continuation')
                for outcome in decision['outcomes']):
            permanent_taste.add_support(tracks.setdefault(keys.pop(), {}), day, episode, strength)
    version = hashlib.sha256(canonical([INTEREST_POLICY, mixes, tracks])).hexdigest()
    return {'version': version, 'mixes': mixes, 'tracks': tracks}


def replay_facts(offers, events, captured, now, predictor=None):
    withdrawn = {e['details'].get('action_id') for e in events if e['kind'] == 'withdraw'}
    forecasts, visible, choices, episodes = {}, {}, set(), {}
    counts = {f: dict(choices=0, scored=0, hits=0, unseen=0, unscored=0, future_context=0,
        missing_context=0, released_context=0, unsupported_precision=0) for f in FORMATS}
    timeline = [(offer['observed_at'], 0, oid, offer) for oid, offer in offers.items()]
    timeline.extend((e['observed_at'], 1, e['id'], e) for e in events)
    for _at, phase, _identity, event in sorted(timeline):
        check_cancellable_work()
        if phase == 0:
            offer, oid = event, event['id']
            context, options, reason = captured[oid]
            forecasts[oid] = ((predictor.predict(context, options) if predictor else offer['prediction'])
                             if reason is None else None)
            if reason is not None:
                counts[offer['format']][reason] += 1
            continue
        if event['id'] in withdrawn or event['kind'] == 'withdraw':
            continue
        offer = offers.get(event['opportunity_id'])
        if not offer or event['at'] < offer['at']:
            continue
        oid = offer['id']
        if event['kind'] == 'visible':
            visible.setdefault(oid, set()).update(event['visible'])
        elif event['kind'] == 'choice':
            choices.add(event['id'])
            row = counts[offer['format']]
            row['choices'] += 1
            prediction = forecasts[oid]
            if event['option_id'] not in visible.get(oid, set()):
                row['unseen'] += 1
            elif not prediction or event['option_id'] not in prediction:
                row['unscored'] += 1
            else:
                order = [identity for identity in prediction if identity in visible[oid]]
                hit = int(order[0] == event['option_id'])
                row['scored'] += 1
                row['hits'] += hit
                unit = offer['format'], offer['episode_id'], int(event['at'] // 86400)
                episodes.setdefault(unit, []).append(hit)
        if predictor and (offer['format'] != 'mixes' or getattr(predictor, 'mix_precision', None) == mix_choices.PRECISION):
            predictor.observe(event)
    for format, row in counts.items():
        units = [sum(values) / len(values) for unit, values in episodes.items() if unit[0] == format]
        row['episode_days'] = len(units)
        row['episode_day_mean'] = sum(units) / len(units) if units else None
        if len(units) >= 2:
            import numpy as np
            rng = np.random.default_rng(141)
            means = np.concatenate([rng.choice(units, (min(32, 2000 - start), len(units)),
                replace=True).mean(axis=1) for start in range(0, 2000, 32)])
            row['interval95'] = list(map(float, np.quantile(means, [.025, .975])))
        else:
            row['interval95'] = None
    return dict(schema=1, at=now, formats=counts, basis='recorded menus only', status='diagnostic',
                promotion_supported=False, source_ids=sorted(choices))
