"""Independent publication preserves exact judgments and authentication ownership."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_collection_feedback_preparation import finish, head, seed
from yt_playlist.core import account_auth_state, album_feedback_storage
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import collection_feedback as preparation, collection_feedback_publication as publication
from yt_playlist.core.store import Store


def prepare(conn, owner):
    owner.worker.install(conn)
    return finish(owner.worker, conn, size=128).prepared_revision


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_preopened_and_reopened_runtime_readers_keep_exact_originals(installed):
    store, _ = installed
    original = seed(store)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        for owner in publication.OWNERS.values():
            revision = prepare(store.conn, owner)
            owner.publish(store.conn, revision=revision)
            owner.publish(store.conn, revision=revision)
            assert owner.selected(other.conn) == revision
        assert {key:other.settings.get_setting(key) for key in original} == original
        assert album_feedback_storage.original(other.conn) == json.loads(original['album_feedback'])
        assert not album_feedback_storage.available(other.conn)
        assert not account_auth_state.available(other.conn)
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert {key:reopened.settings.get_setting(key) for key in original} == original
        assert album_feedback_storage.original(reopened.conn) == json.loads(original['album_feedback'])
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', tuple(publication.OWNERS))
def test_writes_references_and_deletions_withdraw_only_the_affected_owner(installed, name):
    store, _ = installed
    original = seed(store)
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    owner = publication.OWNERS[name]
    retained = head(store.conn, owner.source)
    with store.conn:
        if name == 'album-feedback':
            store.conn.execute("DELETE FROM album_provider_ids WHERE provider_id='alias'")
        else:
            store.conn.execute("UPDATE identities SET label='Renamed' WHERE id=1")
    assert owner.selected(store.conn) is None
    other = next(value for value in publication.OWNERS.values() if value is not owner)
    assert other.selected(store.conn) is not None
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    store.settings.set_setting(owner.source, '{}')
    assert owner.selected(store.conn) is None
    assert store.settings.get_setting(owner.source) == '{}'
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    store.settings.delete_setting(owner.source)
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert store.settings.get_setting(owner.source, 'absent') == 'absent'
    assert preparation.read_original(store.conn, retained) == original[owner.source]
    assert not store.conn.execute("SELECT 1 FROM album_provider_ids WHERE provider_id='missing-release'").fetchone()
    assert not store.conn.execute('SELECT 1 FROM identities WHERE id=999').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', tuple(publication.OWNERS))
@pytest.mark.parametrize('failure', ['file', 'missing-file', 'header', 'member', 'resolution'])
def test_unverified_originals_and_typed_rows_cannot_be_selected(installed, name, failure):
    store, _ = installed
    original = seed(store)
    owner = publication.OWNERS[name]
    revision = prepare(store.conn, owner)
    header, parts, _ = preparation.tables(owner.source)
    members = preparation.RESPONSES if name == 'album-feedback' else preparation.AUTH
    if failure in ('file', 'missing-file'):
        path = Path(store.conn.execute('SELECT filename FROM '+parts+' LIMIT 1').fetchone()[0])
        path.write_bytes(b'corrupt') if failure == 'file' else path.unlink()
    else:
        with store.conn:
            if failure == 'header':
                store.conn.execute('UPDATE '+header+' SET item_count=item_count+1')
            elif failure == 'member':
                store.conn.execute('DELETE FROM '+members+' WHERE position=1')
            elif name == 'album-feedback':
                store.conn.execute('UPDATE '+members+" SET album_id=NULL,unresolved_reason='missing_album' WHERE position=0")
            else:
                store.conn.execute('UPDATE '+members+' SET account_id=NULL,unresolved_account_id=1 WHERE position=0')
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) is None
    assert store.settings.get_setting(owner.source) == original[owner.source]
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('timing', ['file', 'verified'])
@pytest.mark.parametrize('mutation', ['source', 'members', 'manifest'])
def test_verification_and_final_publication_fence_source_and_custody_races(installed, monkeypatch, timing, mutation):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['album-feedback']
    revision = prepare(store.conn, owner)
    conn = sqlite3.connect(store._db_path)
    def change():
        with conn:
            if mutation == 'source':
                conn.execute("UPDATE settings SET value='{}' WHERE key='album_feedback'")
            elif mutation == 'members':
                conn.execute('DELETE FROM '+preparation.RESPONSES+' WHERE position=1')
            else:
                conn.execute('UPDATE '+preparation.tables(owner.source)[0]+' SET item_count=item_count+1')
    if timing == 'file':
        real_read = preparation.read_original
        def reading(connection, row):
            assert not connection.in_transaction
            value = real_read(connection, row)
            change()
            return value
        monkeypatch.setattr(preparation, 'read_original', reading)
    else:
        real_verify = owner.verify
        def verified(connection, revision):
            proof = real_verify(connection, revision)
            change()
            return proof
        # Frozen descriptors allow replacing the method at class level for this
        # narrowly scoped check; the other owner is never published here.
        monkeypatch.setattr(type(owner), 'verify', lambda self, connection, revision:verified(connection, revision))
    try:
        with pytest.raises(MigrationError, match='changed|reconciled'):
            owner.publish(store.conn, revision=revision)
        assert owner.selected(store.conn) is None
    finally:
        conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_publication_rollback_retry_immutable_custody_and_missing_file_recovery(installed, monkeypatch):
    store, _ = installed
    original = seed(store)
    owner = publication.OWNERS['album-feedback']
    revision = prepare(store.conn, owner)
    owner.install(store.conn)
    generations, selected, _ = owner.tables
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_publication_failure BEFORE INSERT ON '+selected+
                           " BEGIN SELECT RAISE(ABORT,'controlled interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled interruption'):
        owner.publish(store.conn, revision=revision)
    assert not store.conn.execute('SELECT 1 FROM '+generations).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_publication_failure')
    owner.publish(store.conn, revision=revision)
    for table, _ in owner.job.destinations:
        if table == preparation.tables(owner.source)[2]:
            continue
        with pytest.raises(sqlite3.IntegrityError, match='retained'):
            with store.conn:
                store.conn.execute('DELETE FROM '+table)
    path = Path(store.conn.execute('SELECT filename FROM '+preparation.tables(owner.source)[1]+' LIMIT 1').fetchone()[0])
    path.unlink()
    with pytest.raises(FileNotFoundError):
        store.settings.get_setting(owner.source)
    real_publish = preparation.files.publish
    def failed(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled repair failure')
    monkeypatch.setattr(preparation.files, 'publish', failed)
    with pytest.raises(OSError, match='repair failure'):
        owner.worker.step(store.conn, retry=True)
    progress = owner.worker.progress(store.conn)
    assert progress.status == 'failed'
    regular.migrate(store.conn)
    assert owner.worker.progress(store.conn) == progress
    monkeypatch.setattr(preparation.files, 'publish', real_publish)
    owner.worker.step(store.conn, retry=True)
    assert owner.worker.progress(store.conn).status == 'prepared'
    assert owner.selected(store.conn) == revision
    assert store.settings.get_setting(owner.source) == original[owner.source]
    assert store.conn.execute('SELECT count(*) FROM '+preparation.RESPONSES).fetchone()[0] == 3


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_long_maps_snapshot_fence_and_real_compatible_feedback_write(installed):
    from yt_playlist.rec.album_feedback import set_feedback
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['album-feedback']
    value = {'release-'+str(index):{'kind':'more', 'at':index} for index in range(2000)}
    store.settings.set_setting(owner.source, json.dumps(value))
    before = store.conn.execute('SELECT count(*) FROM albums').fetchone()[0]
    revision = prepare(store.conn, owner)
    owner.publish(store.conn, revision=revision)
    assert album_feedback_storage.original(store.conn) == value
    assert store.conn.execute('SELECT count(*) FROM albums').fetchone()[0] == before
    with store.read_snapshot() as snapshot:
        assert json.loads(snapshot.store.settings.get_setting(owner.source)) == value
        set_feedback(store, 'release', 'known', 9000)
        assert not snapshot.publish(lambda:None)
    assert owner.selected(store.conn) is None
    assert album_feedback_storage.original(store.conn)['release']['kind'] == 'known'
    plan = ' '.join(str(tuple(row)) for row in store.conn.execute(
        'EXPLAIN QUERY PLAN SELECT 1 FROM '+owner.tables[0]+' WHERE generation=?', (revision,)))
    assert owner.tables[0]+'_custody' in plan


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_original_numeric_kinds_survive_native_text_and_real_affinity(installed):
    store, _ = installed
    seed(store)
    value = {'release':{'kind':'more', 'at':9007199254740993,
                        'opportunity_id':1.2345678901234567, 'action_id':True}}
    raw = json.dumps(value)
    store.settings.set_setting('album_feedback', raw)
    owner = publication.OWNERS['album-feedback']
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert album_feedback_storage.original(store.conn) == value
    assert store.settings.get_setting(owner.source) == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_frozen_upgrade_retains_published_originals_and_prefers_native(installed, tmp_path):
    store, _ = installed
    original = seed(store)
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    upgraded = Store(tmp_path/'native-feedback.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        regular.validate(upgraded.conn)
        for owner in publication.OWNERS.values():
            assert owner.selected(upgraded.conn) is None
            assert owner.value(upgraded.conn, 0) == original[owner.source]
        assert account_auth_state.read(upgraded.conn) == {1:'Exact été label', 999:'Historical account', 998:''}
        assert len(album_feedback_storage.original(upgraded.conn)) == 2  # Later alias judgment wins.
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()
