"""Verify immutable menu/action custody before publishing listening evidence."""
import hashlib
import math

from yt_playlist.core import mix_choices
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.controllers.choice_context import ChoiceContexts


class ChoiceEvidence:
    def __init__(self, ledger, *, publish_mix, retain, roots_enabled):
        self.ledger, self.publish_mix = ledger, publish_mix
        self.retain, self.roots_enabled = retain, roots_enabled

    def issue(self, record, context, *, reuse=False):
        identity = record['id']
        if reuse and self.ledger.get('opportunity', identity) is not None:
            return identity
        context = mix_choices.context(ChoiceContexts(self.ledger).manifest(context), mixes=record['format'] == 'mixes')
        record = issued_record(record, context)
        if record['format'] == 'mixes':
            # Descriptor custody is acknowledged before its immutable event file.
            record = self.publish_mix(record, record['context'])
        self.ledger.put('opportunity', identity, record, artifact=context)
        return identity

    def respond(self, opportunity_id, kind, now, *, option_id, action_id, visible, details):
        offer = self.ledger.get('opportunity', opportunity_id)
        if offer is None:
            return False
        record = response_record(offer, kind, now, option_id=option_id, action_id=action_id,
                                 visible=visible, details=details)
        previous = self.ledger.get('response', record['id'])
        if previous:
            if (previous['opportunity_id'], previous['kind'], previous['option_id']) != (
                    opportunity_id, kind, record['option_id']):
                raise ValueError('Response ID was reused for another action')
            return False
        if self.roots_enabled():
            self.retain('response', record['id'], {'artifact': offer['context']}, now)
        return self.ledger.put('response', record['id'], record)


def issued_record(record, context):
    options = record['options']
    scores = [option.get('score') for option in options]
    scored = all(isinstance(score, (int, float)) and math.isfinite(score) for score in scores)
    prediction = [str(options[i]['id']) for i in sorted(range(len(options)),
        key=lambda i: (-scores[i], i))] if scored else None
    return {**record, 'context': hashlib.sha256(canonical(context)).hexdigest(),
            'prediction': prediction, 'prediction_status': 'issued' if scored else 'unscored'}


def response_record(offer, kind, now, *, option_id, action_id, visible, details):
    ids = {str(option['id']) for option in offer['options']}
    if (option_id is not None and str(option_id) not in ids) or not set(map(str, visible)) <= ids:
        raise ValueError('Response does not belong to this opportunity')
    if now < offer['at']:
        raise ValueError('Response precedes its opportunity')
    if offer['format'] == 'mixes' and details:
        details = {key: value for key, value in details.items()
            if key not in {'tracks', 'keys', 'feature', 'recipe', 'group_id', 'group_version', 'group_generation'}}
    identity = action_id or f"{offer['id']}:{kind}:{option_id or ''}"
    return {'schema': 1, 'id': identity, 'opportunity_id': offer['id'], 'kind': kind,
        'option_id': str(option_id) if option_id is not None else None,
        'at': now, 'observed_at': now, 'source': 'live', 'precision': 'exact',
        'visible': sorted(map(str, visible)), 'details': details or {}}
