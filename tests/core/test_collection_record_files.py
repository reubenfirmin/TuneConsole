"""Deliberate real SQL/file checks beneath mocked collection controller cases."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.store_fixture_support import file_consumer_store, native_sql_calls, without_query_timing
from yt_playlist.core import collection_recovery, fao
from yt_playlist.core.store import Store

pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These persistence checks do not construct the web application."""


def controller(store, workflow):
    return {'radio': store.radio_checkpoints, 'playlist': store.playlist_recipes, 'sync': store.sync}[workflow]


def read(store, workflow):
    if workflow == 'radio':
        return store.radio_checkpoints.load()[0]
    if workflow == 'playlist':
        return store.playlist_recipes.get_recipe('PLAYLIST')
    return store.sync.state()


def save(store, workflow, value, now):
    if workflow == 'radio':
        return store.radio_checkpoints.save(json.dumps(value), expected=store.radio_checkpoints.load()[1])
    if workflow == 'playlist':
        return store.playlist_recipes.set_recipe('PLAYLIST', value, now)
    return store.sync.finish_attempt(now, value)


@pytest.mark.parametrize('workflow', ['radio', 'playlist', 'sync'])
@pytest.mark.parametrize('failure', ['file', 'status'])
def test_collection_interruption_retains_the_prior_checkpoint_and_pending_record_across_restart(
        file_consumer_store, monkeypatch, workflow, failure):
    store = file_consumer_store
    account = store.accounts.upsert_account('Owner', 'owner.json', None, True)
    values = {
        'radio': dict(version=2, phase='playing', episode_id='episode', identity=account, tracks=[]),
        'playlist': dict(media='video'),
        'sync': dict(status='complete', accounts={}, resources={}),
    }
    first = {**values[workflow], 'marker': 'first'}
    save(store, workflow, first, 10)
    prior = read(store, workflow)
    owner = controller(store, workflow)
    files = owner.recovery if workflow == 'sync' else owner.files
    method = {'radio': 'publish', 'playlist': 'publish_recipe', 'sync': 'finish_attempt'}[workflow]
    target, operation = (files, 'publish_reserved') if failure == 'file' else (owner.repository, method)
    second = {**values[workflow], 'marker': 'second'}

    def interrupted(*args, **kwargs):
        raise OSError('collection interruption')

    with monkeypatch.context() as patch:
        patch.setattr(target, operation, interrupted)
        with pytest.raises(OSError, match='collection interruption'):
            save(store, workflow, second, 20)
    assert read(store, workflow) == prior
    assert not store.conn.in_transaction
    with closing(sqlite3.connect(store._db_path)) as observer:
        observer.row_factory = sqlite3.Row
        rows = [dict(row) for row in observer.execute('SELECT * FROM collection_file_writes ORDER BY id')]
    assert [row['file_status'] for row in rows] == ['ready', 'pending']
    root = collection_recovery.directory(store.conn)
    pending = root / 'by-id' / f"{rows[-1]['id'] % 256:02x}" / f"{rows[-1]['id']}.json"
    assert pending.exists() is (failure == 'status')
    if pending.exists():
        assert fao.read(pending).bytes

    restarted = Store(store._db_path)
    try:
        restarted.init_schema(defer_metadata=True)
        assert read(restarted, workflow) == prior
        save(restarted, workflow, second, 30)
        assert read(restarted, workflow)['marker'] == 'second'
        reference = next(row for row in collection_recovery.references(restarted.conn)
                         if row[2] == {'radio': 'radio-session', 'playlist': 'playlist-recipe',
                                       'sync': 'library-sync'}[workflow])
        selected = restarted.conn.execute('SELECT * FROM collection_file_writes ORDER BY id DESC LIMIT 1').fetchone()
        assert selected['file_status'] == 'ready'
        assert Path(reference[3]).stem == str(selected['id'])
        assert reference[4] == selected['content_digest']
        assert Path(reference[3]).stem != selected['content_digest']
        assert not restarted.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restarted.conn.close()


def test_collection_backup_restores_record_paths_and_legacy_reads_remain_writable(
        file_consumer_store, tmp_path):
    store = file_consumer_store
    account = store.accounts.upsert_account('Owner', 'owner.json', None, True)
    save(store, 'radio', dict(version=2, phase='playing', episode_id='episode', identity=account, tracks=[]), 10)
    save(store, 'playlist', dict(media='video', marker='installed'), 10)
    save(store, 'sync', dict(status='complete', accounts={}, resources={}), 10)

    # Retain an installed hash-named body beside native documents, then make an
    # ordinary controller update. No startup conversion is needed to read it.
    recipe = store.conn.execute('SELECT id FROM rec_recipes').fetchone()[0]
    installed = collection_recovery.publish(store.conn, 'playlist-recipe', recipe,
                                            dict(media='video', marker='installed'))
    with store.conn.atomic():
        store.conn.execute('UPDATE rec_recipes SET recovery_filename=?,recovery_digest=?,recovery_bytes=? WHERE id=?',
                           (*installed, recipe))
    assert read(store, 'playlist')['marker'] == 'installed'
    save(store, 'playlist', dict(media='video', marker='updated'), 20)
    before = list(collection_recovery.references(store.conn))
    identities = {(row[0], row[1]): collection_recovery.record_id(row[3]) for row in before}
    assert all(identities.values())
    ages = {(row[0], row[1]): fao.read(row[3]).info.ctime_ns for row in before}

    store.conn.execute('BEGIN')
    try:
        manifest = collection_recovery.export(store.conn, tmp_path / 'backup')
    finally:
        store.conn.rollback()
    clone_path = tmp_path / 'restored.db'
    with closing(sqlite3.connect(clone_path)) as destination:
        store.conn.backup(destination)
    restored = Store(clone_path)
    try:
        restored.init_schema(defer_metadata=True)
        collection_recovery.restore(restored.conn, manifest)
        assert all(read(restored, name) == read(store, name) for name in ('radio', 'playlist', 'sync'))
        for table, identity, _, filename, _, _ in collection_recovery.references(restored.conn):
            assert collection_recovery.record_id(filename) == identities[table, identity]
            assert Path(filename).is_relative_to(collection_recovery.directory(restored.conn))
            assert fao.read(filename).info.ctime_ns == ages[table, identity]
        assert not restored.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restored.conn.close()
