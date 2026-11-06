"""Bounded schema installation and the current retained-copy lifecycle."""
import errno
import logging
import sqlite3
import threading

from yt_playlist.core import db_space
from yt_playlist.core.db_space import StorageCompacting
from yt_playlist.core.db_diagnostics import DiagnosticConnection

log = logging.getLogger(__name__)
OPTIMIZING_BANNER = 'Optimizing app storage; some recommendations are temporarily unavailable.'
LISTENING_MESSAGE = ('Browsing, playback, likes and Next are available. '
                     'Playback feedback is saved locally; history and recommendation updates are delayed. '
                     'Playlist changes, syncing, imports and settings changes are paused. '
                     'New recommendation impressions are not recorded. '
                     'Completed audio uploads retry; sampling may miss points.')
MESSAGE = 'Compacting storage. ' + LISTENING_MESSAGE
REBUILD_MESSAGE = 'Updating library storage. ' + LISTENING_MESSAGE
EVIDENCE_MESSAGE = 'Listening-history cleanup is continuing in the background. Your library remains available.'
EVIDENCE_RETRY_MESSAGE = 'Listening-history cleanup needs another attempt. Your library remains available.'
SCHEMA_RETRY_MESSAGE = ('A storage update needs another attempt. Your library remains available. '
                        'Retry the schema update from the maintenance command.')
COPY_DEFERRED_MESSAGE = 'Library preparation stopped. Activity recording has resumed.'
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
                if previous.get('analytics_pending') and (previous.get('phase') == 'complete'
                        or previous.get('analytics_complete') or previous.get('preparation')):
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
        evidence = (bool(getattr(self.store, '_evidence_maintenance', 0))
                    or getattr(self.store, '_evidence_recovery_pending', False))
        pending = self.store.playback_feedback.pending()
        rebuild_required = getattr(self.store, '_rebuild_source_version', None) == 64
        copy_active = self.active or bool(getattr(self.store,'_activity_paused',False))
        retiring = bool(getattr(self.store, '_recommendation_retirement_pending', False))
        # Ordinary incremental retention remains writable. The priority
        # recommendation drain deliberately pauses app mutations until done.
        active = copy_active or rebuild_required or retiring
        retrying = evidence and getattr(self.store, '_evidence_recovery_failed', False)
        if copy_active:
            phase = self.phase
            message = (self.message if phase == 'recovery_required' else
                       REBUILD_MESSAGE if self._rebuilding else MESSAGE)
        elif retiring:
            phase = 'recommendation_retirement'
            message = 'Cleaning up old recommendations. ' + LISTENING_MESSAGE
        elif evidence:
            phase = getattr(self.store, '_evidence_phase', 'listening_history')
            message = EVIDENCE_RETRY_MESSAGE if retrying else EVIDENCE_MESSAGE
        elif rebuild_required:
            phase = 'rebuild_required'
            message = 'Your library remains available while its storage update is pending. ' + LISTENING_MESSAGE
        elif pending:
            phase = 'playback_feedback'
            message = 'Applying saved playback feedback. Your library remains available.'
        else:
            phase = self.phase
            message = SCHEMA_RETRY_MESSAGE if phase == 'schema_retry' else self.message
        detail = {'inventory': 'Checking saved recommendation contexts.',
                  'checkpoint': 'Checkpointing retained listening evidence.',
                  'retention': 'Reclaiming acknowledged diagnostic storage.'}.get(phase)
        if evidence and detail:
            message += ' ' + detail
        if pending:
            message += f' {pending} saved playback observations await replay.'
        warning = bool(retrying or phase == 'schema_retry' or rebuild_required and self.error
                       or not active and self.phase == 'deferred')
        # Technical failures stay in diagnostics. Normal useful preparation
        # can retain its notice, without turning recovery into a global alert.
        banner_message = ''
        if active and not warning and phase != 'recovery_required':
            banner_message = OPTIMIZING_BANNER
        elif not warning and phase != 'recovery_required' and self.report and self.report.get('analytics_pending'):
            banner_message = OPTIMIZING_BANNER
            message = 'Your library is available while historical data is prepared in the background.'
        return dict(active=active, phase=phase, message=message, pending_feedback=pending,
                    recording=not active, capturing_feedback=True,
                    warning=warning, banner_message=banner_message,
                    progress=dict(getattr(self.store, '_recommendation_retirement_progress', {})) if retiring else dict(self.detail),
                    error=getattr(self.store, '_recommendation_retirement_error', '') if retiring else self.error,
                    evidence_maintenance=dict(pending=bool(evidence),
                        running=bool(getattr(self.store, '_evidence_maintenance', 0)),
                        phase=getattr(self.store, '_evidence_phase', 'inventory'), retrying=bool(retrying),
                        progress=dict(getattr(self.store, '_evidence_progress', {}))))

    def start(self):
        if self.store._db_path is None or self._thread is not None:
            return
        with self.store._lock:
            from . import db_handles
            pending = db_handles.state(self.store._db_path) or {}
            if pending.get('analytics_pending') and (pending.get('phase') == 'complete'
                    or pending.get('analytics_complete') or pending.get('preparation')):
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
            # Supported installed formats keep their current owners writable.
            # Bulk work is selected above or requested explicitly through copy.
            self.phase = 'done'

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

    def _progress(self, phase, detail=None):
        self.phase = phase
        self.detail = detail or {}

    def _deferred(self, error):
        low_space = (isinstance(error, db_space.LowDiskSpace)
                     or getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL
                     or isinstance(error, OSError) and error.errno in
                     (errno.ENOSPC, getattr(errno, 'EDQUOT', errno.ENOSPC)))
        self.message = COPY_LOW_SPACE_MESSAGE if low_space else COPY_DEFERRED_MESSAGE

    def stop(self):
        self._stop.set()
        with self._conn_lock:
            if self._conn is not None:
                self._conn.interrupt()

    def join(self, timeout=30):
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError('Storage maintenance did not stop: '
                                   f'phase={self.phase!r}, progress={self.detail!r}, error={self.error!r}')
