"""Acknowledge sufficient correction inputs before releasing diagnostic contexts."""
import hashlib

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical, EvidenceBudgetExceeded, EvidenceConflict
from yt_playlist.rec.choice_context import (CORRECTION_CONTRACT, checkpoint,
                                           correction_context, resolve_context)

BATCH = 32
DIAGNOSTIC_DAYS = 30
DIAGNOSTIC_FRACTION = .1


def acknowledge(store, now, publish):
    """One bounded batch, published only after the learners' transaction succeeds.

    A checkpoint is an immutable replacement for a context's *learning* inputs,
    not a claim that diagnostic replay fitted a model. Original options, events,
    source IDs and chronological predictions remain in their original records.
    A failed publication or oversized correction capture keeps the raw graph.
    """
    ledger = store.rec_ledger.evidence()
    identities = sorted({offer['context'] for offer in ledger.records('opportunity')
                         if offer['observed_at'] <= now and offer['at'] <= now})
    acknowledged, pending, definitions = set(), [], {}
    for identity in identities:
        check_cancellable_work()
        if checkpoint(ledger, identity):
            acknowledged.add(identity)
        elif len(pending) < BATCH:
            try:
                context = resolve_context(store, {'artifact': identity})
                if context.get('at', now) <= now:
                    if context.get('musical_groups'):
                        historical = []
                        for group in context['musical_groups']:
                            version = group['version']
                            if version not in definitions:
                                definitions[version] = store.taste_groups.definition(version)
                            if definitions[version] is None:
                                raise ValueError('Historical musical definition is not available yet')
                            historical.append(definitions[version])
                        context = {**context, 'musical_definitions': historical}
                    pending.append((identity, correction_context(context)))
            except ValueError:
                continue
    if not pending:
        return acknowledged
    report = publish(store, now)
    for identity, context in pending:
        check_cancellable_work()
        value = {'schema': 1, 'contract': CORRECTION_CONTRACT, 'source_context': identity,
                 'context': context, 'digest': hashlib.sha256(canonical(context)).hexdigest(),
                 'watermark': report['watermark'], 'incorporated_at': now}
        try:
            # Another application's first successful acknowledgement also suffices.
            if not checkpoint(ledger, identity):
                ledger.put('checkpoint', identity, value)
        except (EvidenceBudgetExceeded, EvidenceConflict):
            pass
        if checkpoint(ledger, identity):
            acknowledged.add(identity)
    return acknowledged
