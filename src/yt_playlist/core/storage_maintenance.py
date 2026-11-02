"""Bounded schema installation and explicitly separate legacy storage repair."""
import errno
import json
import logging
import sqlite3
import threading

from yt_playlist.core import db_space, policy_upgrade
from yt_playlist.core.db_space import StorageCompacting
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.repos import policy_storage

log = logging.getLogger(__name__)
LISTENING_MESSAGE = ('Browsing, playback, likes and Next are available. '
                     'Playback feedback is saved locally; history and recommendation updates are delayed. '
                     'Playlist changes, syncing, imports and settings changes are paused. '
                     'New recommendation impressions are not recorded. '
                     'Completed audio uploads retry; sampling may miss points.')
MESSAGE = 'Compacting storage. ' + LISTENING_MESSAGE
REBUILD_MESSAGE = 'Updating library storage. ' + LISTENING_MESSAGE
EVIDENCE_MESSAGE = ('Preparing listening-history storage. Browsing, playback, likes and Next are available. '
                    'Playback feedback is saved locally for replay; history and recommendation updates '
                    'are delayed. Playlist changes, syncing, imports and settings changes are paused. '
                    'New recommendation impressions are not recorded. '
                    'Completed audio uploads retry; sampling may miss points.')
EVIDENCE_RETRY_MESSAGE = ('Listening-history storage preparation needs another attempt. Browsing, playback, '
                          'likes and Next are available. Playback feedback is saved locally for replay; '
                          'history and recommendation updates are delayed. '
                          'Playlist changes, syncing, imports and settings changes are paused. '
                          'New recommendation impressions are not recorded. '
                          'Completed audio uploads retry; sampling may miss points.')
DEFERRED_MESSAGE = ('Storage compaction did not finish. Activity recording has resumed. '
                    'Compaction will retry on the next launch.')
LOW_SPACE_MESSAGE = ('Storage compaction blocked: not enough free disk space. '
                     'Storage cleanup is incomplete. Activity recording has resumed. '
                     'Compaction will retry on the next launch.')
SCHEMA_RETRY_MESSAGE = ('A storage update needs another attempt. Your library remains available. '
                        'Retry the schema update from the maintenance command.')
COPY_DEFERRED_MESSAGE = ('The storage update did not finish. Activity recording has resumed. '
                         'Resolve the reported problem before requesting another attempt.')
COPY_LOW_SPACE_MESSAGE = ('The storage update needs more disk space. Activity recording has resumed. '
                          'Free space or choose another scratch directory before requesting another attempt.')


class StorageMaintenance:
    def __init__(self, store, *, on_resume=lambda: None):
        self.store = store
        self.on_resume = on_resume
        self.active = False
        self.phase = 'idle'
        self.detail = {}
        self.error = ''
        self.message = ''
        self.ready = threading.Event()
        self.ready.set()
        self._stop = threading.Event()
        self._thread = None
        self._conn = None
        self._conn_lock = threading.Lock()
        self.report = None
        self._rebuilding = False
        if getattr(store, '_rebuild_source_version', None) == 64:
            self.ready.clear()

    def start_compaction(self, *, scratch=None):
        """Explicit retained-copy operation, independent of schema preparation."""
        self._start_copy(destination=None, rebuild=False, resume=False, scratch=scratch)

    def start_rebuild(self, destination=None, *, resume=False, scratch=None):
        """Run a schema owner's direct destination through the shared worker."""
        if resume and scratch is not None:
            raise ValueError('Resume reuses the recorded scratch directory')
        self._start_copy(destination=destination, rebuild=True, resume=resume, scratch=scratch)

    def _start_copy(self, *, destination, rebuild, resume, scratch):
        with self.store._lock:
            if self.active or self._thread is not None and self._thread.is_alive():
                raise StorageCompacting()
            self._stop.clear()
            self.active = True
            self.phase = 'checking'
            self.detail = {}
            self.error = ''
            self.report = None
            self._rebuilding = rebuild
            self.message = ''
            self.ready.clear()
            self._thread = threading.Thread(target=self._run_copy,
                kwargs={'destination':destination, 'rebuild':rebuild, 'resume':resume, 'scratch':scratch},
                name='database-rebuild' if self._rebuilding else 'copy-compaction', daemon=True)
            try:
                self._thread.start()
            except BaseException:
                self.active = False
                self.ready.set()
                self._thread = None
                raise

    def _run_copy(self, *, destination, rebuild, resume, scratch):
        from . import playback_feedback
        from .db_rebuild import build, job
        from .activity import cancellable_work
        try:
            with cancellable_work(self._stop.is_set):
                if resume is None:
                    from . import db_handles
                    from .db_rebuild.destination import signature
                    from pathlib import Path
                    previous = db_handles.state(self.store._db_path) or {}
                    resume = bool(previous.get('phase') == 'failed' and previous.get('failed_phase') == 'building'
                        and previous.get('operation') == 'rebuild' and previous.get('source_identity')
                        and previous.get('destination_version') == signature()
                        and (Path(previous['work'])/'copy.db').is_file()
                        and previous['source_identity'] == build._source_identity(self.store._db_path))
                from . import db_handles
                previous = db_handles.state(self.store._db_path) or {}
                if previous.get('phase') == 'complete' and previous.get('analytics_pending'):
                    self.report = previous
                else:
                    self.report = job.run(self.store,destination=destination,rebuild=rebuild,resume=resume,scratch=scratch,
                        progress=self._progress,cancelled=self._stop.is_set)
                if self.report.get('analytics_pending'):
                    # Runtime cutover has completed. Requests may read/write,
                    # while ready continues holding the workers at startup.
                    self.active = False
                    self.on_resume()
                    playback_feedback.try_drain(self.store)
                    self.report = job.finish_analytics(self.store,progress=self._progress,cancelled=self._stop.is_set)
            self.phase = 'done'
        except Exception as error:
            self.phase = 'deferred'
            self.error = repr(error)
            self._deferred(error)
            self.message = COPY_LOW_SPACE_MESSAGE if self.message==LOW_SPACE_MESSAGE else COPY_DEFERRED_MESSAGE
            log.warning('Database copy stopped; inspect durable recovery state',
                        exc_info=not self._stop.is_set())
        finally:
            self.active = bool(getattr(self.store, '_activity_paused', False))
            if self.active:
                self.phase = 'recovery_required'
                self.message = 'Storage recovery is required before recording can resume. Preserve the recovery files and restart.'
            try:
                if not self.store.activity_paused:
                    self.on_resume()
                    playback_feedback.try_drain(self.store)
            except Exception:
                log.warning('Playback feedback or provenance awaits retry after database copy',exc_info=True)
            pending = db_handles.state(self.store._db_path) or {}
            if not self.store.activity_paused and not pending.get('analytics_pending'):
                self.ready.set()

    def status(self):
        from yt_playlist.core import playback_feedback
        evidence = (bool(getattr(self.store, '_evidence_maintenance', 0))
                    or getattr(self.store, '_evidence_recovery_pending', False))
        pending = playback_feedback.inbox(self.store).pending()
        rebuild_required = getattr(self.store, '_rebuild_source_version', None) == 64
        active = self.active or evidence or bool(pending) or rebuild_required
        retrying = evidence and getattr(self.store, '_evidence_recovery_failed', False)
        phase = (self.phase if self.active else getattr(self.store, '_evidence_phase', 'listening_history')
                 if evidence else 'rebuild_required' if rebuild_required else 'playback_feedback' if pending else self.phase)
        message = (self.message if self.active and self.phase=='recovery_required' else
                   (REBUILD_MESSAGE if self._rebuilding else MESSAGE) if self.active else EVIDENCE_RETRY_MESSAGE if retrying else EVIDENCE_MESSAGE
                   if evidence else 'Your library remains available while its storage update is pending. '+LISTENING_MESSAGE
                   if rebuild_required else 'Applying saved playback feedback. Playback, likes and Next are available; '
                   'history and recommendation updates are delayed. '
                   'Playlist changes, syncing, imports and settings changes are paused.' if pending
                   else SCHEMA_RETRY_MESSAGE if phase == 'schema_retry' else self.message)
        detail = {'inventory': 'Checking saved recommendation contexts.',
                  'checkpoint': 'Checkpointing retained listening evidence.',
                  'retention': 'Reclaiming acknowledged diagnostic storage.'}.get(phase)
        if evidence and detail:
            message += ' ' + detail
        if pending:
            message += f' {pending} saved playback observations await replay.'
        warning = bool(retrying or phase == 'schema_retry' or rebuild_required and self.error
                       or not active and self.phase == 'deferred')
        # Keep the UI to one short sentence; message retains diagnostic detail
        # for API callers and rejected writes.
        banner_message = ''
        if active:
            banner_message = ('Library recovery is needed.' if phase=='recovery_required' else
                              'Library updates need attention.' if retrying else
                              'Catching up on your listening.' if not self.active and not evidence else
                              'Performing database maintenance - this should take just a few minutes.')
        elif warning:
            banner_message = ('Library update needs a retry.' if phase == 'schema_retry' else
                              'Free disk space to finish.' if self.message in {LOW_SPACE_MESSAGE,COPY_LOW_SPACE_MESSAGE} else
                              'Library cleanup needs a retry.' if self.message == COPY_DEFERRED_MESSAGE else
                              'Library update will retry.')
        return dict(active=active, phase=phase, message=message, pending_feedback=pending,
                    recording=not active, capturing_feedback=True,
                    warning=warning, banner_message=banner_message,
                    progress=dict(self.detail), error=self.error)

    def start(self):
        if self.store._db_path is None or self._thread is not None:
            return
        with self.store._lock:
            from . import db_handles
            pending = db_handles.state(self.store._db_path) or {}
            if pending.get('phase') == 'complete' and pending.get('analytics_pending'):
                self._start_copy(destination=None,rebuild=True,resume=False,scratch=None)
                return
            if self.store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] in (64,93):
                # Admission is constant-size. The worker selects and validates
                # the full source/target contract after fencing source writes.
                self._start_copy(destination=None,rebuild=True,resume=None,scratch=None)
                return
            from .migrations import regular
            if regular.validate(self.store.conn) < len(regular.CHANGES):
                # Only additive bounded DDL runs here. The job's connection never
                # borrows the serving lock, pauses writes or converts old data.
                self.phase = 'schema'
                self.ready.clear()
                self._thread = threading.Thread(target=self._run_schema, name='schema-maintenance', daemon=True)
                try:
                    self._thread.start()
                except BaseException:
                    self._thread = None
                    self.ready.set()
                    raise
                return
            if getattr(self.store, '_schema_compatibility', False):
                # The old policy sharing/conversion job also belongs to explicit
                # preparation. Its installed owners remain in ordinary use.
                self.phase = 'done'
                return
            # File-backed policy migration has retired the SQLite sharing job.
            # It must not pause recording or revisit a superseded repair cursor.
            if policy_storage.typed(self.store.conn):
                self.phase = 'done'
                return
            row = self.store.conn.execute('SELECT value FROM settings WHERE key=?', (policy_upgrade.KEY,)).fetchone()
            if row and json.loads(row[0]).get('phase') == 'done':
                self.phase = 'done'
                return
            self._query_only = self.store.conn.execute('PRAGMA query_only').fetchone()[0]
            self.store.conn.execute('PRAGMA query_only=ON')
            self.store.conn._storage_compacting = True
            self.store.activity_paused = True
            self.active = True
            self.phase = 'checking'
            self.ready.clear()
        self._thread = threading.Thread(target=self._run, name='storage-maintenance', daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._resume()
            raise

    def _run_schema(self):
        """Apply regular DDL once; bulk preparation has an explicit lifecycle."""
        from yt_playlist.core.activity import cancellable_work, interrupt_query
        from yt_playlist.core.migrations import regular
        try:
            conn = sqlite3.connect(self.store._db_path, timeout=.1, factory=DiagnosticConnection)
            try:
                with self._conn_lock:
                    self._conn = conn
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA foreign_keys=ON')
                conn.set_progress_handler(interrupt_query, 1000)
                with cancellable_work(self._stop.is_set):
                    regular.migrate(conn)
                self.phase = 'done'
            finally:
                with self._conn_lock:
                    self._conn = None
                conn.set_progress_handler(None, 0)
                conn.close()
        except Exception:
            if not self._stop.is_set():
                self.phase = 'schema_retry'
                log.warning('Bounded schema update deferred; installed owners remain writable', exc_info=True)
        finally:
            self.ready.set()

    def _resume(self):
        try:
            with self.store._lock:
                self.store.conn.rollback()
                self.store.conn.execute(f'PRAGMA query_only={self._query_only}')
                self.store.conn._storage_compacting = False
                self.store.activity_paused = False
                self.active = False
                try:
                    if not getattr(self.store, '_schema_recovery_pending', False):
                        self.on_resume()
                except Exception:
                    log.warning('Could not retain current playback provenance after storage repair', exc_info=True)
        finally:
            self.ready.set()

    def _progress(self, phase, detail=None):
        self.phase = phase
        self.detail = detail or {}

    def _deferred(self, error):
        low_space = (isinstance(error, db_space.LowDiskSpace)
                     or getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL
                     or isinstance(error, OSError) and error.errno in
                     (errno.ENOSPC, getattr(errno, 'EDQUOT', errno.ENOSPC)))
        self.message = LOW_SPACE_MESSAGE if low_space else DEFERRED_MESSAGE

    def _run(self):
        try:
            with sqlite3.connect(self.store._db_path, timeout=.1, factory=DiagnosticConnection) as conn:
                with self._conn_lock:
                    self._conn = conn
                conn.row_factory = sqlite3.Row
                conn._maintenance_cancelled = self._stop.is_set
                conn.execute('PRAGMA foreign_keys=ON')
                conn.set_progress_handler(lambda: int(self._stop.is_set()), 1000)
                complete = False
                for _ in range(120):
                    if self._stop.is_set():
                        break
                    conn.set_progress_handler(lambda: int(self._stop.is_set()), 1000)
                    complete = policy_upgrade.run_once(conn, progress=self._progress,
                                                       on_deferred=self._deferred)
                    if complete:
                        break
                    # A UI reader may temporarily pin the WAL. The repair checks
                    # that checkpoint before committing any additional batch.
                    if self.phase != 'waiting' or self._stop.wait(.5):
                        break
                self.phase = 'done' if complete else 'deferred'
                self.message = '' if complete else self.message or DEFERRED_MESSAGE
        except Exception as error:
            self.phase = 'deferred'
            self._deferred(error)
            log.warning('Background storage repair interrupted or deferred; committed progress retained',
                        exc_info=not self._stop.is_set())
        finally:
            try:
                with self._conn_lock:
                    conn, self._conn = self._conn, None
                    if conn is not None:
                        conn.set_progress_handler(None, 0)
                        conn.close()
            finally:
                self._resume()

    def stop(self):
        self._stop.set()
        with self._conn_lock:
            if self._conn is not None:
                self._conn.interrupt()

    def join(self, timeout=30):
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError('Storage maintenance did not stop')
