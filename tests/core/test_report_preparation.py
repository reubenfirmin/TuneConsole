"""Report files retain exact paid output and typed serving fields before cutover."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.store_fixture_support import numbered_store, numbered_store as installed, preparation_ddl_connection, publication_preparation_input
from yt_playlist.core import recommendation_outputs as files, recommendation_reports as reports
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import recommendation_reports as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_preparation_input')


VALUE = {'at': 98, 'schema': 2, 'watermark': 'captured-watermark', 'report_id': 'paid-report',
         'refresh': {'generation': 'exact-generation', 'inputs': 'exact-inputs', 'next_at': 200},
         'home_format': {'scores': {'mixes': 1.25}, 'episodes': {'albums': 3}, 'days': {'radio': 2},
                         'vibe_episodes': 4, 'half_life_days': 30},
         'explanation': 'Retained prose and unicode 🎵 ' * 6000}


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Report preparation does not construct an HTTP application."""


def seed(store):
    raw = json.dumps(VALUE, ensure_ascii=False, indent=2)
    with store.conn:
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)',
                              [('listening_choices', raw, 100), ('model_health', '{}', 101)])
    return raw


def finish(conn):
    for _ in range(20):
        result = preparation.JOB.step(conn, batch_size=1)
        if result.status == 'prepared':
            return result
    pytest.fail('Report preparation did not finish')


def head(conn, surface='listening_choices'):
    return conn.execute(f'SELECT g.* FROM {preparation.HEAD} g JOIN prepared_report_heads h '
                        'USING(surface,generation) WHERE surface=?', (surface,)).fetchone()


@pytest.mark.parametrize('numbered_store', [79, 92], indirect=True)
def test_exact_multi_part_report_and_usage_keep_source_authority(numbered_store):
    store, _ = numbered_store
    raw = seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    first = preparation.JOB.step(conn, batch_size=1)
    assert first.processed == 1 and first.prepared_revision is None
    regular.migrate(conn)
    finish(conn)
    captured = head(conn)
    assert captured['part_count'] > 1
    assert preparation.read_original(conn, captured) == raw
    assert tuple(captured[key] for key in ('built_at', 'observed_at', 'schema_version', 'watermark',
        'report_key', 'evidence_generation', 'inputs_key', 'next_at', 'vibe_episodes', 'half_life_days')) == (
            100, 98, 2, 'captured-watermark', 'paid-report', 'exact-generation', 'exact-inputs', 200, 4, 30)
    assert list(map(tuple, conn.execute('SELECT format,field,value FROM prepared_report_usage ORDER BY format,field'))) == [
        ('albums', 'episodes', 3), ('mixes', 'scores', 1.25), ('radio', 'days', 2)]
    assert conn.execute("SELECT payload FROM rec_proposals WHERE surface='listening_choices'").fetchone()[0] == raw
    assert store.rec_proposals.get_proposals('listening_choices') == VALUE
    assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recommendation_report_heads'").fetchone()
    store.rec_proposals.put_proposals('listening_choices', {'at': 202, 'explanation': 'Later paid output'}, 203)
    finish(conn)
    assert json.loads(preparation.read_original(conn, head(conn)))['explanation'] == 'Later paid output'
    assert preparation.read_original(conn, captured) == raw
    with conn:
        conn.execute("UPDATE rec_proposals SET surface='retained-other',payload='opaque text' WHERE surface='listening_choices'")
    finish(conn)
    assert head(conn) is None
    assert preparation.read_original(conn, captured) == raw
    assert all(Path(row[0]).exists() for row in conn.execute(f'SELECT filename FROM {preparation.PARTS}'))
    assert files.collect_orphans(conn) == 0


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_file_work_releases_snapshot_and_rejects_concurrent_replacement(installed, monkeypatch):
    store, _ = installed
    seed(store)
    preparation.JOB.install(store.conn)
    publish = files.publish
    second = Store(store._db_path)
    calls = []
    try:
        def edit(root, encoded):
            assert not store.conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='listening_choices'")
                calls.append(True)
            return publish(root, encoded)
        monkeypatch.setattr(files, 'publish', edit)
        progress = preparation.JOB.step(store.conn, batch_size=1)
        assert progress.prepared_revision is None and progress.input_revision == 1
        assert not store.conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()
        finish(store.conn)
        assert preparation.read_original(store.conn, head(store.conn)) == '{}'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_disk_failure_interruption_and_restart_keep_committed_reports(installed, preparation_ddl_connection, monkeypatch):
    store, _ = installed
    raw = seed(store)
    conn = preparation_ddl_connection
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=1)
    publish = files.publish
    def interrupted(*args):
        assert not conn.in_transaction
        raise KeyboardInterrupt('controlled report interruption')
    monkeypatch.setattr(files, 'publish', interrupted)
    with pytest.raises(KeyboardInterrupt):
        preparation.JOB.step(conn, batch_size=1)
    assert preparation.JOB.progress(conn) == checkpoint
    def failed(*args):
        assert not conn.in_transaction
        raise OSError('controlled report disk failure')
    monkeypatch.setattr(files, 'publish', failed)
    with pytest.raises(OSError, match='controlled report disk failure'):
        preparation.JOB.step(conn, batch_size=1)
    assert preparation.JOB.progress(conn).status == 'failed'
    assert preparation.read_original(conn, head(conn)) == raw
    regular.migrate(conn)
    monkeypatch.setattr(files, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.JOB.step(reopened.conn)
        preparation.JOB.step(reopened.conn, retry=True)
        finish(reopened.conn)
        assert preparation.read_original(reopened.conn, head(reopened.conn)) == raw
        assert head(reopened.conn, 'model_health')['has_content'] == 0
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [92], indirect=True)
@pytest.mark.parametrize('value', [None, [], {'at': 1e999}, {'home_format': {'vibe_episodes': -1}},
                                  {'home_format': {'half_life_days': 0}}, {'home_format': {'scores': {'unknown': 1}}}])
def test_invalid_report_does_not_change_source_or_publish_staging_head(installed, value):
    store, _ = installed
    raw = json.dumps(value)
    with store.conn:
        store.conn.execute("INSERT INTO rec_proposals VALUES ('listening_choices',?,100)", (raw,))
    preparation.JOB.install(store.conn)
    with pytest.raises((MigrationError, ValueError, sqlite3.Error)):
        preparation.JOB.step(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.conn.execute("SELECT payload FROM rec_proposals WHERE surface='listening_choices'").fetchone()[0] == raw
    assert head(store.conn) is None


@pytest.mark.parametrize('installed', [92], indirect=True)
def test_source_byte_budget_is_checked_before_decoding_or_publishing(installed, monkeypatch):
    store, _ = installed
    raw = '{}' + ' ' * reports.MAX_BYTES
    with store.conn:
        store.conn.execute("INSERT INTO rec_proposals VALUES ('model_health',?,100)", (raw,))
    preparation.JOB.install(store.conn)
    def unexpected(*args):
        pytest.fail('Oversized source reached file publication')
    monkeypatch.setattr(files, 'publish', unexpected)
    with pytest.raises(MigrationError, match='byte budget'):
        preparation.JOB.step(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.conn.execute("SELECT payload FROM rec_proposals WHERE surface='model_health'").fetchone()[0] == raw
