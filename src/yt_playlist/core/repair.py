"""Repair coarse YouTube history only with verified import coverage and a published backup.

Event timestamps describe observations, not an export's account scope or completeness. Ordinary
Takeout uploads therefore never authorize deletion. Legacy daily rows also lack source proof and
are preserved; only observations known to come solely from YouTube's history window are eligible.
"""
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time

CORROBORATION_TOLERANCE_DAYS = 1
PHANTOM_PURGE_SEEN = "repair_phantom_verified_import_seen"

logger = logging.getLogger(__name__)


def _verified_imports(conn):
    return conn.execute(
        "SELECT id,identity_id,coverage_start,coverage_end FROM takeout_imports "
        "WHERE account_verified=1 AND complete=1 AND unparsed_dates=0 "
        "AND coverage_start IS NOT NULL AND coverage_end > coverage_start ORDER BY id").fetchall()


def takeout_windows(conn, identity_id):
    """Whole UTC days covered by each verified import; never bridge disjoint uploads.

    Both partial boundary days are excluded. Observed first/last event times are deliberately
    unused: neither can establish the interval during which missing events are meaningful.
    """
    windows = []
    for row in _verified_imports(conn):
        if row['identity_id'] != identity_id:
            continue
        start, end = row['coverage_start'], row['coverage_end']
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        lo, hi = math.ceil(start / 86400), math.floor(end / 86400) - 1
        if lo <= hi:
            windows.append((lo, hi))
    return windows


def _phantom_rows(conn, identity_id, lo, hi):
    # Any exact event corroborates, regardless of its source. Unknown and independently observed
    # daily rows cannot be disproved by a YouTube export and must survive even without an event.
    return conn.execute(
        "SELECT hi.rowid rid,hi.*,hs.identity_id,hs.taken_at "
        "FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id "
        "WHERE hs.identity_id=? AND hi.source='ytm_history' "
        "AND CAST(hs.taken_at / 86400 AS INTEGER) BETWEEN ? AND ? "
        "AND NOT EXISTS (SELECT 1 FROM play_events pe "
        "  WHERE pe.identity_id=hs.identity_id AND pe.identity_key=hi.identity_key "
        "  AND ABS(CAST(pe.played_at / 86400 AS INTEGER) "
        "          - CAST(hs.taken_at / 86400 AS INTEGER)) <= ?)",
        (identity_id, lo, hi, CORROBORATION_TOLERANCE_DAYS)).fetchall()


def _publish_backup(backup_path, rows):
    """Publish complete, flushed JSONL atomically without overwriting earlier recovery evidence."""
    path = Path(backup_path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.phantom-history-', delete=False) as fh:
            temporary = Path(fh.name)
            for row in rows:
                fh.write(json.dumps(dict(row)) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def purge_phantom_history(conn, backup_path=None) -> int:
    """Delete contradicted YouTube observations after publishing their complete recovery file.

    Own the transaction: failures roll back every deletion. Refuse an outstanding caller transaction
    rather than committing unrelated work or allowing a later caller commit to publish a failed purge.
    Callers sharing a Store connection must hold its lock (run_once does this).
    """
    if conn.in_transaction:
        raise ValueError('History repair requires its own transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        rows = {}
        for ident in {r['identity_id'] for r in _verified_imports(conn)}:
            for lo, hi in takeout_windows(conn, ident):
                for row in _phantom_rows(conn, ident, lo, hi):
                    rows[row['rid']] = row
        if not rows:
            conn.rollback()
            return 0
        if backup_path is None:
            raise ValueError('History repair requires a recovery backup; nothing was deleted')
        _publish_backup(backup_path, rows.values())
        conn.executemany('DELETE FROM history_items WHERE rowid=?', [(rid,) for rid in rows])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info('repair: purged %d contradicted YouTube observations; recoverable from %s',
                len(rows), backup_path)
    return len(rows)


def run_once(store) -> None:
    """Repair once per verified import, leaving ordinary, incomplete uploads non-destructive."""
    with store._lock:
        imports = _verified_imports(store.conn)
        if not imports:
            return
        # An older upload can be verified later, or have its verified interval corrected.
        # The newest id alone would hide those changes once a newer upload had been repaired.
        evidence = [tuple(row) for row in imports]
        watermark = hashlib.sha256(json.dumps(evidence).encode()).hexdigest()
        if watermark == store.settings.get_setting(PHANTOM_PURGE_SEEN):
            return
        try:
            from yt_playlist.core import paths
            backup = paths.backups_dir() / f'phantom_history_purge_{watermark}_{time.time_ns()}.jsonl'
            purge_phantom_history(store.conn, backup)
        except Exception:
            logger.warning('repair skipped; history preserved, no successful purge recorded', exc_info=True)
            return
        store.settings.set_setting(PHANTOM_PURGE_SEEN, watermark)
