"""Atomic release judgments and their durable listening-response intents."""
from contextlib import contextmanager, nullcontext
import json
import sqlite3
import uuid

from yt_playlist.core import album_feedback_outbox, album_feedback_storage
from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from yt_playlist.core.db_revisions import setting_reads
from yt_playlist.core.db_writes import atomic_write, reserved_write
from yt_playlist.repos.base import Repo, snapshot_read


def _capture(conn, previous, action, opportunity_id, browse, kind, now):
    withdrawn = set()
    for row in previous:
        row = dict(row)
        opportunity, original_action = row.get('opportunity_id'), row.get('action_id')
        if not opportunity or not original_action or (opportunity, original_action) in withdrawn:
            continue
        withdrawal = uuid.uuid5(uuid.NAMESPACE_URL, 'album-feedback-withdrawal:' + str(original_action)).hex
        album_feedback_outbox.enqueue(conn, withdrawal, opportunity, 'withdraw', now,
                                      target_action_id=original_action)
        withdrawn.add((opportunity, original_action))
    if action and opportunity_id:
        album_feedback_outbox.enqueue(conn, action, opportunity_id, 'feedback', now,
                                      option_id=browse, feedback_kind=kind)


class AlbumFeedbackRepo(Repo):
    @contextmanager
    def protection(self, capture=None):
        """Hold capture fences across the final durable-pin check and deletion."""
        with album_feedback_outbox.fence(self._store, wait_seconds=0):
            guard = capture.protection() if capture is not None else nullcontext(((), ()))
            with guard as (artifacts, opportunities):
                try:
                    # A joined capture may still be uncommitted on another
                    # connection. Reserve the writer to detect it, then release
                    # SQL ownership before deleting files under the fences.
                    with reserved_write(self._store, wait_seconds=0):
                        durable = ({row[0] for row in self.conn.execute(
                            'SELECT DISTINCT opportunity_id FROM '
                            f'{album_feedback_outbox.TABLE} WHERE delivered=0')}
                            if album_feedback_outbox.available(self.conn) else set())
                except sqlite3.OperationalError as exc:
                    if getattr(exc, 'sqlite_errorcode', 0) & 255 not in (
                            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    raise WorkBudgetExceeded('Album feedback capture has not committed') from exc
                yield artifacts, durable | set(opportunities)

    @snapshot_read
    def original(self):
        with setting_reads(self._store, ('album_feedback',)):
            return album_feedback_storage.original(self.conn)

    def replace(self, browse, kind, now, opportunity_id=None, *, account_id=None):
        with album_feedback_outbox.fence(self._store), atomic_write(self._store):
            # Deferred admission may precede the regular, bounded outbox migration.
            album_feedback_outbox.install(self.conn)
            if album_feedback_storage.available(self.conn):
                with self.conn:
                    previous, action = album_feedback_storage.replace(self.conn, browse, kind, now,
                        opportunity_id=opportunity_id, account_id=account_id)
            else:
                original = json.loads(self._store.settings.get_setting('album_feedback') or '{}')
                preferred = self._store.collection.preferred_album_ids(set(original) | {browse})
                previous = []
                for source in list(original):
                    if preferred[source] == preferred[browse]:
                        previous.append(original.pop(source))
                action = uuid.uuid4().hex if kind != 'clear' else None
                if action:
                    original[browse] = {'kind': kind, 'at': now,
                                       'opportunity_id': opportunity_id, 'action_id': action}
                self._store.settings.set_setting('album_feedback', json.dumps(original))
            _capture(self.conn, previous, action, opportunity_id, browse, kind, now)

    def pending_responses(self, *, after, limit):
        """Yield a detached prefix only if the local writer is immediately free."""
        if not self._lock.acquire(blocking=False):
            return []
        try:
            check_cancellable_work()
            if self.conn.in_transaction:
                return []
            rows = album_feedback_outbox.pending(self.conn, after=after, limit=limit)
            if not rows and after:
                rows = album_feedback_outbox.pending(self.conn, limit=limit)
            return [dict(row) for row in rows]
        finally:
            self._lock.release()

    def acknowledge_response(self, action_id):
        with atomic_write(self._store, wait_seconds=0):
            album_feedback_outbox.acknowledge(self.conn, action_id)
