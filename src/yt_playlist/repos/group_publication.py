"""Transaction boundaries and row budgets for group publication."""
from contextlib import contextmanager
import hashlib
import json

from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.core.db_checkpoint import checkpoint_wal, deferred_autocheckpoint
from yt_playlist.core.db_diagnostics import database_operation

BATCH_ROWS = 128
BATCH_BYTES = 256 * 1024
MAX_ROW_BYTES = 8 * 1024 * 1024
LEASE_SECONDS = 60
_CLEANUP_OWNER = 'cleanup'


class _BuildBeingCleaned(Exception):
    pass


class BuildUnavailable(WorkBudgetExceeded):
    """A retained definition or live lease still belongs to another build."""


@contextmanager
def _write(repo):
    committed, checkpoint = False, False
    try:
        with database_operation('CompactTasteGroupsRepo.stage'), repo._lock:
            check_cancellable_work()
            if repo.conn.in_transaction:
                raise RuntimeError('Group staging requires an idle serving connection')
            with deferred_autocheckpoint(repo.conn, repo._store._db_path is not None) as checkpoint:
                with repo.conn:
                    repo.conn.execute('BEGIN IMMEDIATE')
                    yield repo.conn
                committed = True
    finally:
        if checkpoint and committed:
            checkpoint_wal(repo._store._db_path)


def _batches(rows):
    batch, size = [], 0
    for row in rows:
        check_cancellable_work()
        used = sum(len(value.encode('utf-8')) if isinstance(value, str) else 8 for value in row)
        if used > MAX_ROW_BYTES:
            raise WorkBudgetExceeded('Musical group publication row exceeds byte budget')
        if batch and (len(batch) >= BATCH_ROWS or size + used > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += used
    if batch:
        yield batch


def _header(prepared):
    generation = prepared['generation']
    at = prepared['at']
    identity = (generation, prepared['policy'], prepared['fingerprint'], at, prepared['serial'])
    if '_stage_digest' not in prepared:
        # The ordered digest also identifies prefixes staged before restart.
        groups = prepared['groups']
        sections = (
            [(*identity, prepared['payload'])],
            ((g['version'], generation, g['group_id'], g['parent_version'], g['payload'],
              g.get('legacy_mode_id'), at) for g in groups),
            ((g['version'], *member) for g in groups for member in g['members']),
            ((generation, *row) for row in prepared['unknown']),
            ((generation, *row) for row in prepared['edges']),
        )
        digest = hashlib.sha256()
        for rows in sections:
            for batch in _batches(rows):
                for row in batch:
                    digest.update(json.dumps(row, separators=(',', ':'), allow_nan=False).encode())
                    digest.update(b'\n')
        prepared['_stage_digest'] = digest.hexdigest()
