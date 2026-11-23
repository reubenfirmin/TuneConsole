"""Check that ordered content requests resume after the installed queue upgrade."""
from contextlib import closing
import json

from tests.library_image.fixtures import report_file
from yt_playlist.core import content_work
from yt_playlist.core.migrations import regular
from yt_playlist.core.store import Store


def test_pending_requests_keep_order_and_revisions(managed_library):
    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:199])
        orphan = 'upgrade-test:detached-recording'
        with conn.atomic():
            conn.execute('INSERT INTO rec_content_pending(identity_key,revision) VALUES (?,?)', (orphan, 5))
        original = list(map(tuple, conn.execute('SELECT identity_key,revision FROM rec_content_pending ORDER BY position')))
        assert len(original) > 1
        store.init_schema()
        assert content_work.pending(conn, -1) == original
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='rec_content_pending'").fetchone()
        assert [row[0] for row in conn.execute(f'SELECT key FROM settings WHERE {content_work.PENDING}')] == [
            content_work.PREFIX + orphan]
        owned = conn.execute('SELECT count(*) FROM recordings WHERE content_pending_position IS NOT NULL').fetchone()[0]
        assert owned == len(original) - 1
        store.prepared_content.rotate(original[:1])
        assert content_work.pending(conn, -1) == original[1:] + original[:1]
        before = store.prepared_content.repository
        before.acknowledge([(orphan, 4)])
        assert dict(content_work.pending(conn, -1))[orphan] == 5

    store = Store(managed_library)
    with closing(store.conn) as conn:
        store.init_schema()
        assert content_work.pending(conn, -1) == original[1:] + original[:1]
        before = store.prepared_content.repository
        before.acknowledge([(orphan, 5)])
        assert orphan not in dict(content_work.pending(conn, -1))
        plan = conn.execute('EXPLAIN QUERY PLAN ' + content_work.PAGE, (32,)).fetchall()
        assert any(content_work.INDEX in line[-1] for line in plan)
        assert any(content_work.RECORDING_INDEX in line[-1] for line in plan)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    report_file('content-work-c210.json').write_text(json.dumps({
        'queued_before_upgrade': len(original), 'migrated_queue_order_unchanged': True,
        'requests_stored_on_recordings': owned, 'ownerless_retractions': 1,
        'rotation_survived_reopen': True, 'stale_acknowledgement_ignored': True,
        'pending_index': content_work.RECORDING_INDEX,
    }, indent=2) + '\n', encoding='utf-8')
