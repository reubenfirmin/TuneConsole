"""Radio provenance staging preserves originals and unresolved scope identity."""
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.store_fixture_support import (
    numbered_store as installed, preparation_ddl_connection, publication_consumer_reads,
)
from yt_playlist.core import radio_provenance, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import radio_origins as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture
def disable_periodic_library_sync():
    pass


def finish(conn, size=16):
    for _ in range(100):
        progress = preparation.JOB.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Radio origin fixture did not finish')


def head(conn, source=preparation.KEYS[0]):
    return conn.execute(f'SELECT g.* FROM {preparation.CURRENT} h JOIN {preparation.HEAD} g USING(source,generation) '
                        'WHERE source=?', (source,)).fetchone()


def start_list(conn):
    for _ in range(10):
        progress = preparation.JOB.step(conn, batch_size=1)
        if conn.execute(f'SELECT 1 FROM {preparation.HEAD} WHERE source=?', (preparation.KEYS[0],)).fetchone():
            return progress
    pytest.fail('Radio origin list did not start')


def seed(store):
    first = store.accounts.upsert_account('First', 'first.json', None, True)
    second = store.accounts.upsert_account('Second', 'second.json', None, False)
    for account in (first, second):
        store.playlists.upsert_playlist(account, 'LM', 'Liked Music', 0, '', 1)
    store.playlists.upsert_playlist(first, 'shared', 'Shared', 0, '', 1)
    original = {preparation.KEYS[0]: json.dumps(['shared', 'shared', 'LM', 'missing'], indent=2),
                preparation.KEYS[1]: 'shared', preparation.KEYS[2]: '', preparation.KEYS[3]: None}
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)', original.items())
    return original


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_original_list_scalar_null_blank_and_repeated_origins_remain_exact(installed, preparation_ddl_connection):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    before = conn.execute('SELECT count(*) FROM main.playlist_scopes').fetchone()[0]
    preparation.JOB.install(conn)
    assert preparation.JOB.step(conn, batch_size=1).processed <= 1
    finish(conn)
    for source, raw in original.items():
        assert preparation.read_original(conn, head(conn, source)) == raw
        assert conn.execute('SELECT value FROM settings WHERE key=?', (source,)).fetchone()[0] == raw
    assert [tuple(row) for row in conn.execute(f'SELECT position,provider_playlist_id,playlist_scope_id,unresolved_reason '
        f'FROM {preparation.ORIGINS} WHERE source=? ORDER BY position', (preparation.KEYS[0],))] == [
            (0, 'shared', 'shared', None), (1, 'shared', 'shared', None),
            (2, 'LM', None, 'ambiguous_scope'), (3, 'missing', None, 'missing_scope')]
    assert head(conn, preparation.KEYS[2])['source_kind'] == 'blank'
    assert head(conn, preparation.KEYS[3])['source_kind'] == 'null'
    assert conn.execute('SELECT count(*) FROM main.playlist_scopes').fetchone()[0] == before
    assert radio_provenance.ids(conn) == ['shared', 'LM', 'missing']
    assert files.collect_orphans(conn) == 0
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    regular.migrate(preparation_ddl_connection)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_separate_frozen_upgrade_preserves_the_same_provider_origins(installed, tmp_path):
    store, _ = installed
    seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    upgraded = Store(tmp_path / 'origin-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        expected = sorted({row[0] for row in store.conn.execute(f'SELECT provider_playlist_id FROM {preparation.ORIGINS}')})
        assert radio_provenance.ids(upgraded.conn) == expected
        assert [row[0] for row in upgraded.conn.execute('SELECT unresolved_provider_id FROM radio_playlist_origins '
            'WHERE unresolved_provider_id IS NOT NULL ORDER BY 1')] == ['LM', 'missing']
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_long_origin_list_commits_bounded_members_without_deduplication(installed):
    store, _ = installed
    raw = json.dumps(['same-provider'] * 1200)
    store.settings.set_setting(preparation.KEYS[0], raw)
    preparation.JOB.install(store.conn)
    prior = 0
    for _ in range(15):
        progress = preparation.JOB.step(store.conn, batch_size=128)
        assert 0 <= progress.processed - prior <= 128
        prior = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Long Radio origin list did not finish')
    assert store.conn.execute(f'SELECT count(*) FROM {preparation.ORIGINS}').fetchone()[0] == 1200
    assert preparation.read_original(store.conn, head(store.conn)) == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_old_writer_partial_replacement_scope_edits_and_rename_reconcile(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    preparation.JOB.install(conn)
    start_list(conn)
    with conn:
        radio_provenance.remember(conn, 'later-provider')
    finish(conn)
    previous = head(conn)
    assert json.loads(preparation.read_original(conn, previous)) == ['shared', 'LM', 'missing', 'later-provider']
    store.playlists.upsert_playlist(1, 'missing', 'Now known', 0, '', 1)
    assert preparation.JOB.progress(conn).prepared_revision is None
    finish(conn)
    assert conn.execute(f'SELECT playlist_scope_id FROM {preparation.ORIGINS} WHERE source=? AND generation=? '
        'AND provider_playlist_id=?', (preparation.KEYS[0], head(conn)['generation'], 'missing')).fetchone()[0] == 'missing'
    assert json.loads(preparation.read_original(conn, previous))[-1] == 'later-provider'
    scalar = head(conn, preparation.KEYS[1])
    with conn:
        conn.execute('DELETE FROM settings WHERE key=?', (preparation.KEYS[1],))
        conn.execute('UPDATE settings SET key=? WHERE key=?', (preparation.KEYS[1], preparation.KEYS[0]))
    # Its new scalar representation is intentionally retained exactly, even
    # though the legacy IDs reader now sees this JSON text as one provider ID.
    finish(conn)
    assert head(conn) is None
    assert preparation.read_original(conn, scalar) == original[preparation.KEYS[1]]
    assert head(conn, preparation.KEYS[1])['item_count'] == 1


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_file_io_allows_source_replacement_and_fences_stale_work(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    seed(store)
    preparation.JOB.install(conn)
    start_list(conn)
    second = Store(store._db_path)
    publish = files.publish
    calls = []
    try:
        def edit(*args):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute('UPDATE settings SET value=? WHERE key=?', ('[]', preparation.KEYS[0]))
                calls.append(True)
            return publish(*args)
        monkeypatch.setattr(files, 'publish', edit)
        assert preparation.JOB.step(conn, batch_size=1).input_revision == 1
        assert head(conn) is None
        finish(conn)
        assert preparation.read_original(conn, head(conn)) == '[]'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupted_head_and_disk_failure_preserve_commits_for_reopened_retry(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    seed(store)
    preparation.JOB.install(conn)
    checkpoint = start_list(conn)
    def interrupted(*args):
        preparation.complete(*args)
        raise KeyboardInterrupt('controlled Radio origin head interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(preparation.JOB, complete=interrupted).step(conn)
    assert preparation.JOB.progress(conn) == checkpoint
    assert head(conn) is None
    assert not conn.execute(f'SELECT 1 FROM {preparation.ORIGINS} WHERE source=?', (preparation.KEYS[0],)).fetchone()
    publish = files.publish
    def failed(*args):
        raise OSError('controlled Radio origin disk failure')
    monkeypatch.setattr(files, 'publish', failed)
    with pytest.raises(OSError, match='controlled Radio origin disk failure'):
        preparation.JOB.step(conn)
    regular.migrate(conn)
    monkeypatch.setattr(files, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.JOB.step(reopened.conn)
        preparation.JOB.step(reopened.conn, retry=True)
        finish(reopened.conn)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_member_batch_failure_rolls_back_every_insert_and_retries_from_checkpoint(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    preparation.JOB.install(conn)
    checkpoint = start_list(conn)
    prior_origins = list(map(tuple, conn.execute(f'SELECT * FROM {preparation.ORIGINS} ORDER BY source,generation,position')))
    prior_parts = list(map(tuple, conn.execute(f'SELECT * FROM {preparation.PARTS} ORDER BY source,generation,position')))
    conn.execute(f'''CREATE TRIGGER reject_second_origin BEFORE INSERT ON {preparation.ORIGINS}
        WHEN NEW.position=1 BEGIN SELECT RAISE(ABORT,'controlled origin batch failure'); END''')
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='controlled origin batch failure'):
        preparation.JOB.step(conn, batch_size=4)
    failed = preparation.JOB.progress(conn)
    assert failed.status == 'failed'
    assert (failed.cursor, failed.processed) == (checkpoint.cursor, checkpoint.processed)
    assert head(conn) is None
    assert list(map(tuple, conn.execute(f'SELECT * FROM {preparation.ORIGINS} ORDER BY source,generation,position'))) == prior_origins
    assert list(map(tuple, conn.execute(f'SELECT * FROM {preparation.PARTS} ORDER BY source,generation,position'))) == prior_parts
    conn.execute('DROP TRIGGER reject_second_origin')
    conn.commit()
    preparation.JOB.step(conn, batch_size=4, retry=True)
    finish(conn)
    assert preparation.read_original(conn, head(conn)) == original[preparation.KEYS[0]]
    assert [tuple(row) for row in conn.execute(f'SELECT position,provider_playlist_id,playlist_scope_id,unresolved_reason '
        f'FROM {preparation.ORIGINS} WHERE source=? ORDER BY position', (preparation.KEYS[0],))] == [
            (0, 'shared', 'shared', None), (1, 'shared', 'shared', None),
            (2, 'LM', None, 'ambiguous_scope'), (3, 'missing', None, 'missing_scope')]


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw', ['{}', '[null]', '[""]', '[true]'])
def test_invalid_origin_list_fails_durably_without_affecting_source_or_ddl(installed, raw):
    store, _ = installed
    store.settings.set_setting(preparation.KEYS[0], raw)
    preparation.JOB.install(store.conn)
    with pytest.raises(ValueError):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.settings.get_setting(preparation.KEYS[0]) == raw
    regular.migrate(store.conn)
