"""Acknowledge sufficient correction inputs before releasing diagnostic contexts."""
from contextlib import ExitStack
import hashlib
import json

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical, EvidenceBudgetExceeded, EvidenceConflict
from yt_playlist.rec.choice_context import (CORRECTION_CONTRACT, checkpoint,
                                           correction_context, resolve_context)
from yt_playlist.rec import group_roots

BATCH = 32
DIAGNOSTIC_DAYS = 30
DIAGNOSTIC_FRACTION = .1
AFTER = 'listening_evidence_checkpoint_after'


class _Scan:
    """Resume immutable correction metadata without keeping a SQLite reader.

    Published checkpoints and the last fully examined opportunity survive a
    process restart. No raw evidence is released by this cursor.
    """
    def __init__(self, ledger, after):
        self.paths = iter(sorted(path for path in (ledger.directory / 'opportunity').glob('*/*.json')
                                 if path.stem > after))
        self.path = next(self.paths, None)
        self.identity = self.context = None
        self.historical = []
        self.acknowledged = set()

    def advance(self, store):
        store.settings.set_setting(AFTER, self.path.stem)
        self.path = next(self.paths, None)
        self.identity = self.context = None
        self.historical = []


def acknowledge(store, now, publish):
    with group_roots.admission(store):
        return _acknowledge(store, now, publish)


def _acknowledge(store, now, publish):
    """Commit each completed context before examining the next opportunity.

    Learner publication precedes the checkpoint, and checkpoint roots precede
    its immutable file. A deadline retains the current context's metadata for
    the next turn; a restart skips only fully examined opportunities.
    """
    ledger = store.rec_ledger.evidence()
    scan = getattr(store, '_choice_retention_scan', None)
    if scan is None:
        scan = store._choice_retention_scan = _Scan(ledger, store.settings.get_setting(AFTER) or '')
    store._choice_retention_more = True
    report, reader = None, None
    with ExitStack() as readers:
        for _ in range(BATCH):
            check_cancellable_work()
            if scan.path is None:
                break
            if scan.identity is None:
                try:
                    offer = ledger.project_opportunity(json.loads(scan.path.read_bytes()))
                except FileNotFoundError:
                    scan.advance(store)
                    continue
                if offer['observed_at'] > now or offer['at'] > now:
                    scan.advance(store)
                    continue
                scan.identity = offer['context']
            identity = scan.identity
            if saved := checkpoint(ledger, identity):
                group_roots.retain(store, 'checkpoint', identity, saved['context'], now)
                scan.acknowledged.add(identity)
                scan.advance(store)
                continue
            try:
                if scan.context is None:
                    scan.context = resolve_context(store, {'artifact': identity})
                context = scan.context
                if context.get('at', now) > now:
                    scan.advance(store)
                    continue
                context = correction_context(context)
            except ValueError:
                # Preserve the raw graph if its correction inputs are not yet
                # available. A later complete scan can examine it again.
                scan.advance(store)
                continue
            if report is None:
                report = publish(store, now)
            value = {'schema': 1, 'contract': CORRECTION_CONTRACT, 'source_context': identity,
                     'context': context, 'digest': hashlib.sha256(canonical(context)).hexdigest(),
                     'watermark': report['watermark'], 'incorporated_at': now}
            try:
                if not checkpoint(ledger, identity):
                    group_roots.retain(store, 'checkpoint', identity, context, now)
                    ledger.put('checkpoint', identity, value)
            except (EvidenceBudgetExceeded, EvidenceConflict):
                pass
            if checkpoint(ledger, identity):
                scan.acknowledged.add(identity)
            scan.advance(store)
    if scan.path is None:
        store.settings.set_setting(AFTER, '')
        store._choice_retention_scan = None
        store._choice_retention_more = False
    return scan.acknowledged
