"""Frozen album choices preserve order, aliases and installed completeness gates."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_album_owner_preparation import finish, seed
from tests.store_fixture_support import guarded_numbered_store as guarded_installed
from tests.store_fixture_support import (
    numbered_store as installed, preparation_ddl_connection, publication_consumer_reads,
)
from yt_playlist.core import album_tracklists
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import album_shares as preparation, album_share_publication as publication
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Album SQL and file publication do not start the HTTP lifecycle."""


def complete_source(store, count=3):
    album, entry = seed(store, count=count)
    with store.conn:
        store.conn.execute('DELETE FROM main.album_tracks WHERE album_id=? AND is_full=1', (album,))
        store.conn.executemany("INSERT INTO main.album_tracks(album_id,entry_id,is_full,source) VALUES (?,?,1,'legacy')", [(album,entry)]*3)
    assert store.collection.is_album_tracklist_complete('release')
    return album, entry


def stage(conn, size=128):
    preparation.JOB.install(conn)
    finish(preparation.JOB, conn)
    for _ in range(100):
        result = publication.OWNER.step(conn, batch_size=size)
        if result.complete:
            return result.id
    pytest.fail('Album share map did not finish bounded fixture')


def rejection_inputs(store):
    complete_source(store)
    preparation.JOB.install(store.conn)
    finish(preparation.JOB, store.conn)
    assert publication.OWNER.selected(store.conn) is None


@pytest.mark.parametrize('installed', [79, 93, 98], indirect=True)
def test_preopened_readers_use_exact_choices_aliases_and_existing_completeness(installed):
    store, _ = installed
    album, _ = complete_source(store)
    with store.conn:
        store.conn.execute("INSERT INTO main.album_provider_ids(album_id,provider,provider_id,is_primary,equivalence_digest,verified_at) "
            "VALUES (?,'youtube','alias',0,'verified-fixture',1)", (album,))
    original = dict(store.conn.execute('SELECT * FROM main.album_share_snapshots').fetchone())
    preopened = Store(store._db_path)
    try:
        preopened.init_schema(defer_metadata=True)
        selected = stage(store.conn, size=1)
        publication.OWNER.publish(store.conn, stage=selected)
        def authorizer(operation, owner, column, *unused):
            if operation == sqlite3.SQLITE_READ and owner == 'album_share_snapshots' and column in ('title','video_ids_json'):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        preopened.conn.set_authorizer(authorizer)
        try:
            expected = {'title':'Frozen 東京', 'video_ids':['one','missing','one']}
            assert preopened.collection.cached_album_share('release') == expected
            assert preopened.collection.cached_album_share('alias') == expected
            assert preopened.collection.cached_album_share('unknown') is None
        finally:
            preopened.conn.set_authorizer(None)
        assert publication.OWNER.value(store.conn, selected, 'release') == original
        assert not store.conn.execute("SELECT 1 FROM main.youtube_items WHERE video_id='missing'").fetchone()
        regular.validate(preopened.conn)
        with store.conn:
            store.conn.execute('UPDATE main.album_fetch_state SET expected_track_count=4 WHERE album_id=?', (album,))
        assert preopened.collection.cached_album_share('release') is None
        # Share custody does not turn an incomplete provider tracklist complete.
        assert publication.OWNER.selected(store.conn) == selected
    finally:
        preopened.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.collection.cached_album_share('release') is None
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['fields','member','reference','extra','omitted','original','plan'])
def test_wrong_header_choices_references_coverage_or_files_cannot_publish(installed, preparation_ddl_connection, failure):
    store, _ = installed
    complete_source(store)
    conn = preparation_ddl_connection
    preparation.JOB.install(conn)
    finish(preparation.JOB, conn)
    assert publication.OWNER.selected(conn) is None
    header = preparation.head(conn, 'release')
    with conn:
        if failure == 'fields':
            conn.execute("UPDATE prepared_album_share_generations SET title='different'")
        elif failure == 'member':
            conn.execute("UPDATE prepared_album_share_items SET raw_source_ref='different' WHERE position=0")
        elif failure == 'reference':
            conn.execute("UPDATE prepared_album_share_items SET entry_id=NULL,recording_id=NULL,resolution_status='unresolved' WHERE position=0")
        elif failure == 'extra':
            conn.execute('INSERT INTO prepared_album_share_items SELECT source,preparation_revision,999,raw_source_ref,entry_id,recording_id,'
                'captured_title,captured_credit,resolution_status FROM prepared_album_share_items WHERE position=0')
        elif failure == 'omitted':
            conn.execute('DELETE FROM prepared_album_share_heads')
        else:
            filename = header['plan_filename'] if failure == 'plan' else conn.execute('SELECT filename FROM prepared_album_share_parts LIMIT 1').fetchone()[0]
            Path(filename).write_bytes(b'corrupt')
    selected = publication.OWNER.step(conn).id
    with pytest.raises((MigrationError, ValueError, OSError)):
        publication.OWNER.publish(conn, stage=selected)
    assert publication.OWNER.selected(conn) is None
    regular.migrate(conn)


@pytest.mark.parametrize('guarded_installed', [93], indirect=True)
def test_real_cache_writer_and_snapshots_reconcile_order_without_changing_archives(guarded_installed):
    store, _ = guarded_installed
    _, entry = complete_source(store, count=8192)
    selected = stage(store.conn)
    publication.OWNER.publish(store.conn, stage=selected)
    key = store.conn.execute('SELECT r.identity_key FROM main.catalog_entries e JOIN main.recordings r ON r.id=e.recording_id WHERE e.id=?', (entry,)).fetchone()[0]
    with store.read_snapshot() as snapshot:
        assert len(snapshot.store.collection.cached_album_share('release')['video_ids']) == 8192
        store.collection.cache_album_tracklist('release', 'Changed', 'Artist', None, [key]*3,
            entries=[{'entry_id':entry}]*3, video_ids=['one','one','one'])
        assert not snapshot.publish(lambda:None)
    assert publication.OWNER.selected(store.conn) is None
    assert store.collection.cached_album_share('release') == {'title':'Changed', 'video_ids':['one']*3}
    publication.OWNER.publish(store.conn, stage=stage(store.conn))
    assert store.collection.cached_album_share('release') == {'title':'Changed', 'video_ids':['one']*3}
    assert len(json.loads(publication.OWNER.value(store.conn, selected, 'release')['video_ids_json'])) == 8192
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM prepared_album_share_items')


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_reference_race_and_interrupted_cutover_leave_no_mixed_selection(installed):
    store, _ = installed
    complete_source(store)
    selected = stage(store.conn)
    other = sqlite3.connect(store._db_path)
    changed = False
    def race(conn, header):
        nonlocal changed
        publication.verify_document(conn, header)
        if not changed:
            changed = True
            with other:
                other.execute("UPDATE youtube_items SET title='Continuing metadata' WHERE video_id='one'")
    try:
        with pytest.raises(MigrationError, match='changed|reconciled'):
            replace(publication.OWNER, verify_document=race).publish(store.conn, stage=selected)
    finally:
        other.close()
    selected = stage(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_album_share_cutover BEFORE INSERT ON '+publication.OWNER.table('head')+
            " BEGIN SELECT RAISE(ABORT,'controlled cutover'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled cutover'):
        publication.OWNER.publish(store.conn, stage=selected)
    assert not store.conn.execute('SELECT 1 FROM '+publication.OWNER.table('generations')).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_album_share_cutover')
    publication.OWNER.publish(store.conn, stage=selected)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_missing_files_and_failed_preparation_remain_independent_of_ddl(installed, monkeypatch):
    store, _ = installed
    complete_source(store)
    selected = stage(store.conn)
    header = preparation.head(store.conn, 'release')
    Path(header['plan_filename']).unlink()
    Path(store.conn.execute('SELECT filename FROM prepared_album_share_parts LIMIT 1').fetchone()[0]).unlink()
    with pytest.raises(FileNotFoundError):
        publication.OWNER.publish(store.conn, stage=selected)
    regular.migrate(store.conn)
    publication.repair(store.conn, 'release')
    publication.OWNER.publish(store.conn, stage=selected)
    with store.conn:
        store.conn.execute("UPDATE main.album_share_snapshots SET title='Continuing' WHERE browse_id='release'")
    publish = preparation.files.publish
    def fail(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled album share file failure')
    monkeypatch.setattr(preparation.files, 'publish', fail)
    with pytest.raises(OSError, match='controlled album share'):
        preparation.JOB.step(store.conn)
    failed = preparation.JOB.progress(store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_album_share_publication(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_album_share_publication'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1,
        'independent_album_share_publication', install, validate)))
    regular.migrate(store.conn)
    assert preparation.JOB.progress(store.conn) == failed
    monkeypatch.setattr(preparation.files, 'publish', publish)
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.JOB.step(store.conn)
    preparation.JOB.step(store.conn, retry=True)
    publication.OWNER.publish(store.conn, stage=stage(store.conn))


@pytest.mark.parametrize('installed', [98], indirect=True)
def test_frozen_native_owner_has_precedence_without_losing_map_custody(installed):
    store, _ = installed
    path = store._db_path
    try:
        store.init_schema(defer_metadata=True)
        complete_source(store)
        selected = stage(store.conn)
        publication.OWNER.publish(store.conn, stage=selected)
        original = publication.OWNER.value(store.conn, selected, 'release')
        store.conn.close()
        store = Store(path)
        store.conn.execute('PRAGMA synchronous=OFF')
        runner.migrate(store.conn, registry=registry.MIGRATIONS)
        store.init_schema(defer_metadata=True)
        assert album_tracklists.available(store.conn)
        assert publication.OWNER.selected(store.conn) is None
        assert store.collection.cached_album_share('release') == {'title':'Frozen 東京','video_ids':['one','missing','one']}
        assert publication.OWNER.value(store.conn, selected, 'release') == original
        regular.validate(store.conn)
        with store.conn:
            store.conn.execute('DROP TRIGGER prepared_album_share_items_mapped_delete')
        with pytest.raises(MigrationError, match='publication object'):
            regular.validate(store.conn)
    finally:
        store.conn.close()
