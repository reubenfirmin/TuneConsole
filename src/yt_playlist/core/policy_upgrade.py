"""Resumable policy repair on a separate NORMAL/WAL maintenance connection.

Temporary compaction installs through SQLite's writer transaction while UI readers continue.
Only disposable policy rows are collected; no stale snapshot replaces live data.
"""
import json
import logging
from pathlib import Path
import sqlite3
import time

from yt_playlist.core import db_space, policy_config, temporary_compaction
from yt_playlist.core.policy_recovery import _verify
from yt_playlist.repos import policy_storage

log = logging.getLogger(__name__)
KEY = 'repair_shared_policies_v1'
BATCH_ROWS = 128
BATCH_BYTES = 4 * 1024 * 1024
REPAIR_RESERVE = 32 * 1024 * 1024
COMPACT_MIN_BYTES = 8 * 1024 * 1024
LEGACY = ("(NOT EXISTS (SELECT 1 FROM rec_policy_variants v WHERE v.model_version=p.model_version) "
          "OR json_type(p.configuration,'$._policy_base') IS NULL)")


def _save(conn, state):
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (KEY, json.dumps(state)))


def _has_legacy(conn):
    return conn.execute('SELECT 1 FROM rec_serving_policies p WHERE ' + LEGACY + ' LIMIT 1').fetchone()


def _checkpoint(conn):
    if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal':
        busy, pages, done = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if busy or pages != done:
            raise sqlite3.OperationalError('Policy repair checkpoint is busy; stop other application instances')


def _batch(conn, state, roots, now, directory):
    conn.execute('BEGIN IMMEDIATE')
    try:
        return _batch_locked(conn, state, roots, now, directory)
    except BaseException:
        conn.rollback()
        raise


def _batch_locked(conn, state, roots, now, directory):
    after = ' AND p.model_version>?' if state['cursor'] is not None else ''
    params = [state['cursor']] if state['cursor'] is not None else []
    metadata, size = [], 0
    for version, created_at, byte_size, unused in conn.execute(
            'SELECT p.model_version,p.created_at,length(CAST(p.configuration AS BLOB)),('
            + policy_storage.UNSERVED + ') FROM rec_serving_policies p WHERE ' + LEGACY + after +
            ' ORDER BY p.model_version LIMIT ?', (*params, BATCH_ROWS)):
        metadata.append((version, created_at, unused))
        size += byte_size
        if size >= BATCH_BYTES:
            break
    if not metadata:
        conn.rollback()
        return False
    if directory is not None:
        # Journal/WAL, new shared bodies and index pages, without a second copy of
        # the entire oversized file. Every batch checkpoints before proceeding.
        db_space.require_space(directory, 4 * size + BATCH_BYTES, reserve=REPAIR_RESERVE)
    # data_version changes only for other connections' commits. Cache the large
    # prepared/session roots while our own policy-only commits leave them intact.
    # Capture/revalidate the version after reserving the writer, closing races.
    version = conn.execute('PRAGMA data_version').fetchone()[0]
    if roots.get('version') != version:
        roots.update(version=version, protected=policy_storage.protected_references(conn))
    protected = roots['protected']
    try:
        for version, created_at, unused in metadata:
            if unused and version not in protected and created_at < now - policy_storage.GRACE_SECONDS:
                conn.execute('DELETE FROM rec_serving_policies WHERE model_version=?', (version,))
                state['deleted'] += 1
                continue
            original = policy_storage.configuration(conn, version)
            packed, base = policy_config.pack(original, created_at, version=version)
            conn.execute('INSERT OR IGNORE INTO rec_policy_bases '
                '(base_version,configuration,byte_size,created_at) VALUES (?,?,?,?)', base)
            conn.execute('UPDATE rec_serving_policies SET configuration=? WHERE model_version=?',
                         (packed[1], version))
            previous = conn.execute('SELECT base_version FROM rec_policy_variants WHERE model_version=?',
                                    (version,)).fetchone()
            conn.execute('INSERT INTO rec_policy_variants(model_version,base_version) VALUES (?,?) '
                         'ON CONFLICT(model_version) DO UPDATE SET base_version=excluded.base_version', (version, base[0]))
            policy_storage.refresh_references(conn, [version])
            if previous and previous[0] != base[0]:
                conn.execute('UPDATE rec_policy_bases SET served=EXISTS(SELECT 1 FROM rec_policy_variants '
                    'WHERE base_version=? AND served=1) WHERE base_version=?', (previous[0], previous[0]))
            if policy_storage.configuration(conn, version) != original:
                raise ValueError(f'Automatic policy repair changed configuration {version}')
            state['shared'] += 1
        state['cursor'] = metadata[-1][0]
        _save(conn, state)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    _checkpoint(conn)
    return True


def _compact(conn, directory):
    """Build in OS temp storage; install transactionally with one compact WAL."""
    _checkpoint(conn)
    temporary_compaction.compact(conn, directory,
        cancelled=getattr(conn, '_maintenance_cancelled', lambda: False))


def run_once(conn, *, now=None, progress=lambda phase: None, on_deferred=lambda error: None):
    """Run in a background worker; never use the serving Store connection."""
    row = conn.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    state = json.loads(row[0]) if row else None
    if state and state['phase'] == 'done':
        return True
    if state is None:
        legacy = _has_legacy(conn)
        free_bytes = conn.execute('PRAGMA freelist_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]
        phase = 'sharing' if legacy else ('compacting' if free_bytes >= COMPACT_MIN_BYTES else 'done')
        state = dict(phase=phase, cursor=None, deleted=0, shared=0)
        if phase == 'done':
            with conn:
                _save(conn, state)
            return True
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    directory = Path(path).parent if path else None
    secure_delete = conn.execute('PRAGMA secure_delete').fetchone()[0]
    try:
        if directory is not None:
            if conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise sqlite3.OperationalError('Automatic policy repair requires WAL')
            if conn.execute('PRAGMA locking_mode').fetchone()[0].lower() != 'normal':
                raise ValueError('Online policy repair requires NORMAL locking')
            # A reader may have delayed the last attempt's checkpoint. Do not
            # accumulate another cleanup batch in WAL until that work drains.
            _checkpoint(conn)
        if state['phase'] == 'compacting' and _has_legacy(conn):
            state.update(phase='sharing', cursor=None)
        log.warning('Automatic policy storage repair: phase=%s deleted=%s shared=%s',
                    state['phase'], state['deleted'], state['shared'])
        if state['phase'] == 'sharing':
            progress('checking')
            _verify(conn)
            roots = {}
            # Erasure is not required for derived policies that are about to be
            # compacted. Restore the connection preference even on deferral.
            conn.execute('PRAGMA secure_delete=OFF')
            progress('sharing')
            while True:
                if _batch(conn, state, roots, time.time() if now is None else now, directory):
                    log.info('Automatic policy storage repair: deleted=%s shared=%s', state['deleted'], state['shared'])
                elif _has_legacy(conn):
                    # An old application may have written a lower-sorting hash
                    # while a previous upgrade attempt was deferred.
                    state['cursor'] = None
                    with conn:
                        _save(conn, state)
                    _checkpoint(conn)
                else:
                    break
            _verify(conn)
            state['phase'] = 'compacting'
            with conn:
                _save(conn, state)
            _checkpoint(conn)
        if directory is not None and state['phase'] == 'compacting':
            free_bytes = conn.execute('PRAGMA freelist_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]
            if free_bytes >= COMPACT_MIN_BYTES:
                progress('compacting')
                _compact(conn, directory)
            state['phase'] = 'checkpointing'
            with conn:
                _save(conn, state)
        progress('checkpointing')
        _checkpoint(conn)
        _verify(conn)
        state['phase'] = 'done'
        with conn:
            _save(conn, state)
        _checkpoint(conn)
        log.warning('Automatic policy storage repair complete: deleted=%s shared=%s', state['deleted'], state['shared'])
        return True
    except (db_space.LowDiskSpace, sqlite3.OperationalError) as exc:
        conn.rollback()
        code = getattr(exc, 'sqlite_errorcode', sqlite3.SQLITE_BUSY) & 255
        if isinstance(exc, sqlite3.OperationalError) and code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_FULL):
            raise
        retryable = isinstance(exc, sqlite3.OperationalError) and code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        progress('waiting' if retryable else 'deferred')
        # SQLite rolls back an incomplete installation. Checkpoint completed work
        # before allowing ordinary writers to resume.
        try:
            _checkpoint(conn)
        except sqlite3.OperationalError:
            pass
        on_deferred(exc)
        log.warning('Automatic policy storage repair deferred; will retry next startup: %s', exc)
        return False
    finally:
        conn.set_progress_handler(None, 0)
        conn.execute(f'PRAGMA secure_delete={secure_delete}')
