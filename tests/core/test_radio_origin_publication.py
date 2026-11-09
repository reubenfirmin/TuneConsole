"""Radio origin cutover preserves scope uncertainty and continuing provenance."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.store_fixture_support import guarded_numbered_store as guarded_installed
from tests.core.test_radio_origin_preparation import finish, head, seed
from tests.store_fixture_support import (
    _numbered_input, mock_preparation_schema, native_sql_calls,
    publication_consumer_reads, preparation_ddl_connection, without_query_timing,
)
from yt_playlist.core import radio_provenance
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import radio_origin_publication as publication, radio_origins as preparation
from yt_playlist.core.store import Store



pytestmark = pytest.mark.usefixtures('publication_consumer_reads', 'native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """This owner runs directly without the HTTP application."""


@pytest.fixture
def installed(request, tmp_path, monkeypatch, native_sql_calls, without_query_timing):
    with _numbered_input(request.param, tmp_path, monkeypatch) as value:
        yield value


def prepare(conn, *, install=True, size=128):
    if install:
        preparation.JOB.install(conn)
    return finish(conn,size=size).prepared_revision


@pytest.mark.parametrize('installed',[79,93],indirect=True)
def test_preopened_and_reopened_readers_use_exact_originals_and_typed_origins(installed,monkeypatch):
    store,_ = installed
    original = seed(store)
    before = store.conn.execute('SELECT count(*) FROM main.playlist_scopes').fetchone()[0]
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        revision = prepare(store.conn)
        publication.publish(store.conn,revision=revision)
        publication.publish(store.conn,revision=revision)
        assert publication.selected(other.conn) == revision
        assert {key:other.settings.get_setting(key) for key in original} == original
        def no_file_read(*args):
            pytest.fail('Origin IDs should use selected immutable typed members')
        monkeypatch.setattr(preparation,'read_original',no_file_read)
        assert radio_provenance.ids(other.conn) == ['shared','LM','missing']
        assert not radio_provenance.available(other.conn)
        assert store.conn.execute('SELECT count(*) FROM main.playlist_scopes').fetchone()[0] == before
        regular.validate(other.conn)
    finally:
        other.conn.close()
    monkeypatch.undo()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert publication.selected(reopened.conn) == revision
        assert {key:reopened.settings.get_setting(key) for key in original} == original
        assert radio_provenance.ids(reopened.conn) == ['shared','LM','missing']
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('guarded_installed',[93],indirect=True)
def test_continuing_remember_and_scope_edits_preserve_custody_without_admission(guarded_installed):
    store,_ = guarded_installed
    original = seed(store)
    revision = prepare(store.conn)
    publication.publish(store.conn,revision=revision)
    old = head(store.conn)
    with store.conn:
        radio_provenance.remember(store.conn,'later-provider')
    assert publication.selected(store.conn) is None
    assert radio_provenance.ids(store.conn) == ['shared','LM','missing','later-provider']
    publication.publish(store.conn,revision=prepare(store.conn))
    store.playlists.upsert_playlist(1,'missing','Now known',0,'',1)
    assert publication.selected(store.conn) is None
    publication.publish(store.conn,revision=prepare(store.conn))
    assert preparation.read_original(store.conn,old) == original[preparation.KEYS[0]]
    assert radio_provenance.ids(store.conn) == ['shared','LM','missing','later-provider']
    assert not store.conn.execute("SELECT 1 FROM main.playlist_scopes WHERE provider_id='later-provider'").fetchone()
    for owner in (preparation.HEAD,preparation.PARTS,preparation.ORIGINS,publication.LINKS):
        with pytest.raises(sqlite3.IntegrityError,match='(retained|future cleanup)'):
            with store.conn:
                store.conn.execute('DELETE FROM '+owner)
    with store.read_snapshot() as snapshot:
        assert radio_provenance.ids(snapshot.store.conn)[-1] == 'later-provider'
        store.settings.set_setting(preparation.KEYS[1],'new-scalar')
        assert not snapshot.publish(lambda:None)


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_remember_reserves_writer_before_reading_selected_predecessor(installed,monkeypatch):
    store,_ = installed
    seed(store)
    publication.publish(store.conn,revision=prepare(store.conn))
    other = sqlite3.connect(store._db_path,timeout=.01)
    real_ids = radio_provenance.ids
    def reading(conn):
        assert conn.in_transaction
        with pytest.raises(sqlite3.OperationalError,match='locked'):
            other.execute('UPDATE main.settings SET value=? WHERE key=?',('[]',preparation.KEYS[0]))
        other.rollback()
        return real_ids(conn)
    monkeypatch.setattr(radio_provenance,'ids',reading)
    try:
        with store.conn:
            radio_provenance.remember(store.conn,'new')
        assert real_ids(store.conn) == ['shared','LM','missing','new']
    finally:
        other.close()


@pytest.mark.parametrize('installed',[93],indirect=True)
@pytest.mark.parametrize('failure',['file','missing-file','fields','member'])
def test_incomplete_or_corrupt_preparation_keeps_installed_origins(installed,preparation_ddl_connection,failure):
    store,_ = installed
    conn = preparation_ddl_connection
    original = seed(store)
    revision = prepare(conn)
    if failure in ('file','missing-file'):
        path = Path(conn.execute('SELECT filename FROM '+preparation.PARTS+' LIMIT 1').fetchone()[0])
        if failure == 'file':
            path.write_bytes(b'corrupt')
        else:
            path.unlink()
    else:
        with conn:
            if failure == 'fields':
                conn.execute('UPDATE '+preparation.HEAD+' SET item_count=item_count+1')
            else:
                conn.execute('DELETE FROM '+preparation.ORIGINS+' WHERE position=1')
    with pytest.raises((MigrationError,ValueError,OSError)):
        publication.publish(conn,revision=revision)
    assert publication.selected(conn) is None
    assert radio_provenance.ids(store.conn) == ['shared','LM','missing']
    assert {key:store.settings.get_setting(key) for key in original} == original
    regular.migrate(conn)


@pytest.mark.parametrize('installed',[93],indirect=True)
@pytest.mark.parametrize('timing,mutation',[('file','source'),('file','members'),('file','manifest'),
                                         ('verified','source'),('verified','members')])
def test_source_and_custody_races_cannot_publish_stale_origins(installed,monkeypatch,timing,mutation):
    store,_ = installed
    seed(store)
    revision = prepare(store.conn)
    other = sqlite3.connect(store._db_path)
    def change():
        with other:
            if mutation == 'source':
                other.execute('UPDATE main.settings SET value=? WHERE key=?',('[]',preparation.KEYS[0]))
            elif mutation == 'manifest':
                other.execute('UPDATE '+preparation.HEAD+' SET item_count=item_count+1')
            else:
                other.execute('DELETE FROM '+preparation.ORIGINS+' WHERE position=1')
    try:
        if timing == 'file':
            real_read = preparation.read_original
            calls = []
            def reading(conn,row):
                assert not conn.in_transaction
                if not calls:
                    change()
                    calls.append(True)
                return real_read(conn,row)
            monkeypatch.setattr(preparation,'read_original',reading)
        else:
            real_verify = publication.verify
            def verified(conn,revision):
                result = real_verify(conn,revision)
                change()
                return result
            monkeypatch.setattr(publication,'verify',verified)
        with pytest.raises(MigrationError,match='(changed|reconciled preparation)'):
            publication.publish(store.conn,revision=revision)
        assert publication.selected(store.conn) is None
        assert not store.conn.execute('SELECT 1 FROM '+publication.GENERATIONS).fetchone()
    finally:
        other.close()


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_interruption_after_link_copy_retains_previous_generation_and_retry_is_atomic(installed):
    store,_ = installed
    original = seed(store)
    first = prepare(store.conn)
    publication.publish(store.conn,revision=first)
    retained = head(store.conn)
    store.settings.set_setting(preparation.KEYS[0],'["replacement"]')
    revision = prepare(store.conn)
    def interrupted():
        raise KeyboardInterrupt('controlled origin interruption')
    with pytest.raises(KeyboardInterrupt):
        publication.publish(store.conn,revision=revision,clock=interrupted)
    store.conn.execute('CREATE TEMP TRIGGER abort_origin_cutover BEFORE INSERT ON main.'+publication.HEAD+
        " BEGIN SELECT RAISE(ABORT,'controlled origin cutover failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='controlled origin cutover'):
        publication.publish(store.conn,revision=revision)
    assert publication.selected(store.conn) is None
    assert list(map(tuple,store.conn.execute('SELECT revision FROM '+publication.GENERATIONS))) == [(first,)]
    assert store.conn.execute('SELECT count(*) FROM '+publication.LINKS).fetchone()[0] == 4
    assert preparation.read_original(store.conn,retained) == original[preparation.KEYS[0]]
    store.conn.execute('DROP TRIGGER temp.abort_origin_cutover')
    publication.publish(store.conn,revision=revision)
    assert radio_provenance.ids(store.conn) == ['replacement','shared']


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_missing_original_files_are_recovered_explicitly_without_replacing_members(installed):
    store,_ = installed
    original = seed(store)
    revision = prepare(store.conn)
    publication.publish(store.conn,revision=revision)
    manifest = list(map(tuple,store.conn.execute('SELECT * FROM '+preparation.PARTS+' ORDER BY source,position')))
    for row in store.conn.execute('SELECT filename FROM '+preparation.PARTS):
        Path(row[0]).unlink()
    assert radio_provenance.ids(store.conn) == ['shared','LM','missing']
    with pytest.raises(FileNotFoundError):
        store.settings.get_setting(preparation.KEYS[0])
    preparation.JOB.step(store.conn,retry=True)
    assert preparation.JOB.progress(store.conn).status == 'prepared'
    assert {key:store.settings.get_setting(key) for key in original} == original
    assert list(map(tuple,store.conn.execute('SELECT * FROM '+preparation.PARTS+' ORDER BY source,position'))) == manifest


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_long_repeated_lists_and_absent_sources_preserve_runtime_order(installed, monkeypatch):
    store,_ = installed
    raw = json.dumps(['first','missing','first']*2700,indent=2)
    store.settings.set_setting(preparation.KEYS[0],raw)
    preparation.JOB.install(store.conn)
    mock_preparation_schema(monkeypatch, preparation.JOB.definition, store.conn)
    publication.publish(store.conn,revision=prepare(store.conn,install=False,size=512))
    assert radio_provenance.ids(store.conn) == ['first','missing']
    assert store.settings.get_setting(preparation.KEYS[0]) == raw
    assert store.settings.get_setting(preparation.KEYS[1],'absent') == 'absent'
    assert store.conn.execute('SELECT count(*) FROM '+preparation.ORIGINS).fetchone()[0] == 8100
    store.settings.delete_setting(preparation.KEYS[0])
    publication.publish(store.conn,revision=prepare(store.conn,install=False,size=512))
    assert radio_provenance.ids(store.conn) == []
    assert store.settings.get_setting(preparation.KEYS[0],'absent') == 'absent'
