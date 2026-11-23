"""Disposable database snapshots for maintenance, with guarded publication.

File-backed readers use their own connection and WAL snapshot. They only take the serving
connection's lock to capture input revisions and publish, never during calculation.
"""
from contextlib import contextmanager
import logging

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.core.db_checkpoint import checkpoint_wal, deferred_autocheckpoint
from yt_playlist.core.db_diagnostics import PublicationTimings
from yt_playlist.core.db_revisions import DERIVED_TABLES, Reads
from yt_playlist.core.db_writes import reserved_write
from yt_playlist.core.read_memo import ReadMemo, preparation_memo

log = logging.getLogger(__name__)


class SnapshotBusy(RuntimeError):
    """A foreground reader can use committed data without revision tracking."""


@contextmanager
def _capture_lock(source, wait):
    if not source._lock.acquire(blocking=wait):
        raise SnapshotBusy('The serving writer is busy')
    try:
        yield
    finally:
        source._lock.release()


class ReadSnapshot:
    def __init__(self, source, reader, changes, version, revisions, generation, revision_scope=None, *,
                 allow_stale_keys=frozenset()):
        self.source = source
        self.store = reader
        self._changes = changes
        self._version = version
        self._revisions = revisions
        self._schema = revisions.schema_version
        self._revision_scope = revision_scope
        self._allow_stale_keys = frozenset(allow_stale_keys)
        self._reader_released = False
        self._database_generation = generation

    def _same_admission(self):
        return (getattr(self.source, '_database_generation', 0) == self._database_generation
                and self.source._snapshot_revisions is self._revisions)

    def release_reader(self):
        """Retain the publication guard without retaining a connection or pinned WAL view."""
        if not self._reader_released:
            self.store.conn.rollback()
            self.store._memoized_read = None
            self.store.conn.close()
            self._reader_released = True

    def publish(self, target, *, defer_checkpoint=True, allow_stale_tables=frozenset(),
                validate_inputs=True):
        """Publish atomically, then checkpoint outside the serving lock for every projection.

        ``defer_checkpoint=False`` retains SQLite's inline policy for diagnostic comparisons.
        Disposable rankings may explicitly tolerate changes to soft catalog/evidence inputs.
        Their serving path must recheck current eligibility. Models use the strict default;
        external writers and database readmission invalidate ordinary publication.
        Captured recommendation outputs may set ``validate_inputs=False``:
        source updates then belong to a later run. Database readmission still
        invalidates a capture from the previous admission.
        """
        timings = PublicationTimings()
        checkpoint = False
        published = False
        outcome = 'failed'
        if not self._reader_released:
            self.store.conn.rollback()  # release the read snapshot before reserving the writer
        try:
            with reserved_write(self.source, timings=timings):
                check_cancellable_work()
                changes = self.source.conn.total_changes
                with deferred_autocheckpoint(self.source.conn,
                        defer_checkpoint and self.source._db_path is not None) as checkpoint:
                    published = self._publish_locked(target, timings, allow_stale_tables, validate_inputs)
                checkpoint = checkpoint and self.source.conn.total_changes != changes
            outcome = 'published' if published else 'deferred'
            return published
        except (WorkClosing, WorkBudgetExceeded):
            outcome = 'cancelled'
            raise
        except Exception:
            self.source.maintenance_diagnostics.record('failed')
            raise
        finally:
            try:
                if checkpoint and published:
                    with timings.measure('checkpoint'):
                        checkpoint_wal(self.source._db_path)
            finally:
                timings.report(outcome)

    def is_current(self, *, allow_stale_tables=frozenset()):
        """Cheaply discard deferred work; final publication still rechecks under its writer."""
        with self.source._lock:
            return self._inputs_current(PublicationTimings(), allow_stale_tables)

    def publish_ordered(self, target, *, allow_stale_tables=frozenset(), validate_inputs=True):
        """Check captured inputs once, then let each repository commit its own write.

        The serving lock preserves application ordering. A failed later step
        leaves earlier commits intact; the coordinator acknowledges queued work
        last so the next attempt can finish it. Captures that skip input checks
        still verify that the schema they publish into matches their admission.
        """
        timings = PublicationTimings()
        outcome = 'failed'
        checkpoint = False
        self.release_reader()
        try:
            with timings.locked(self.source._lock):
                if self.source.conn.in_transaction:
                    raise RuntimeError('Ordered publication cannot join another transaction')
                check_cancellable_work()
                current = (self._inputs_current(timings, allow_stale_tables) if validate_inputs else
                           self._same_admission())
                if current and not validate_inputs:
                    # DDL can change the destination without rebinding the Store.
                    current = self.source.conn.execute('PRAGMA main.schema_version').fetchone()[0] == self._schema
                    if not current:
                        self.source.maintenance_diagnostics.record('deferred', reasons={'schema'})
                if not current:
                    outcome = 'deferred'
                    return False
                changes = self.source.conn.total_changes
                with deferred_autocheckpoint(self.source.conn, self.source._db_path is not None) as checkpoint:
                    try:
                        with timings.measure('writes'):
                            target()
                    finally:
                        checkpoint = checkpoint and self.source.conn.total_changes != changes
                self.source.maintenance_diagnostics.record('published')
                outcome = 'published'
                return True
        except (WorkClosing, WorkBudgetExceeded):
            outcome = 'cancelled'
            raise
        except Exception:
            self.source.maintenance_diagnostics.record('failed')
            raise
        finally:
            try:
                if checkpoint:
                    with timings.measure('checkpoint'):
                        checkpoint_wal(self.source._db_path)
            finally:
                timings.report(outcome)

    def _inputs_current(self, timings, allow_stale_tables):
        """Check the original guard even after its reader has been released.

        Only writes to tables/settings actually read invalidate this connection's work. External
        writers remain conservative. The writer reservation closes the check/write race.
        ``target`` must only write prepared results, without doing further calculation or I/O.
        """
        if not self._same_admission():
            return False
        conn = self.source.conn
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        dependencies = self.store._snapshot_reads.dependencies
        tracked = self._revisions.tables
        guarded = {key for key in dependencies if key[0] in tracked
                   and key[0] not in allow_stale_tables}
        if self._revision_scope is not None and guarded - self._revision_scope:
            raise RuntimeError('Snapshot consumed inputs outside its declared revision scope')
        # Memoized reads retain all dependencies. An unkeyed read of the same
        # table also stays guarded when a specific key is allowed to go stale.
        guarded.difference_update(self._allow_stale_keys)
        with timings.measure('revisions'):
            current = self._revisions.capture(guarded)
        # View reads report their underlying tables in the admitted schema.
        unknown = ({table for table, _key in dependencies
                    if not table.startswith("sqlite_")} - tracked
                   - self._revisions.views - DERIVED_TABLES)
        changed = sorted(key for key in guarded
                         if current.get(key, 0) > self._changes)
        if (version != self._version or changed
                or (unknown and conn.total_changes != self._changes)):
            log.debug('Maintenance publication deferred: changed=%s external_writer=%s unknown=%s',
                     changed[:12], version != self._version, sorted(unknown)[:8])
            reasons = {'source:' + table for table, _key in changed}
            if version != self._version:
                reasons.add('external_writer')
            if unknown and conn.total_changes != self._changes:
                reasons.add('untracked_source')
            self.source.maintenance_diagnostics.record('deferred', reasons=reasons)
            return False
        return True

    def _publish_locked(self, target, timings, allow_stale_tables, validate_inputs=True):
        conn = self.source.conn
        try:
            current = (self._inputs_current(timings, allow_stale_tables) if validate_inputs else
                       self._same_admission())
            if not current:
                conn.rollback()
                return False
            with timings.measure('writes'):
                target()
            check_cancellable_work()
            with timings.measure('commit'):
                conn.commit()
            self.source.maintenance_diagnostics.record('published')
            return True
        except BaseException:
            conn.rollback()
            raise


@contextmanager
def read_snapshot(source, make_reader, *, dependencies=None, memo=None, wait_for_writer=True,
                  read_transaction=True, allow_stale_keys=frozenset()):
    # A bounded caller may declare all inputs up front. Publication rejects any
    # consumed input outside this scope, including a previously missing row.
    revision_scope = frozenset(dependencies) if dependencies is not None else None
    reader = make_reader()
    try:
        # Pin the committed inputs and their connection-local revisions together. This boundary
        # only reads counters and starts a reader; all substantive SQL runs after releasing it.
        with _capture_lock(source, wait_for_writer):
            check_cancellable_work()
            if source.conn.in_transaction:
                raise RuntimeError("Cannot snapshot uncommitted changes")
            changes = source.conn.total_changes
            revisions = source._snapshot_revisions
            version = source.conn.execute("PRAGMA data_version").fetchone()[0]
            generation = getattr(source, '_database_generation', 0)
            if source._db_path is None:
                source._copy_memory_to(reader)
            else:
                if read_transaction and not reader.conn.in_transaction:
                    reader.conn.execute("BEGIN")
                reader.conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
            # External commits can race reader pinning. Only reuse results when
            # the external-writer epoch stayed stable across that boundary.
            committed = not read_transaction and source._db_path is not None
            reader_version = reader.conn.execute("PRAGMA data_version").fetchone()[0] if committed else None
            stable = source.conn.execute("PRAGMA data_version").fetchone()[0] == version
            if memo is None:
                if not hasattr(source, "_read_memo"):
                    source._read_memo = ReadMemo()
                memo = source._read_memo
        reader._snapshot_reads = reader.conn._snapshot_reads = Reads()

        def cache_current():
            # A committed reader follows writes after admission. Values computed
            # across those commits cannot enter the memo under the old revision.
            return reader.conn.execute("PRAGMA data_version").fetchone()[0] == reader_version

        def unchanged(dependencies, serial):
            guarded = {key for key in dependencies if key[0] in revisions.tables}
            # Recomputing on the pinned reader is always safe. Foreground cache
            # validation must not wait behind unrelated preparation writes.
            if not source._lock.acquire(blocking=wait_for_writer):
                return False
            try:
                check_cancellable_work()
                if source._snapshot_revisions is not revisions:
                    return False
                current = revisions.capture(guarded)
            finally:
                source._lock.release()
            return all(revision <= serial for revision in current.values())

        def calculate(name, prepare, owner=None):
            selected = (preparation_memo(source, owner, wait_for_writer=wait_for_writer)
                        if owner is not None and read_transaction else None)
            epoch = ((generation, version, revisions) if stable and revision_scope is None
                     and (not committed or cache_current()) else None)
            return (selected or memo).calculate(
                name, prepare, epoch=epoch,
                serial=changes, unchanged=unchanged,
                reads=reader._snapshot_reads, tables=revisions.tables, views=revisions.views,
                cache_current=cache_current if committed else None)

        reader._memoized_read = calculate
        # SQLite 3.40 reports schema writes while first declaring these read-only
        # modules. Prepare them before installing the write-denying authorizer.
        # Qualifying with temp keeps real tables in main from shadowing them.
        reader.conn.execute("SELECT 1 FROM temp.json_each('[]'), temp.json_tree('[]') LIMIT 0").fetchall()
        reader.conn.set_authorizer(reader._snapshot_reads.authorize)
        yield ReadSnapshot(source, reader, changes, version, revisions, generation, revision_scope,
                           allow_stale_keys=allow_stale_keys)
    finally:
        # Drop the callback's reader references promptly; repositories also point
        # back to the reader and can otherwise delay collection of a closed Store.
        reader._memoized_read = None
        reader.conn.close()
