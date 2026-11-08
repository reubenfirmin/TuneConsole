"""Select typed operations while preserving installed checkpoint identity."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.independent_schema_support import independent_regular_ddl
from tests.core.test_append_operation_preparation import seed
from tests.store_fixture_support import publication_consumer_reads
from yt_playlist.core import collection_recovery, radio_storage
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import append_operations as preparation, append_publication as publication
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture
def disable_periodic_library_sync():
    # These SQL/file owners do not construct the web application.
    pass


@pytest.mark.parametrize('version', [93, 97])
def test_append_input_keeps_installed_history_and_private_snapshot_guards(
        version, _bounded_installed_schema):
    source = _bounded_installed_schema(version).source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert runner.history(source.conn, registry.MIGRATIONS) == version
    assert source._snapshot_revisions.conn is source.conn
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


def prepare(owner, conn):
    owner.worker.install(conn)
    for _ in range(1000):
        progress = owner.worker.step(conn, batch_size=128)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Append preparation did not finish bounded fixture')


def stage(owner, conn):
    prepare(owner, conn)
    for _ in range(100):
        result = owner.step(conn, batch_size=32)
        if result.complete:
            return result.id
    pytest.fail('Append publication map did not finish')


@pytest.mark.parametrize('installed', [79, 93, 94, 96, 97], indirect=True)
@pytest.mark.parametrize('append', [False, True])
def test_preopened_runtime_readers_keep_exact_operation_state_and_revision(installed, append, monkeypatch):
    store, version = installed
    # Each fixture owner commits before the independent reader opens.
    names, sources, recipe, state = seed(store, version, append=append)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        for name, source in zip(names, sources):
            owner = publication.OWNERS[name]
            identity = stage(owner, store.conn)
            owner.publish(store.conn, stage=identity)
            assert owner.selected(other.conn) == identity
            original = json.loads(owner.value(other.conn, identity, source))
            assert (original['state'] if name.endswith('_native') else original) == (recipe if name.startswith('tv_') else state)
        seen, read = [], collection_recovery.read
        def selected(filename, *args, **kwargs):
            if '.preparation' in filename:
                seen.append(kwargs['kind'])
            return read(filename, *args, **kwargs)
        monkeypatch.setattr(collection_recovery, 'read', selected)
        assert other.playlist_recipes.get_recipe('TV') == recipe
        actual, revision = radio_storage.load(other.conn)
        assert actual == state
        assert revision == (('episode', 1) if version >= 94 else None)
        assert seen == ['tv-append', 'radio-session']
        regular.validate(other.conn)
        assert not other.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='tv_append_intents'").fetchone()
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.playlist_recipes.get_recipe('TV') == recipe
        actual, revision = radio_storage.load(reopened.conn)
        assert actual == state
        assert revision == (('episode', 1) if version >= 94 else None)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['header', 'member', 'extra', 'omitted', 'recovery', 'original', 'plan'])
def test_incorrect_operation_state_members_coverage_or_files_cannot_publish(installed, failure):
    store, version = installed
    names, sources, _, _ = seed(store, version)
    name, source = names[1], sources[1]
    owner = publication.OWNERS[name]
    prepare(owner, store.conn)
    header = preparation.head(store.conn, name, source)
    items = preparation.table(name, 'items')
    with store.conn:
        if failure == 'header':
            store.conn.execute('UPDATE '+owner.header+" SET receipt_handle_snapshot='different'")
        elif failure == 'member':
            store.conn.execute('UPDATE '+items+" SET video_id_snapshot='different' WHERE role='allowed'")
        elif failure == 'extra':
            store.conn.execute('INSERT INTO '+items+' SELECT source,preparation_revision,role,list_position,99,video_id_snapshot FROM '+items+
                " WHERE role='confirmed' AND position=0")
        elif failure == 'omitted':
            store.conn.execute('DELETE FROM '+owner.current)
        else:
            filename = (header[failure+'_filename'] if failure != 'original' else store.conn.execute(
                'SELECT filename FROM '+preparation.table(name, 'parts')+' LIMIT 1').fetchone()[0])
            Path(filename).write_bytes(b'corrupt')
    identity = owner.step(store.conn).id
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(store.conn, stage=identity)
    assert owner.selected(store.conn) is None


@pytest.mark.parametrize('installed', [93, 97], indirect=True)
@pytest.mark.parametrize('surface', ['tv', 'radio'])
def test_continuing_checkpoint_writes_and_snapshots_preserve_previous_operations(installed, surface):
    store, version = installed
    # Cross the default 128-member transfer boundary, including its final
    # member. Repeating identical members 8,192 times adds no write/snapshot
    # obligation; the preparation owner separately checks long resumptions.
    names, sources, recipe, state = seed(store, version, count=129)
    name, source = (names[0], sources[0]) if surface == 'tv' else (names[1], sources[1])
    owner = publication.OWNERS[name]
    identity = stage(owner, store.conn)
    owner.publish(store.conn, stage=identity)
    original = owner.value(store.conn, identity, source)
    with store.read_snapshot() as snapshot:
        if surface == 'tv':
            assert snapshot.store.playlist_recipes.get_recipe('TV') == recipe
            completed = {**recipe, 'tv_refill':{'request_id':'request', 'state':'complete', 'items':[]}}
            store.playlist_recipes.set_recipe('TV', completed, 4)
        else:
            actual, revision = radio_storage.load(snapshot.store.conn)
            assert actual == state
            newer = {**state, 'replacement':None, 'pending':None, 'phase':'playing'}
            if version < 94:
                store.settings.set_setting(radio_storage.SESSION_KEY, json.dumps(newer))
            else:
                with store.conn:
                    radio_storage.save(store.conn, newer, expected=revision, now=4)
        assert not snapshot.publish(lambda:None)
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, stage=stage(owner, store.conn))
    if surface == 'tv':
        assert store.playlist_recipes.get_recipe('TV') == completed
    else:
        assert radio_storage.load(store.conn)[0] == newer
    assert owner.value(store.conn, identity, source) == original
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM '+preparation.table(name, 'items'))


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_race_and_interrupted_publication_do_not_select_mixed_operations(installed):
    store, version = installed
    names, sources, _, _ = seed(store, version)
    owner = publication.OWNERS[names[0]]
    identity = stage(owner, store.conn)
    other = sqlite3.connect(store._db_path, timeout=.01)
    def race(conn, header):
        publication.verify_document(names[0], conn, header)
        with other:
            other.execute('UPDATE rec_recipes SET created_at=created_at')
    try:
        with pytest.raises(MigrationError, match='changed|reconciled'):
            replace(owner, verify_document=race).publish(store.conn, stage=identity)
    finally:
        other.close()
    identity = stage(owner, store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_append_cutover BEFORE INSERT ON '+owner.table('head')+
            " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled cutover interruption'):
        owner.publish(store.conn, stage=identity)
    assert not store.conn.execute('SELECT 1 FROM '+owner.table('generations')).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_append_cutover')
    owner.publish(store.conn, stage=identity)


@pytest.mark.parametrize('installed', [93, 97], indirect=True)
def test_missing_files_and_failed_preparation_remain_independent_of_regular_ddl(installed, monkeypatch, independent_regular_ddl):
    store, version = installed
    names, sources, recipe, _ = seed(store, version)
    name, source = names[0], sources[0]
    owner = publication.OWNERS[name]
    identity = stage(owner, store.conn)
    owner.publish(store.conn, stage=identity)
    header = owner.capture(store.conn, identity, source)
    Path(header['plan_filename']).unlink()
    Path(header['recovery_filename']).unlink()
    Path(store.conn.execute('SELECT filename FROM '+preparation.table(name, 'parts')+' LIMIT 1').fetchone()[0]).unlink()
    with pytest.raises(OSError):
        store.playlist_recipes.get_recipe('TV')
    store.init_schema(defer_metadata=True)
    regular.migrate(store.conn)
    publication.repair(name, store.conn, source)
    owner.publish(store.conn, stage=identity)
    assert store.playlist_recipes.get_recipe('TV') == recipe
    store.playlist_recipes.set_recipe('TV', {**recipe, 'tv_stream':'continuing-stream'}, 4)
    publish = preparation.recovery
    def fail(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled append recovery failure')
    monkeypatch.setattr(preparation, 'recovery', fail)
    with pytest.raises(OSError, match='controlled append recovery failure'):
        owner.worker.step(store.conn)
    failed = owner.worker.progress(store.conn)
    regular.migrate(store.conn)
    assert owner.worker.progress(store.conn) == failed
    monkeypatch.setattr(preparation, 'recovery', publish)
    with pytest.raises(MigrationError, match='retry explicitly'):
        owner.worker.step(store.conn)
    owner.worker.step(store.conn, retry=True)
    owner.publish(store.conn, stage=stage(owner, store.conn))


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_duplicate_video_metadata_and_optional_presence_are_not_collapsed(installed):
    store, version = installed
    names, sources, recipe, state = seed(store, version)
    recipe['tv_refill']['additions'] = [{'video_id':'same', 'position_metadata':1}, {'video_id':'same', 'position_metadata':2}]
    state['replacement']['proposed'] = [{'video_id':'same', 'paid':1}, {'video_id':'same', 'paid':2}]
    store.playlist_recipes.set_recipe('TV', recipe, 2)
    store.settings.set_setting(radio_storage.SESSION_KEY, json.dumps(state))
    for name in names:
        owner = publication.OWNERS[name]
        owner.publish(store.conn, stage=stage(owner, store.conn))
    assert store.playlist_recipes.get_recipe('TV') == recipe
    assert radio_storage.load(store.conn)[0] == state


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_frozen_owner_replacement_retires_old_selections_and_native_ops_keep_archives(installed, tmp_path):
    store, version = installed
    names, sources, recipe, state = seed(store, version, append=True)
    originals, identities = {}, {}
    for name, source in zip(names, sources):
        owner = publication.OWNERS[name]
        identities[name] = stage(owner, store.conn)
        owner.publish(store.conn, stage=identities[name])
        originals[name] = owner.value(store.conn, identities[name], source)
    upgraded = Store(tmp_path/'append-publication-native.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:97])
        upgraded.init_schema(defer_metadata=True)
        regular.validate(upgraded.conn)
        for name in names:
            assert publication.OWNERS[name].selected(upgraded.conn) is None
        for name in ('tv_native', 'radio_native'):
            owner = publication.OWNERS[name]
            owner.publish(upgraded.conn, stage=stage(owner, upgraded.conn))
        assert upgraded.playlist_recipes.get_recipe('TV') == recipe
        assert radio_storage.load(upgraded.conn)[0] == state
        # The explicit frozen upgrade runs on a dedicated connection, as its
        # one-time invocation does; serving connections have TEMP read adapters.
        upgraded.conn.close()
        upgraded = Store(upgraded._db_path)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        upgraded.init_schema(defer_metadata=True)
        regular.validate(upgraded.conn)
        for name, source in zip(names, sources):
            owner = publication.OWNERS[name]
            assert owner.selected(upgraded.conn) is None
            assert owner.value(upgraded.conn, identities[name], source) == originals[name]
        for name in ('tv_native', 'radio_native'):
            assert publication.OWNERS[name].selected(upgraded.conn) is None
        assert upgraded.playlist_recipes.get_recipe('TV') == recipe
        assert radio_storage.load(upgraded.conn)[0]['pending'] == state['pending']
    finally:
        upgraded.conn.close()
