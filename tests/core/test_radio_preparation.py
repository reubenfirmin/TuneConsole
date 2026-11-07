"""Retained Radio sessions prepare independently of migration and live writers."""
import json
from pathlib import Path

import pytest

from yt_playlist.core import collection_recovery, radio_storage
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import radio_session as preparation
from yt_playlist.core.store import Store


def finish(conn, *, size=2):
    for _ in range(30):
        result = preparation.JOB.step(conn, batch_size=size)
        if result.status == 'prepared':
            return result
    pytest.fail('Radio preparation did not finish')


def header(conn):
    generation = preparation.JOB.progress(conn).prepared_revision
    return conn.execute('SELECT * FROM prepared_radio_sessions WHERE generation=?', (generation,)).fetchone()


def test_queue_batches_preserve_session_wire_order_and_continuing_writes(tmp_path, installed_radio):
    store, state, raw = installed_radio(tmp_path / 'radio.db')
    history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    try:
        preparation.JOB.install(store.conn)
        first = preparation.JOB.step(store.conn, batch_size=2)
        assert first.status == 'copying' and first.processed == 2
        assert store.conn.execute('SELECT count(*) FROM prepared_radio_queue').fetchone()[0] == 2
        regular.migrate(store.conn)
        finish(store.conn)
        original = header(store.conn)
        assert preparation.read(original) == raw
        assert (original['episode_id'], original['account_id'], original['phase']) == (
            'retained-episode', 3, 'appending')
        assert [tuple(row) for row in store.conn.execute(
            'SELECT position,video_id,state FROM prepared_radio_queue WHERE generation=? ORDER BY position',
                (original['generation'],))] == [
                    (0, 'first', 'played'), (1, 'second', 'current'), (2, 'first', 'queued'),
                    (3, 'second', 'initial'), (4, 'pending', 'pending')]
        assert radio_storage.load_published(store.conn) == (state, None)
        assert not radio_storage.available(store.conn)
        # Changes behind a completed checkpoint invalidate its proof. The new
        # generation starts at position zero and retains every original byte.
        updated = {**state, 'phase': 'stopped', 'position': 2, 'pending_sent': True,
                   'pending_result': {'uncertain': True}, 'tuning': {'paid_result': ['retained']}}
        edited = json.dumps(updated, indent=2, ensure_ascii=False)
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (edited, preparation.KEY))
        assert preparation.JOB.progress(store.conn).prepared_revision is None
        finish(store.conn)
        assert preparation.read(header(store.conn)) == edited
        assert header(store.conn)['phase'] == 'stopped'
        assert preparation.read(original) == raw
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert radio_storage.load_published(store.conn) == (updated, None)
        # Serving collectors do not delete retained preparation files.
        assert collection_recovery.collect_orphans(store.conn) == 0
        assert Path(original['filename']).exists()
    finally:
        store.conn.close()


def test_source_edit_during_file_publication_cannot_publish_stale_queue(tmp_path, monkeypatch, installed_radio):
    store, state, _ = installed_radio(tmp_path / 'racing.db')
    second = Store(store._db_path)
    publish = preparation._publish_encoded
    calls = []
    try:
        preparation.JOB.install(store.conn)

        def editing(root, encoded, digest):
            assert not store.conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute('UPDATE settings SET value=? WHERE key=?',
                        (json.dumps({**state, 'position': 2, 'phase': 'stopped'}), preparation.KEY))
                calls.append(True)
            return publish(root, encoded, digest)

        monkeypatch.setattr(preparation, '_publish_encoded', editing)
        result = preparation.JOB.step(store.conn, batch_size=2)
        assert result.input_revision == 1 and result.prepared_revision is None
        assert not store.conn.execute('SELECT 1 FROM prepared_radio_queue').fetchone()
        assert not store.conn.execute('SELECT 1 FROM prepared_radio_sessions').fetchone()
        finish(store.conn)
        assert header(store.conn)['phase'] == 'stopped'
        assert json.loads(preparation.read(header(store.conn)))['position'] == 2
    finally:
        second.conn.close()
        store.conn.close()


def test_interruption_resumes_committed_batch_and_failure_needs_explicit_retry(tmp_path, monkeypatch, installed_radio):
    store, state, raw = installed_radio(tmp_path / 'interrupted.db')
    publish = preparation._publish_encoded
    try:
        preparation.JOB.install(store.conn)
        checkpoint = preparation.JOB.step(store.conn, batch_size=1)

        def interrupted(*args):
            assert not store.conn.in_transaction
            raise KeyboardInterrupt('controlled file interruption')

        monkeypatch.setattr(preparation, '_publish_encoded', interrupted)
        with pytest.raises(KeyboardInterrupt):
            preparation.JOB.step(store.conn, batch_size=1)
        assert preparation.JOB.progress(store.conn) == checkpoint
        monkeypatch.setattr(preparation, '_publish_encoded', publish)
        finish(store.conn, size=1)
        assert preparation.read(header(store.conn)) == raw
        assert header(store.conn)['queue_size'] == 5
        assert store.conn.execute('SELECT count(*) FROM prepared_radio_queue').fetchone()[0] == 5
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?',
                               (json.dumps({**state, 'identity': 999}), preparation.KEY))
        with pytest.raises(MigrationError, match='recorded account'):
            preparation.JOB.step(store.conn)
        assert preparation.JOB.progress(store.conn).status == 'failed'
        regular.migrate(store.conn)
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (raw, preparation.KEY))
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.JOB.step(store.conn)
        preparation.JOB.step(store.conn, batch_size=1, retry=True)
        finish(store.conn, size=1)
        assert preparation.read(header(store.conn)) == raw
    finally:
        store.conn.close()


def test_missing_identity_stays_unresolved_and_catalog_edits_reconcile(tmp_path, installed_radio):
    store, state, _ = installed_radio(tmp_path / 'unresolved.db')
    try:
        state.pop('episode_id')
        state['tracks'].append({'video_id': 'unknown-upload', 'title': 'No inference'})
        with store.conn:
            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(state), preparation.KEY))
        preparation.JOB.install(store.conn)
        finish(store.conn)
        row = header(store.conn)
        assert row['episode_id'] is None and row['episode_resolution'] == 'missing_episode_id'
        assert tuple(store.conn.execute('SELECT recording_id,entry_id,unresolved_reason FROM prepared_radio_queue '
            "WHERE generation=? AND video_id='unknown-upload'", (row['generation'],)).fetchone()) == (
                None, None, 'unadmitted_video')
        assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='unknown-upload'").fetchone()
        # Even an unchanged source must be rechecked if exact upload ownership
        # changes. No inferred names or new catalog admissions are involved.
        from yt_playlist.core import catalog
        with store.conn:
            catalog.upsert(store.conn, 'unknown-upload', 'Title', 'Artist', preferred_key='known-recording')
        assert preparation.JOB.progress(store.conn).prepared_revision is None
        finish(store.conn)
        assert store.conn.execute('SELECT recording_id FROM prepared_radio_queue '
            "WHERE generation=? AND video_id='unknown-upload'", (header(store.conn)['generation'],)).fetchone()[0] is not None
    finally:
        store.conn.close()


def test_large_queue_installation_and_commits_stay_bounded_with_serving_views(tmp_path, installed_radio):
    setup, state, _ = installed_radio(tmp_path / 'large.db')
    state['tracks'] = [state['tracks'][0]] * 1100
    state['initial_tracks'], state['pending'] = [], None
    with setup.conn:
        setup.conn.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(state), preparation.KEY))
    setup.conn.close()
    store = Store(tmp_path / 'large.db')
    try:
        store.init_schema(defer_metadata=True)
        preparation.JOB.install(store.conn)
        assert not store.conn.execute('SELECT 1 FROM prepared_radio_queue').fetchone()
        for index in range(3):
            preparation.JOB.step(store.conn, batch_size=128)
            assert store.conn.execute('SELECT count(*) FROM prepared_radio_queue').fetchone()[0] == 128 * (index + 1)
        regular.migrate(store.conn)
        finish(store.conn, size=128)
        assert header(store.conn)['queue_size'] == 1100
        assert json.loads(preparation.read(header(store.conn))) == state
        assert radio_storage.load_published(store.conn) == (state, None)
    finally:
        store.conn.close()


@pytest.mark.parametrize('present,raw', [(False, None), (True, None), (True, 'null'), (True, '')])
def test_absent_and_null_sessions_do_not_invent_an_active_episode(tmp_path, installed_radio, present, raw):
    store, _, _ = installed_radio(tmp_path / 'empty.db')
    try:
        with store.conn:
            store.conn.execute('DELETE FROM settings WHERE key=?', (preparation.KEY,))
            if present:
                store.conn.execute('INSERT INTO settings VALUES (?,?)', (preparation.KEY, raw))
        preparation.JOB.install(store.conn)
        finish(store.conn)
        row = header(store.conn)
        assert row['active'] == 0 and row['episode_id'] is None and row['queue_size'] == 0
        assert row['source_present'] == present
        assert preparation.read(row) == raw
    finally:
        store.conn.close()
