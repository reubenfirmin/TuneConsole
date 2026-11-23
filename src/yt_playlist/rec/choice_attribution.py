"""Verified listener actions linked to their recorded offer, option and episode."""

from collections import defaultdict

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.listening_calendar import day_at

OUTCOMES = {'start', 'experience', 'save', 'promote', 'return'}
ORDER = {'visible': 0, 'choice': 1, 'start': 2, 'experience': 3}


def observed_responses(responses, now):
    observed = [row for row in responses if row['at'] <= now and row['observed_at'] <= now]
    withdrawn = {row['details'].get('action_id') for row in observed if row['kind'] == 'withdraw'}
    unique = {row['id']: row for row in observed if row['kind'] != 'withdraw' and row['id'] not in withdrawn}
    return sorted(unique.values(), key=response_order)


def response_order(row):
    return row['at'], ORDER.get(row['kind'], 4), row.get('observed_at', row['at']), row['id']


def by_offer(responses):
    grouped = defaultdict(list)
    for response in responses:
        grouped[response['opportunity_id']].append(response)
    return grouped


def trace(offer, responses, horizon):
    """Join observed responses to the exact alternative and the choice that started its episode."""
    options = {str(row['id']): row for row in offer['options']}
    selected, starts, visible, receipts = {}, {}, set(), set()
    decisions, closed = [], False
    for event in sorted(responses, key=response_order):
        check_cancellable_work()
        if event['at'] < offer['at']:
            continue
        kind, option_id = event['kind'], event['option_id']
        if kind == 'visible':
            visible.update(value for value in event['visible'] if value in options)
            continue
        if kind == 'close':
            closed |= bool(visible)
            continue
        if option_id not in options:
            continue
        details = event.get('details') or {}
        origin = details.get('choice_action_id')
        if origin is not None and not isinstance(origin, str):
            continue
        decision = selected.get((option_id, origin))
        if origin is None and isinstance(details.get('start_at'), (int, float)):
            decision = starts.get((option_id, details['start_at']))
        # A confirmed save directly from a menu is also an explicit selection.
        if (kind == 'choice' or (kind == 'save' and origin is None and 'start_at' not in details and
                (decision is None or event['at'] - decision['choice']['at'] > horizon))):
            decision = {'offer': offer, 'option': options[option_id], 'choice': event,
                        'shown': option_id in visible, 'outcomes': []}
            selected[(option_id, None)] = selected[(option_id, event['id'])] = decision
            decisions.append(decision)
        if kind in OUTCOMES and decision and 0 <= event['at'] - decision['choice']['at'] <= horizon:
            if kind == 'start' or (kind == 'experience' and details.get('video_id') and not details.get('kind')):
                starts[(option_id, event['at'])] = decision
            receipt = details.get('receipt_id')
            if not isinstance(receipt, str) or not receipt:
                receipt = event['id']
            if (kind, receipt) not in receipts:
                receipts.add((kind, receipt))
                decision['outcomes'].append(event)
    return visible, closed, decisions


def actions(offers, responses, horizon):
    return sorted((decision for identity, events in by_offer(responses).items() if identity in offers
                   for decision in trace(offers[identity], events, horizon)[2]),
                  key=lambda row: (row['choice']['at'], row['choice']['id']))


def provenance(decision):
    """Link the published context to its retained evidence and audible session clock."""
    event = decision['choice']
    evidence = {'choice_action_id': event['id']}
    start = next((row for row in decision['outcomes'] if row['kind'] == 'start'), None)
    account = (event.get('details') or {}).get('account_id')
    if start is not None:
        evidence['choice_start_at'] = start['at']
        if account is None:
            account = (start.get('details') or {}).get('account_id')
    if account is not None:
        evidence['choice_account_id'] = account
    return evidence


def calendar_day(at, provenance, timezone, calendars):
    return day_at(at, calendars.get(provenance.get('choice_account_id'), timezone))


def strength(decision):
    """A later outcome corroborates the original decision once."""
    kinds = {event['kind'] for event in decision['outcomes']}
    return 1. if kinds & {'start', 'save', 'promote', 'return'} else .5
