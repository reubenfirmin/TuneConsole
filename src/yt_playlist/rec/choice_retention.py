"""Acknowledge sufficient correction inputs before releasing diagnostic contexts."""
import hashlib

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical, EvidenceBudgetExceeded, EvidenceConflict
from yt_playlist.rec.choice_context import CORRECTION_CONTRACT, correction_context
from yt_playlist.controllers.choice_context import ChoiceContexts
from yt_playlist.rec import group_roots

BATCH = 32
DIAGNOSTIC_DAYS = 30
DIAGNOSTIC_FRACTION = .1


class _Scan:
    """Resume immutable correction metadata without keeping a SQLite reader.

    Published checkpoints and the last fully examined opportunity survive a
    process restart. No raw evidence is released by this cursor.
    """
    def __init__(self, ledger, after):
        self.inventory = ledger.checkpoint_inventory()
        self.paths = iter(sorted((path for path in ledger.paths('opportunity')
                                  if path.stem > after), key=lambda path: path.stem))
        self.path = next(self.paths, None)
        self.identity = self.context = None
        self.acknowledged = set()

    def advance(self, store, *, already_checkpointed=False):
        if not already_checkpointed:
            store.evidence_files.update_maintenance(checkpoint_after=self.path.stem)
        self.path = next(self.paths, None)
        self.identity = self.context = None


def acknowledge(store, now, publish):
    with group_roots.admission(store):
        return _acknowledge(store, now, publish)


def _acknowledge(store, now, publish):
    """Commit each completed context before examining the next opportunity.

    Learner publication precedes the immutable checkpoint. A deadline retains
    the current context's metadata for the next turn. On restart, already
    checkpointed contexts are safe to examine again without republishing them.
    """
    ledger = store.rec_ledger.evidence()
    scan = getattr(store, '_choice_retention_scan', None)
    if scan is None:
        after = store.evidence_files.maintenance_state()['checkpoint_after']
        scan = store._choice_retention_scan = _Scan(ledger, after)
    store._choice_retention_more = True
    report = None
    for _ in range(BATCH):
        check_cancellable_work()
        if scan.path is None:
            break
        if scan.identity is None:
            try:
                offer = ledger.read_path(scan.path, opportunity_inventory=scan.inventory)
            except FileNotFoundError:
                scan.advance(store)
                continue
            if offer is None or offer['observed_at'] > now or offer['at'] > now:
                scan.advance(store)
                continue
            scan.identity = offer['context']
        identity = scan.identity
        if ledger.has_checkpoint(identity, scan.inventory):
            scan.acknowledged.add(identity)
            scan.advance(store, already_checkpointed=True)
            continue
        try:
            if scan.context is None:
                scan.context = ChoiceContexts(ledger).resolve({'artifact': identity})
            context = scan.context
            if context.get('at', now) > now:
                scan.advance(store)
                continue
            context = correction_context(context)
        except ValueError:
            # Keep the raw graph when its correction inputs are unavailable.
            scan.advance(store)
            continue
        if report is None:
            report = publish(store, now)
        value = {'schema': 1, 'contract': CORRECTION_CONTRACT, 'source_context': identity,
                 'context': context, 'digest': hashlib.sha256(canonical(context)).hexdigest(),
                 'watermark': report['watermark'], 'incorporated_at': now}
        try:
            ledger.put('checkpoint', identity, value)
        except (EvidenceBudgetExceeded, EvidenceConflict):
            pass
        if ledger.has_checkpoint(identity, scan.inventory):
            scan.acknowledged.add(identity)
        scan.advance(store)
    if scan.path is None:
        store.evidence_files.update_maintenance(checkpoint_after='')
        store._choice_retention_scan = None
        store._choice_retention_more = False
    return scan.acknowledged
