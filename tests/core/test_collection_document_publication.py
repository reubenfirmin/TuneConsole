"""Saved-document maps preserve mixed generations, deletion and continuing use."""
from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from time import perf_counter

import pytest

from tests.publication_fixture_support import installed, installed_reader
from tests.store_fixture_support import numbered_store as unadmitted_installed
from tests.core.test_collection_document_preparation import seed
from tests.store_fixture_support import (
    mock_regular_numbered_input, native_sql_calls, publication_consumer_reads,
    preparation_ddl_connection, without_query_timing,
)
from yt_playlist.core import schema_inspection
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import collection_documents as preparation, collection_document_publication as publication
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing', 'publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Saved-document publication checks do not construct an HTTP application."""


def prepare(owner, conn):
    owner.worker.install(conn)
    for _ in range(2000):
        progress = owner.worker.step(conn, batch_size=128)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Saved-document preparation did not finish bounded fixture')


def stage(owner, conn, size=128):
    prepare(owner, conn)
    for _ in range(100):
        result = owner.step(conn, batch_size=size)
        if result.complete:
            return result.id
    pytest.fail('Publication map did not finish bounded fixture')


@pytest.mark.parametrize('installed', [79, 93, 96], indirect=True)
def test_preopened_and_reopened_readers_find_exact_documents_and_timestamps(installed, monkeypatch):
    store, _ = installed
    _, _, recipe, canvas = seed(store)
    with installed_reader(store._db_path, monkeypatch) as other:
        for owner in publication.OWNERS.values():
            identity = stage(owner, store.conn)
            owner.publish(store.conn, stage=identity)
            assert owner.selected(other.conn) == identity
        assert other.playlist_recipes.get_recipe('PLAYLIST') == recipe
        assert other.playlist_recipes.get_cluster_canvas('PLAYLIST') == canvas
        assert other.playlist_recipes.get_recipe_created_ats() == {'PLAYLIST':3}
        assert other.playlist_recipes.get_recipe('missing') is None
        regular.validate(other.conn)
    with installed_reader(store._db_path, monkeypatch) as reopened:
        assert reopened.playlist_recipes.get_recipe('PLAYLIST') == recipe
        assert reopened.playlist_recipes.get_cluster_canvas('PLAYLIST') == canvas


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_maps_preserve_mixed_generations_and_deleted_documents(installed):
    store, _ = installed
    _, _, recipe, canvas = seed(store)
    store.playlist_recipes.set_recipe('unchanged', {'media':'video'}, None)
    owner = publication.OWNERS['recipes']
    old = stage(owner, store.conn, size=1)
    owner.publish(store.conn, stage=old)
    store.playlist_recipes.set_recipe('PLAYLIST', {'media':'video', 'facets':None}, 5)
    new = stage(owner, store.conn, size=1)
    generations = list(store.conn.execute('SELECT generation FROM '+owner.table('map')+' WHERE stage=?', (new,)))
    assert len({row[0] for row in generations}) == 2
    assert json.loads(owner.value(store.conn, old, 'PLAYLIST')) == recipe
    owner.publish(store.conn, stage=new)
    assert store.playlist_recipes.get_recipe_created_ats() == {'PLAYLIST':5, 'unchanged':None}
    with store.conn:
        store.conn.execute("DELETE FROM rec_recipes WHERE playlist_ytm='PLAYLIST'")
    assert owner.selected(store.conn) is None
    deleted = stage(owner, store.conn)
    owner.publish(store.conn, stage=deleted)
    assert store.playlist_recipes.get_recipe('PLAYLIST') is None
    assert store.playlist_recipes.get_recipe_created_ats() == {'unchanged':None}
    assert json.loads(owner.value(store.conn, old, 'PLAYLIST')) == recipe
    canvas_owner = publication.OWNERS['canvases']
    first = stage(canvas_owner, store.conn)
    canvas_owner.publish(store.conn, stage=first)
    store.playlist_recipes.delete_cluster_canvas('PLAYLIST')
    canvas_owner.publish(store.conn, stage=stage(canvas_owner, store.conn))
    assert store.playlist_recipes.get_cluster_canvas('PLAYLIST') is None
    assert canvas_owner.value(store.conn, first, 'PLAYLIST') == canvas


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_mapping_is_bounded_resumable_and_restarts_after_intervening_source_writes(installed, record_property):
    store, _ = installed
    # Exercise three batches and a one-item remainder; the separate default
    # boundary control below retains the production 128-link limit.
    began = perf_counter()
    for index in range(7):
        store.playlist_recipes.set_cluster_canvas(f'canvas-{index:04d}', 'state '+str(index), index)
    record_property('canvas_source_seconds', perf_counter() - began)
    owner = publication.OWNERS['canvases']
    began = perf_counter()
    prepare(owner, store.conn)
    record_property('canvas_preparation_seconds', perf_counter() - began)
    began = perf_counter()
    first = owner.step(store.conn, batch_size=3)
    assert first.copied == 3 and not first.complete
    with pytest.raises(MigrationError, match='incomplete'):
        owner.publish(store.conn, stage=first.id)
    second = owner.step(store.conn, batch_size=3)
    assert second.id == first.id and second.copied == 6 and not second.complete
    reopened = Store(store._db_path)
    try:
        completed = owner.step(reopened.conn, batch_size=3)
        assert completed.id == first.id and completed.copied == 7 and completed.complete
        owner.publish(reopened.conn, stage=completed.id)
    finally:
        reopened.conn.close()
    record_property('canvas_map_and_publication_seconds', perf_counter() - began)
    assert store.playlist_recipes.get_cluster_canvas('canvas-0006') == 'state 6'
    store.playlist_recipes.set_cluster_canvas('canvas-0000', 'changed', None)
    prepare(owner, store.conn)
    partial = owner.step(store.conn, batch_size=1)
    store.playlist_recipes.set_cluster_canvas('canvas-0001', 'changed again', None)
    prepare(owner, store.conn)
    restarted = owner.step(store.conn, batch_size=1)
    assert restarted.id != partial.id and restarted.copied == 1
    assert owner.stage(store.conn, partial.id) == partial
    assert owner.stage(store.conn, completed.id) == completed
    assert owner.value(store.conn, first.id, 'canvas-0000') == 'state 0'


@pytest.mark.parametrize('unadmitted_installed', [93], indirect=True)
def test_canvas_default_mapping_batch_stops_at_128_real_prepared_documents(unadmitted_installed, preparation_ddl_connection, record_property):
    store, _ = unadmitted_installed
    conn = preparation_ddl_connection
    began = perf_counter()
    for index in range(129):
        store.playlist_recipes.set_cluster_canvas(f'boundary-{index:04d}', 'state '+str(index), index)
    record_property('canvas_boundary_source_seconds', perf_counter() - began)
    owner = publication.OWNERS['canvases']
    began = perf_counter()
    prepare(owner, conn)
    record_property('canvas_boundary_preparation_seconds', perf_counter() - began)
    began = perf_counter()
    first = owner.step(conn)  # Use the production default, not a supplied size.
    assert first.copied == 128 and not first.complete
    links = list(conn.execute('SELECT source FROM '+owner.table('map')+' WHERE stage=? ORDER BY source', (first.id,)))
    assert [row[0] for row in links] == [f'boundary-{index:04d}' for index in range(128)]
    with pytest.raises(ValueError, match='1 to 128'):
        owner.step(conn, batch_size=129)
    assert owner.stage(conn, first.id) == first
    completed = owner.step(conn)
    assert completed.id == first.id and completed.copied == 129 and completed.complete
    assert owner.value(conn, completed.id, 'boundary-0128') == 'state 128'
    assert owner.selected(conn) is None
    record_property('canvas_boundary_mapping_seconds', perf_counter() - began)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_partial_map_interruption_rolls_back_cursor_links_and_custody(installed):
    store, _ = installed
    for key in ('a', 'b', 'c'):
        store.playlist_recipes.set_cluster_canvas(key, key)
    owner = publication.OWNERS['canvases']
    prepare(owner, store.conn)
    first = owner.step(store.conn, batch_size=1)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_map_interrupt BEFORE INSERT ON '+owner.table('map')+
            " WHEN NEW.source='b' BEGIN SELECT RAISE(ABORT,'controlled map interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled map interruption'):
        owner.step(store.conn, batch_size=1)
    assert owner.stage(store.conn, first.id) == first
    assert store.conn.execute('SELECT count(*) FROM '+owner.table('map')).fetchone()[0] == 1
    assert store.conn.execute('SELECT count(*) FROM '+owner.table('custody')).fetchone()[0] == 1
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_map_interrupt')
    owner.publish(store.conn, stage=stage(owner, store.conn, size=1))
    assert store.playlist_recipes.get_cluster_canvas('b') == 'b'


@pytest.mark.parametrize('unadmitted_installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['metadata', 'facet', 'extra', 'original', 'recovery', 'plan'])
def test_unverified_fields_members_or_files_cannot_select_saved_documents(
        unadmitted_installed, failure, preparation_ddl_connection):
    store, _ = unadmitted_installed
    conn = preparation_ddl_connection
    seed(store)
    owner = publication.OWNERS['recipes']
    prepare(owner, conn)
    header = preparation.head(conn, 'recipes', 'PLAYLIST')
    with conn:
        if failure == 'metadata':
            conn.execute('UPDATE '+owner.header+" SET media='video'")
        elif failure == 'facet':
            conn.execute('UPDATE '+preparation.table('recipes', 'facets')+" SET captured_value='changed' WHERE kind='genre'")
        elif failure == 'extra':
            conn.execute('INSERT INTO '+preparation.table('recipes', 'facets')+
                " SELECT source,preparation_revision,kind,99,captured_value,axis_digest,axis_ref,axis_kind,genre_key,family_key,value,resolution_reason FROM "+
                preparation.table('recipes', 'facets')+" WHERE kind='genre' AND position=0")
        else:
            filename = (header[failure+'_filename'] if failure != 'original' else conn.execute(
                'SELECT filename FROM '+preparation.table('recipes', 'parts')+' LIMIT 1').fetchone()[0])
            Path(filename).write_bytes(b'corrupt')
    identity = owner.step(conn).id
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(conn, stage=identity)
    assert owner.selected(conn) is None
    regular.migrate(conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_completed_maps_and_captured_members_are_immutable_before_and_after_publication(installed):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['recipes']
    identity = stage(owner, store.conn)
    for sql in ('UPDATE '+owner.header+' SET created_at=created_at',
                'DELETE FROM '+owner.table('map'), 'UPDATE '+owner.table('stages')+' SET copied=0',
                'DELETE FROM '+owner.table('custody'), 'INSERT INTO '+owner.table('map')+
                ' SELECT stage,source||\'extra\',generation FROM '+owner.table('map')):
        with pytest.raises(sqlite3.IntegrityError, match='retained|Retained'):
            with store.conn:
                store.conn.execute(sql)
    owner.publish(store.conn, stage=identity)
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM '+preparation.table('recipes', 'facets'))


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_source_race_and_cutover_interruption_cannot_publish_a_mixed_map(installed):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['recipes']
    identity = stage(owner, store.conn)
    other = sqlite3.connect(store._db_path, timeout=.01)
    def race(conn, header):
        publication.verify_document('recipes', conn, header)
        with other:
            other.execute('UPDATE rec_recipes SET created_at=created_at')
    try:
        with pytest.raises(MigrationError, match='changed|reconciled'):
            replace(owner, verify_document=race).publish(store.conn, stage=identity)
    finally:
        other.close()
    assert owner.selected(store.conn) is None
    identity = stage(owner, store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_document_cutover BEFORE INSERT ON '+owner.table('head')+
            " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled cutover interruption'):
        owner.publish(store.conn, stage=identity)
    assert not store.conn.execute('SELECT 1 FROM '+owner.table('generations')).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_document_cutover')
    owner.publish(store.conn, stage=identity)
    assert owner.selected(store.conn) == identity


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_real_writers_snapshot_invalidation_and_owner_independence(installed):
    store, _ = installed
    _, _, _, canvas = seed(store)
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, stage=stage(owner, store.conn))
    with store.read_snapshot() as snapshot:
        assert snapshot.store.playlist_recipes.get_recipe_created_ats() == {'PLAYLIST':3}
        assert snapshot.store.playlist_recipes.get_recipe('PLAYLIST')
        store.playlist_recipes.set_recipe('PLAYLIST', {'media':'video'}, 20)
        assert not snapshot.publish(lambda:None)
    assert publication.OWNERS['recipes'].selected(store.conn) is None
    assert publication.OWNERS['canvases'].selected(store.conn) is not None
    assert store.playlist_recipes.get_cluster_canvas('PLAYLIST') == canvas
    assert store.playlist_recipes.get_recipe('PLAYLIST') == {'media':'video'}
    assert store.playlist_recipes.get_recipe_created_ats() == {'PLAYLIST':20}
    owner = publication.OWNERS['recipes']
    owner.publish(store.conn, stage=stage(owner, store.conn))
    assert store.playlist_recipes.get_recipe('PLAYLIST') == {'media':'video'}


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', ['recipes', 'canvases'])
def test_missing_files_can_be_repaired_without_rewriting_mapped_rows_or_blocking_ddl(
        installed, name, preparation_ddl_connection):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS[name]
    identity = stage(owner, store.conn)
    owner.publish(store.conn, stage=identity)
    original = owner.value(store.conn, identity, 'PLAYLIST')
    header = owner.capture(store.conn, identity, 'PLAYLIST')
    Path(header['plan_filename']).unlink()
    Path(header['recovery_filename']).unlink()
    Path(store.conn.execute('SELECT filename FROM '+preparation.table(name, 'parts')+' LIMIT 1').fetchone()[0]).unlink()
    with pytest.raises(OSError):
        owner.value(store.conn, identity, 'PLAYLIST')
    store.init_schema(defer_metadata=True)
    regular.migrate(preparation_ddl_connection)
    publication.repair(name, store.conn, 'PLAYLIST')
    owner.publish(store.conn, stage=identity)
    assert owner.value(store.conn, identity, 'PLAYLIST') == original


@pytest.mark.parametrize('unadmitted_installed', [85], indirect=True)
def test_new_optional_axis_owner_withdraws_selection_without_blocking_regular_ddl(unadmitted_installed, tmp_path, monkeypatch):
    # This edge performs real DDL and publication without taking a Store
    # snapshot, so its completed source needs no unused TEMP revision guards.
    store, _ = unadmitted_installed
    seed(store)
    owner = publication.OWNERS['recipes']
    owner.publish(store.conn, stage=stage(owner, store.conn))
    upgraded = Store(tmp_path/'recipe-optional-owner.db')
    with closing(upgraded.conn), schema_inspection.reuse_tables(upgraded.conn):
        upgraded.conn.execute('PRAGMA synchronous=OFF')
        store.conn.backup(upgraded.conn)
        upgraded.conn.execute('PRAGMA journal_mode=WAL')
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:88])
        assert owner.selected(upgraded.conn) is None
        with monkeypatch.context() as patch:
            mock_regular_numbered_input(patch, upgraded.conn)
            regular.migrate(upgraded.conn)
        owner.publish(upgraded.conn, stage=stage(owner, upgraded.conn))


@pytest.mark.parametrize('installed', [88], indirect=True)
def test_new_axis_withdraws_recipe_selection_and_republication_captures_its_identity(installed):
    from yt_playlist.core import taste_axes

    store, _ = installed
    seed(store)
    owner = publication.OWNERS['recipes']
    owner.publish(store.conn, stage=stage(owner, store.conn))
    with store.conn:
        axis = taste_axes.identity(store.conn, 'genre:Unknown original', create=True)
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, stage=stage(owner, store.conn))
    assert store.conn.execute('SELECT axis_ref FROM '+preparation.table('recipes', 'facets')+
        " WHERE source='PLAYLIST' AND kind='genre' ORDER BY preparation_revision DESC LIMIT 1").fetchone()[0] == axis


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw', ['', 'verbatim { not JSON\n東京'])
def test_empty_namespaces_and_blank_or_verbatim_canvas_text_remain_usable(installed, raw):
    store, _ = installed
    for owner in publication.OWNERS.values():
        identity = stage(owner, store.conn)
        owner.publish(store.conn, stage=identity)
        assert owner.stage(store.conn, identity).copied == 0
    assert store.playlist_recipes.get_recipe_created_ats() == {}
    assert store.playlist_recipes.get_recipe('missing') is None
    store.playlist_recipes.set_cluster_canvas('nullable', raw, None)
    owner = publication.OWNERS['canvases']
    identity = stage(owner, store.conn)
    owner.publish(store.conn, stage=identity)
    assert owner.value(store.conn, identity, 'nullable', 'absent') == raw
    assert owner.value(store.conn, identity, 'missing', 'absent') == 'absent'
    assert store.playlist_recipes.get_cluster_canvas('nullable') == raw
