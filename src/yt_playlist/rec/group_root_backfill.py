"""Resumable bounded inventory of existing, explicitly typed group evidence.

The live producers install their own roots. Restarting this iterator merely
revisits immutable records; the SQL fence remains closed until every old domain
has been inventoried. There is no historical schema registry or generic JSON scan.
"""
import hashlib
from dataclasses import dataclass
from itertools import tee
import json
import math
import time

from yt_playlist.core import group_evidence
from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from . import group_roots
from .choice_context import checkpoint

BATCH_ROWS = 32
BATCH_BYTES = 256 * 1024
BATCH_SECONDS = .15
MAX_RECORD_BYTES = 8 * 1024 * 1024
KINDS = ('opportunity', 'response', 'checkpoint', 'proposals', 'orphan_offers')


@dataclass(frozen=True)
class Owner:
    kind: str
    identity: str
    captured: dict | None
    at: float
    raw: str
    reason: str


@dataclass(frozen=True)
class Proposal:
    surface: str
    payload: str
    at: float
    data: dict
    contexts: dict


class Backfill:
    def __init__(self, store):
        self.store = group_roots.source(store)
        self.stage = 0
        self.paths = None
        self.pending_path = None
        self.after = ''
        self._retry_cursor = None

    def _unknown(self, kind, identity, raw, reason, at):
        return Owner(kind, identity, None, at, raw, reason)

    def _record(self, ledger, kind, value, path):
        if not isinstance(value, dict):
            raise ValueError('Historical listening evidence must be an object')
        identity = value.get('source_context') if kind == 'checkpoint' else value.get('id')
        if (not isinstance(identity, str) or not identity
                or hashlib.sha256(identity.encode()).hexdigest() != path.stem):
            raise ValueError('Historical listening evidence has an invalid source identity')
        at = value.get('at', value.get('incorporated_at'))
        if type(at) not in (int, float) or not math.isfinite(at):
            raise ValueError('Historical listening evidence has an invalid observation time')
        owner_kind = 'offer' if kind == 'opportunity' else kind
        # A current producer already established these exact immutable roots.
        # Never reinterpret an explicitly unresolved historical reference later.
        with self.store._lock:
            if self.store.conn.execute('SELECT 1 FROM group_evidence_owners WHERE kind=? AND external_key=?',
                                       (owner_kind, identity)).fetchone():
                return
        # Choice owners no longer depend on historical groups. Preparation
        # releases existing pins only after publishing the replacement learners.
        # Backfill must neither recreate a pin nor manufacture an unknown owner.
        return Owner(owner_kind, identity, {}, at, '', '')

    def _advance(self):
        self.stage += 1
        self.paths, self.pending_path, self.after = None, None, ''

    def _proposals(self):
        with self.store.read_view() as reader:
            row = reader.conn.execute('SELECT surface,length(CAST(payload AS BLOB)) FROM rec_proposals '
                'WHERE surface>? AND (surface IN (?,?,?,?,?) OR surface LIKE ? ESCAPE \'\\\') '
                'ORDER BY surface LIMIT 1',
                (self.after, *sorted(group_roots.PREPARED), r'model\_pair:%')).fetchone()
            if row is None:
                self._advance()
                return 0, None
            surface, size = row
            if size > MAX_RECORD_BYTES:
                raise WorkBudgetExceeded('Historical prepared group evidence exceeds its admitted memory')
            payload, at = reader.conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?',
                                              (surface,)).fetchone()
        value = json.loads(payload)
        data = value if isinstance(value, dict) else {}
        if surface.startswith('model_pair:') and isinstance(data.get('data'), dict):
            data = data['data']
        contexts = {}
        for field in group_roots.FIELDS:
            captured = data.get(field)
            if surface == 'adaptive_group_preview' and field == 'context' and data.get('generation'):
                captured = {'group_generation': data['generation']}
            if captured:
                try:
                    contexts[field] = group_roots.context(self.store, captured)
                except ValueError:
                    pass  # Publication retains this original unresolved source explicitly.
        self.after = surface
        return size, Proposal(surface, payload, at, value, contexts)

    def _orphan(self, ledger):
        with self.store._lock:
            row = self.store.conn.execute("SELECT external_key FROM group_evidence_owners WHERE kind='offer' "
                                           'AND external_key>? ORDER BY external_key LIMIT 1', (self.after,)).fetchone()
        if row is None:
            self._advance()
            return
        identity = row[0]
        # The admission lock fences every file publisher. A queued offer has
        # its own pending root until it establishes the durable offer root.
        self.after = identity
        return identity if ledger.get('opportunity', identity) is None else None

    def _apply(self, operation):
        if isinstance(operation, Owner):
            if operation.captured is not None:
                try:
                    group_roots.retain(self.store, operation.kind, operation.identity,
                                       operation.captured, operation.at, strict=False)
                    return
                except ValueError:
                    pass
            group_evidence.retain_unknown_context(self.store.conn, operation.kind, operation.identity,
                                                  operation.raw, operation.reason, operation.at)
        elif isinstance(operation, Proposal):
            # Admission fences publishers while preparation happens outside the
            # serving lock. Also verify the exact SQL input before installing roots.
            if self.store.conn.execute('SELECT 1 FROM rec_proposals WHERE surface=? AND payload=? AND built_at IS ?',
                    (operation.surface, operation.payload, operation.at)).fetchone():
                group_roots.replace_prepared(self.store, operation.surface, operation.data, operation.at,
                                            historical=True, resolved_contexts=operation.contexts)
        else:
            group_roots.release(self.store, 'offer', operation)

    def step(self):
        if not group_roots.enabled(self.store):
            return False
        with self.store._lock:
            if self.store.conn.execute('SELECT roots_complete FROM group_retention_state WHERE id=1').fetchone()[0]:
                return False
        with group_roots.admission(self.store):
            if self._retry_cursor is not None:
                self.stage, self.paths, self.pending_path, self.after = self._retry_cursor
                self._retry_cursor = None
            ledger = self.store.rec_ledger.evidence()
            if self.paths is None and self.stage < 3:
                self.paths = iter((ledger.directory / KINDS[self.stage]).glob('*/*.json'))
            backup = None
            if self.paths is not None:
                self.paths, backup = tee(self.paths)
            cursor = self.stage, backup, self.pending_path, self.after
            try:
                # File reads, decoding and context expansion can be slow. Keep
                # them outside the serving lock; only the bounded SQL publication
                # shares it. Cancellation replays the entire uncommitted batch.
                operations = self._batch(ledger)
                with group_roots.writer(self.store):
                    for operation in operations:
                        check_cancellable_work()
                        self._apply(operation)
                    if self.stage == len(KINDS):
                        self.store.conn.execute('UPDATE group_retention_state SET roots_complete=1 WHERE id=1')
            except BaseException:
                self._retry_cursor = cursor
                raise
            return True

    def _batch(self, ledger):
        used = count = 0
        operations = []
        deadline = time.monotonic() + BATCH_SECONDS
        while (self.stage < len(KINDS) and count < BATCH_ROWS and used < BATCH_BYTES
               and (not count or time.monotonic() < deadline)):
            check_cancellable_work()
            kind = KINDS[self.stage]
            if kind == 'proposals':
                size, operation = self._proposals()
                used += size
                if operation is not None:
                    operations.append(operation)
                count += 1
                continue
            if kind == 'orphan_offers':
                operation = self._orphan(ledger)
                if operation is not None:
                    operations.append(operation)
                count += 1
                continue
            if self.paths is None:
                self.paths = iter((ledger.directory / kind).glob('*/*.json'))
            path = self.pending_path or next(self.paths, None)
            self.pending_path = None
            if path is None:
                self._advance()
                continue
            try:
                size = path.stat().st_size
                if size > MAX_RECORD_BYTES:
                    self.pending_path = path
                    raise WorkBudgetExceeded('Historical listening group evidence exceeds its admitted memory')
                if used and used + size > BATCH_BYTES:
                    self.pending_path = path
                    break
                # Keep this exact path on cancellation or malformed input;
                # retry must never skip an unprocessed historical owner.
                self.pending_path = path
                value = json.loads(path.read_bytes())
                operation = self._record(ledger, kind, value, path)
                if operation is not None:
                    operations.append(operation)
                self.pending_path = None
                used += size
                count += 1
            except FileNotFoundError:
                self.pending_path = None
        return operations


def maintain(store):
    store = group_roots.source(store)
    if not group_roots.enabled(store):
        return False
    if not hasattr(store, '_group_root_backfill'):
        store._group_root_backfill = Backfill(store)
    return store._group_root_backfill.step()
