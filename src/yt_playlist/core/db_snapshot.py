"""Disposable database snapshots for maintenance, with guarded publication.

File-backed readers use their own connection and WAL snapshot. They only take the serving
connection's lock to capture input revisions and publish, never during calculation.
"""
from contextlib import contextmanager
import logging

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.core.db_checkpoint import checkpoint_wal, deferred_autocheckpoint
from yt_playlist.core.db_diagnostics import PublicationTimings
from yt_playlist.core.db_revisions import Reads
from yt_playlist.core.read_memo import ReadMemo

log = logging.getLogger(__name__)


class ReadSnapshot:
    def __init__(self, source, reader, changes, version, schema, revision_scope=None):
        self.source = source
        self.store = reader
        self._changes = changes
        self._version = version
        self._schema = schema
        self._revision_scope = revision_scope
        self._reader_released = False

    def release_reader(self):
        """Retain the publication guard without retaining a connection or pinned WAL view."""
        if not self._reader_released:
            self.store.conn.rollback()
            self.store._memoized_read = None
            self.store.conn.close()
            self._reader_released = True

    def publish(self, target, *, defer_checkpoint=True, allow_stale_tables=frozenset()):
        """Publish atomically, then checkpoint outside the serving lock for every projection.

        ``defer_checkpoint=False`` retains SQLite's inline policy for diagnostic comparisons.
        Disposable rankings may explicitly tolerate changes to soft catalog/evidence inputs.
        Their serving path must recheck current eligibility. Models use the strict default;
        external writers and schema changes always invalidate publication.
        """
        timings = PublicationTimings()
        checkpoint = False
        published = False
        outcome = 'failed'
        if not self._reader_released:
            self.store.conn.rollback()  # release the read snapshot before reserving the writer
        try:
            with timings.locked(self.source._lock):
                check_cancellable_work()
                changes = self.source.conn.total_changes
                with deferred_autocheckpoint(self.source.conn,
                        defer_checkpoint and self.source._db_path is not None) as checkpoint:
                    published = self._publish_locked(target, timings, allow_stale_tables)
                checkpoint = checkpoint and self.source.conn.total_changes != changes
            outcome = 'published' if published else 'deferred'
            return published
        except (WorkClosing, WorkBudgetExceeded):
            outcome = 'cancelled'
            raise
        finally:
            try:
                if checkpoint and published:
                    with timings.measure('checkpoint'):
                        checkpoint_wal(self.source._db_path)
            finally:
                timings.report(outcome)

    def _publish_locked(self, target, timings, allow_stale_tables):
        """Publish only while guarded inputs still match the snapshot.

        Only writes to tables/settings actually read invalidate this connection's work. External
        writers remain conservative. The writer reservation closes the check/write race.
        ``target`` must only write prepared results, without doing further calculation or I/O.
        """
        conn = self.source.conn
        if conn.in_transaction:
            raise RuntimeError("Cannot publish maintenance inside another transaction")
        with timings.measure('reserve_writer'):
            conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA data_version").fetchone()[0]
            schema = conn.execute("PRAGMA schema_version").fetchone()[0]
            dependencies = self.store._snapshot_reads.dependencies
            tracked = self.source._snapshot_revisions.tables
            guarded = {key for key in dependencies if key[0] in tracked
                       and key[0] not in allow_stale_tables}
            if self._revision_scope is not None and guarded - self._revision_scope:
                raise RuntimeError('Snapshot consumed inputs outside its declared revision scope')
            with timings.measure('revisions'):
                current = self.source._snapshot_revisions.capture(guarded)
            # View reads also report their underlying tables. Definitions are protected by
            # schema_version; a view itself does not have a row revision.
            unknown = ({table for table, _key in dependencies
                        if not table.startswith("sqlite_")} - tracked
                       - self.source._snapshot_revisions.views)
            changed = sorted(key for key in guarded
                             if current.get(key, 0) > self._changes)
            if (schema != self._schema or version != self._version or changed
                    or (unknown and conn.total_changes != self._changes)):
                conn.rollback()
                log.debug('Maintenance publication deferred: changed=%s external_writer=%s schema=%s unknown=%s',
                         changed[:12], version != self._version, schema != self._schema, sorted(unknown)[:8])
                reasons = {'source:' + table for table, _key in changed}
                if version != self._version:
                    reasons.add('external_writer')
                if schema != self._schema:
                    reasons.add('schema')
                if unknown and conn.total_changes != self._changes:
                    reasons.add('untracked_source')
                self.source.maintenance_diagnostics.record('deferred', reasons=reasons)
                return False
            with timings.measure('writes'):
                target()
            check_cancellable_work()
            with timings.measure('commit'):
                conn.commit()
            self.source.maintenance_diagnostics.record('published')
            return True
        except BaseException as exc:
            conn.rollback()
            if isinstance(exc, Exception) and not isinstance(exc, (WorkClosing, WorkBudgetExceeded)):
                self.source.maintenance_diagnostics.record('failed')
            raise


@contextmanager
def read_snapshot(source, make_reader, *, dependencies=None):
    # A bounded caller may declare all inputs up front. Publication rejects any
    # consumed input outside this scope, including a previously missing row.
    revision_scope = frozenset(dependencies) if dependencies is not None else None
    reader = make_reader()
    try:
        # Pin the committed inputs and their connection-local revisions together. This boundary
        # only reads counters and starts a reader; all substantive SQL runs after releasing it.
        with source._lock:
            check_cancellable_work()
            if source.conn.in_transaction:
                raise RuntimeError("Cannot snapshot uncommitted changes")
            changes = source.conn.total_changes
            schema = source.conn.execute("PRAGMA schema_version").fetchone()[0]
            version = source.conn.execute("PRAGMA data_version").fetchone()[0]
            if source._db_path is None:
                source.conn.backup(reader.conn, pages=128,
                                   progress=lambda *_: check_cancellable_work())
            else:
                reader.conn.execute("BEGIN")
                reader.conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
            # External commits can race reader pinning. Only reuse results when
            # the external-writer epoch stayed stable across that boundary.
            stable = source.conn.execute("PRAGMA data_version").fetchone()[0] == version
            if not hasattr(source, "_read_memo"):
                source._read_memo = ReadMemo()
            memo = source._read_memo
        reader._snapshot_reads = Reads()

        def unchanged(dependencies, serial):
            guarded = {key for key in dependencies if key[0] in source._snapshot_revisions.tables}
            with source._lock:
                check_cancellable_work()
                current = source._snapshot_revisions.capture(guarded)
            return all(revision <= serial for revision in current.values())

        reader._memoized_read = lambda name, prepare: memo.calculate(
            name, prepare, epoch=(version, schema) if stable and revision_scope is None else None,
            serial=changes, unchanged=unchanged,
            reads=reader._snapshot_reads, tables=source._snapshot_revisions.tables,
            views=source._snapshot_revisions.views)
        reader.conn.set_authorizer(reader._snapshot_reads.authorize)
        yield ReadSnapshot(source, reader, changes, version, schema, revision_scope)
    finally:
        # Drop the callback's reader references promptly; repositories also point
        # back to the reader and can otherwise delay collection of a closed Store.
        reader._memoized_read = None
        reader.conn.close()
