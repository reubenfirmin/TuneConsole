"""Background startup repair with read availability and explicit recording pause."""
import errno
import json
import logging
import sqlite3
import threading

from yt_playlist.core import db_space, policy_upgrade
from yt_playlist.core.db_space import StorageCompacting
from yt_playlist.core.db_diagnostics import DiagnosticConnection

log = logging.getLogger(__name__)
MESSAGE = "Compacting storage - won't record activity until done"
DEFERRED_MESSAGE = ('Storage compaction did not finish. Activity recording has resumed. '
                    'Compaction will retry on the next launch.')
LOW_SPACE_MESSAGE = ('Storage compaction blocked: not enough free disk space. '
                     'Storage cleanup is incomplete. Activity recording has resumed. '
                     'Compaction will retry on the next launch.')


class StorageMaintenance:
    def __init__(self, store, *, on_resume=lambda: None):
        self.store = store
        self.on_resume = on_resume
        self.active = False
        self.phase = 'idle'
        self.message = ''
        self.ready = threading.Event()
        self.ready.set()
        self._stop = threading.Event()
        self._thread = None
        self._conn = None
        self._conn_lock = threading.Lock()

    def status(self):
        return dict(active=self.active, phase=self.phase, message=MESSAGE if self.active else self.message,
                    recording=not self.active, warning=not self.active and self.phase == 'deferred')

    def start(self):
        if self.store._db_path is None or self._thread is not None:
            return
        with self.store._lock:
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

    def _resume(self):
        try:
            with self.store._lock:
                self.store.conn.rollback()
                self.store.conn.execute(f'PRAGMA query_only={self._query_only}')
                self.store.conn._storage_compacting = False
                self.store.activity_paused = False
                self.active = False
                try:
                    self.on_resume()
                except Exception:
                    log.warning('Could not retain current playback provenance after storage repair', exc_info=True)
        finally:
            self.ready.set()

    def _progress(self, phase):
        self.phase = phase

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
