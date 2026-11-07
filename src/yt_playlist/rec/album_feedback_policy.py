"""Release-response replacement and withdrawal identities."""
import math
import uuid


def validate_time(now):
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError('Feedback requires a finite observation time')


def validate_account(account_id, active):
    if account_id is None:
        raise ValueError('Choose an active account before giving album feedback')
    if not active:
        raise ValueError('Album feedback requires an active account')


def validate_replacement(previous, now):
    if any(row['created_at'] > now for row in previous if row['withdrawn_at'] is None):
        raise ValueError('Feedback precedes the retained response')


def merged_withdrawals(rows, now):
    ordered = sorted(rows, key=lambda row: (row['created_at'], row['source_browse_id'], row['id']))
    winners = {row['account_id']: row['id'] for row in ordered}
    return [(max(now, row['created_at']), row['id']) for row in ordered
            if winners[row['account_id']] != row['id']]


def action_id(kind):
    return uuid.uuid4().hex if kind != 'clear' else None


def response_intents(previous, action, opportunity_id, browse, kind, now):
    withdrawn, intents = set(), []
    for row in previous:
        opportunity, original_action = row.get('opportunity_id'), row.get('action_id')
        if not opportunity or not original_action or (opportunity, original_action) in withdrawn:
            continue
        withdrawal = uuid.uuid5(uuid.NAMESPACE_URL, 'album-feedback-withdrawal:' + str(original_action)).hex
        intents.append({'action_id': withdrawal, 'opportunity_id': opportunity, 'kind': 'withdraw',
                        'now': now, 'target_action_id': original_action})
        withdrawn.add((opportunity, original_action))
    if action and opportunity_id:
        intents.append({'action_id': action, 'opportunity_id': opportunity_id, 'kind': 'feedback',
                        'now': now, 'option_id': browse, 'feedback_kind': kind})
    return intents


def replace_legacy(original, preferred, browse, kind, now, opportunity_id):
    current, previous = dict(original), []
    for source in original:
        if preferred[source] == preferred[browse]:
            previous.append(current.pop(source))
    action = action_id(kind)
    if action:
        current[browse] = {'kind': kind, 'at': now, 'opportunity_id': opportunity_id, 'action_id': action}
    return current, response_intents(previous, action, opportunity_id, browse, kind, now)
