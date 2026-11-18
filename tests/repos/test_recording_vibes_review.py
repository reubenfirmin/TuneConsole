"""Independent checks for upgrade identity safety and repaired evidence."""
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3

import pytest
from tests.prepared_fixture_support import roots

from tests.rec.test_vibe_prediction import capture, controlled_measurements, drain, fits, track
from tests.store_fixture_support import native_sql_calls, without_query_timing
from tests.test_audio_vibes import sample
from yt_playlist.core import sample_files
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Evidence upgrades and read guards construct no HTTP application."""


def test_identical_retry_restores_measurement_after_missing_file_was_processed(store):
    _tid, original = track(store, 'original')
    _tid, target = track(store, 'target')
    values = capture(store, 'original')
    drain(store)
    before = fits(store, target)
    filename = store.conn.execute('SELECT filename FROM sample_attributes WHERE id=?',
                                  (values[0]['id'],)).fetchone()[0]
    Path(filename).unlink()
    # Another ordinary catalog change causes the worker to observe the missing
    # file. The absent file itself need not be watched by the application.
    with store.conn:
        store.conn.execute('INSERT INTO rec_content_pending(identity_key) VALUES (?)', (original,))
    drain(store)
    assert fits(store, target)['relax']['source'] == 'heuristic'
    store.audio_vibes.save_sample(values[0], 500)
    drain(store)
    assert fits(store, target) == before


@pytest.mark.parametrize('storage', ['memory', 'file'])
@pytest.mark.parametrize('change', ['metadata', 'rename', 'insert', 'delete', 'rollback', 'unscoped'])
def test_identity_read_guard_keeps_membership_and_unscoped_dependencies(request, storage, change):
    store = request.getfixturevalue('store' if storage == 'memory' else 'file_backed_store')
    try:
        with store.conn:
            store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('original')")
        with store.read_snapshot() as snapshot:
            with column_group_reads(snapshot.store, 'recordings', 'identity'):
                assert len(snapshot.store.conn.execute('SELECT id,identity_key FROM recordings').fetchall()) == 1
            if change == 'unscoped':
                snapshot.store.conn.execute('SELECT bpm FROM recordings').fetchall()
            if change == 'insert':
                store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('added')")
            elif change == 'delete':
                store.conn.execute("DELETE FROM recordings WHERE identity_key='original'")
            elif change == 'rename':
                store.conn.execute("UPDATE recordings SET identity_key='renamed'")
            else:
                store.conn.execute('UPDATE recordings SET bpm=130')
            if change == 'rollback':
                store.conn.rollback()
            else:
                store.conn.commit()
            assert snapshot.publish(lambda: None) is (change in ('metadata', 'rollback'))
    finally:
        store.conn.close()
