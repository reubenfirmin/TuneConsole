"""TV cutovers retain exact evidence and keep all continuing writers usable."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_tv_state_preparation import aliases, finish, seed
from tests.test_tv_stream import video
from tests.store_fixture_support import (
    guarded_numbered_store as guarded_installed,
    mock_preparation_schema,
    native_sql_calls,
    numbered_store as installed,
    preparation_ddl_connection,
    publication_consumer_reads,
    reuse_reporting_reader,
    without_query_timing,
)
from yt_playlist.core import schema_inspection, tv_recommendation_state as state
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import tv_publication as publication, tv_state as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """TV SQL and file publication do not start the HTTP lifecycle."""


def prepare(owner, conn, *, size=128):
    owner.worker.install(conn)
    return finish(owner.worker, conn, size=size).prepared_revision


def rejection_inputs(store, conn, *, surface, with_aliases):
    _, originals = seed(store)
    if with_aliases:
        aliases(conn)
    owner = publication.SOURCES[surface]
    revision = prepare(owner, conn)
    assert owner.selected(conn) is None
    return originals, revision


def publish_all(store):
    aliases(store.conn)
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(owner, store.conn))


def published_writer_inputs(store):
    values, originals = seed(store)
    publish_all(store)
    return values, originals


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_selected_presence_reads_no_display_document(installed, monkeypatch):
    store, _ = installed
    values, _ = seed(store)
    publish_all(store)
    def forbidden(*args, **kwargs):
        pytest.fail('Presence read a selected TV document')
    for owner in publication.OWNERS.values():
        monkeypatch.setattr(type(owner), 'value', forbidden)
    for surface in values:
        assert store.rec_proposals.has_proposals(surface)


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_all_owners_work_for_preopened_and_reopened_readers(installed, monkeypatch):
    store, _ = installed
    values, originals = seed(store)
    other = Store(store._db_path)
    try:
        other.conn.execute('PRAGMA synchronous=OFF')
        with schema_inspection.reuse_tables(other.conn):
            other.init_schema(defer_metadata=True)
        publish_all(store)
        with reuse_reporting_reader(other, monkeypatch):
            for surface, owner in publication.SOURCES.items():
                assert owner.selected(other.conn) == 0
                assert other.rec_proposals._proposal_json(surface) == originals[surface]
                assert other.rec_proposals.get_proposals(surface) == values[surface]
                assert other.rec_proposals.has_proposals(surface)
            assert not other._tv_recommendations
            assert other.tv.static_ids(2) == {'visual00001'}
            assert other.tv.receipt('10::request:with:colons') == values['tv_continuations']['10::request:with:colons']
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.conn.execute('PRAGMA synchronous=OFF')
        with schema_inspection.reuse_tables(reopened.conn):
            reopened.init_schema(defer_metadata=True)
        with reuse_reporting_reader(reopened, monkeypatch):
            for surface, value in values.items():
                assert reopened.rec_proposals.get_proposals(surface) == value
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_absence_empty_shapes_and_recreated_sources_remain_distinct(installed):
    store, _ = installed
    publish_all(store)
    for owner in publication.OWNERS.values():
        assert owner.selected(store.conn) == 0
        assert store.rec_proposals.get_proposals(owner.source) is None
        assert not store.rec_proposals.has_proposals(owner.source)
        store.rec_proposals.put_proposals(owner.source, {}, None)
        assert owner.selected(store.conn) is None
        owner.publish(store.conn, revision=prepare(owner, store.conn))
        assert store.rec_proposals.get_proposals(owner.source) == {}
        assert not store.rec_proposals.has_proposals(owner.source)
        revision = owner.selected(store.conn)
        with store.conn:
            store.conn.execute('DELETE FROM rec_proposals WHERE surface=?', (owner.source,))
        assert owner.selected(store.conn) is None
        owner.publish(store.conn, revision=prepare(owner, store.conn))
        assert store.rec_proposals.get_proposals(owner.source) is None
        assert owner.value(store.conn, revision) == '{}'


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('surface,column,value', [
    ('tv_pool', 'field_mask', 0), ('tv_artist_artwork', 'artist_id', 999),
    ('tv_artist_artwork_requests', 'requested_at', 99),
    ('tv_visual_checks', 'sample_times', 99), ('tv_continuations', 'target_stream', 'wrong'),
])
def test_each_owner_rejects_unverified_typed_state(installed, surface, column, value, preparation_ddl_connection):
    store, _ = installed
    conn = preparation_ddl_connection
    owner = publication.SOURCES[surface]
    originals, revision = rejection_inputs(store, conn, surface=surface, with_aliases=True)
    table = {'tv_pool':'tv_pool_state', 'tv_artist_artwork':'tv_artist_artwork',
        'tv_artist_artwork_requests':'tv_artist_requests', 'tv_visual_checks':'tv_visual_samples',
        'tv_continuations':'tv_continuation_receipts'}[surface]
    if column == 'sample_times':
        column = 'at'
    with conn:
        conn.execute('UPDATE '+preparation.relation(table)+' SET '+column+'=?'+
            (' WHERE resolution_reason IS NULL' if column == 'artist_id' else ''), (value,))
    with pytest.raises(MigrationError, match='differs'):
        owner.publish(conn, revision=revision)
    assert owner.selected(conn) is None
    assert store.rec_proposals._proposal_json(surface) == originals[surface]
    regular.migrate(preparation_ddl_connection)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['time', 'missing', 'extra', 'original'])
def test_time_completeness_and_original_file_are_verified(installed, failure, preparation_ddl_connection):
    store, _ = installed
    conn = preparation_ddl_connection
    owner = publication.OWNERS['pool']
    _, revision = rejection_inputs(store, conn, surface=owner.source, with_aliases=False)
    with conn:
        if failure == 'time':
            conn.execute('UPDATE '+preparation.tables(owner.source)[0]+' SET built_at=1')
        elif failure == 'missing':
            conn.execute('DELETE FROM '+preparation.relation('tv_pool_items')+' WHERE position=1')
        elif failure == 'extra':
            conn.execute('INSERT INTO '+preparation.relation('tv_pool_attempts')+
                '(source,preparation_revision,pool_id,position,raw_key,resolution_reason) VALUES (?,?,1,2,?,?)',
                (owner.source, revision, 'extra', 'missing_recording'))
        else:
            Path(conn.execute('SELECT filename FROM '+preparation.tables(owner.source)[2]).fetchone()[0]).write_bytes(b'corrupt')
    with pytest.raises((MigrationError, OSError, ValueError)):
        owner.publish(conn, revision=revision)
    assert owner.selected(conn) is None
    regular.migrate(preparation_ddl_connection)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('timing', ['files', 'rows'])
@pytest.mark.usefixtures('publication_consumer_reads')
def test_source_and_custody_races_cannot_select_stale_tv_state(installed, timing, monkeypatch):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['pool']
    revision = prepare(owner, store.conn)
    other = sqlite3.connect(store._db_path, timeout=.01)
    read = preparation.read_original
    def files(conn, header):
        assert not conn.in_transaction
        raw = read(conn, header)
        with other:
            other.execute('UPDATE rec_proposals SET built_at=1 WHERE surface=?', (owner.source,))
        return raw
    def rows(conn, header, value):
        publication.verify_rows(conn, header, value)
        with other:
            other.execute('UPDATE '+preparation.relation('tv_pool_state')+' SET at=99')
    try:
        if timing == 'files':
            monkeypatch.setattr(publication.TVPreparation, 'read_original', staticmethod(files))
            target = owner
        else:
            target = replace(owner, verify_rows=rows)
        with pytest.raises(MigrationError, match='changed|reconciled'):
            target.publish(store.conn, revision=revision)
        assert owner.selected(store.conn) is None
    finally:
        other.close()


@pytest.mark.parametrize('guarded_installed', [79, 93], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_real_continuing_writers_withdraw_selections_and_reconcile(guarded_installed):
    store, _ = guarded_installed
    values, originals = published_writer_inputs(store)
    with store.read_snapshot() as snapshot:
        assert snapshot.store.tv.receipt('10::request:with:colons') == values['tv_continuations']['10::request:with:colons']
        store.tv.complete_receipt('11:stream:next', {'playlist_id':21, 'stream':'continued', 'items':[video(3)]}, 99)
        assert not snapshot.publish(lambda:None)
    assert publication.OWNERS['continuations'].selected(store.conn) is None
    assert publication.OWNERS['pool'].selected(store.conn) == 0
    store.tv.request_artwork('new', 'Artist', 99)
    assert publication.OWNERS['artwork-requests'].selected(store.conn) is None
    assert store.tv.publish_artwork('new', {'images':['https://example.test/new'], 'at':99}, 99, (None, None), complete=True)
    assert publication.OWNERS['artwork'].selected(store.conn) is None
    assert 'new' not in store.rec_proposals.get_proposals('tv_artist_artwork_requests')
    store.tv.publish_visual('visual00002', {'verdict':'moving', 'version':2}, 99)
    store.rec_proposals.put_proposals('tv_pool', {'items':[video(4)], 'at':99}, 99)
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(owner, store.conn))
        assert owner.value(store.conn, 0) == originals[owner.source]
    assert store.tv.static_ids(2) == {'visual00001'}
    assert store.tv.receipt('11:stream:next')['items'] == [video(3)]
    regular.validate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_legacy_artwork_publication_survives_later_request_removal_failure(installed, monkeypatch):
    store, _ = installed
    values, _ = seed(store)
    publish_all(store)
    other = sqlite3.connect(store._db_path, timeout=.001)
    read = store.rec_proposals.get_proposals
    writes = store.rec_proposals.put_proposals
    checked = []
    def outside_transaction(surface):
        assert not store.conn.in_transaction
        other.execute('BEGIN IMMEDIATE')
        other.rollback()
        checked.append(surface)
        return read(surface)
    monkeypatch.setattr(store.rec_proposals, 'get_proposals', outside_transaction)
    try:
        store.tv.request_artwork('new', 'Artist', 99)
        store.tv.publish_visual('visual00002', {'verdict':'moving'}, 99)
        store.tv.complete_receipt('11:s:r', {'playlist_id':21, 'stream':'next'}, 99)
        def fail(surface, value, now):
            if surface == 'tv_artist_artwork_requests':
                raise RuntimeError('controlled request removal failure')
            return writes(surface, value, now)
        monkeypatch.setattr(store.rec_proposals, 'put_proposals', fail)
        with pytest.raises(RuntimeError, match='controlled request removal failure'):
            store.tv.publish_artwork('new', {'images':[]}, 99, (None, None), complete=True)
        monkeypatch.setattr(store.rec_proposals, 'get_proposals', read)
        assert checked
        assert read('tv_artist_artwork') == {**values['tv_artist_artwork'], 'new': {'images': []}}
        assert read('tv_artist_artwork_requests')['new'] == 'Artist'
        assert publication.OWNERS['artwork'].selected(store.conn) is None
        assert not store.conn.in_transaction
    finally:
        other.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_selected_custody_missing_file_and_failed_recovery_keep_ddl_independent(
        installed, monkeypatch, preparation_ddl_connection):
    store, _ = installed
    _, originals = seed(store)
    owner = publication.OWNERS['pool']
    owner.publish(store.conn, revision=prepare(owner, store.conn))
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('UPDATE '+preparation.relation('tv_pool_items')+' SET title=title')
    filename = store.conn.execute('SELECT filename FROM '+preparation.tables(owner.source)[2]).fetchone()[0]
    Path(filename).unlink()
    def fail(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled TV recovery failure')
    with pytest.raises(OSError, match='controlled TV recovery failure'):
        replace(owner.worker, repair_files=fail).step(store.conn, retry=True)
    before = owner.worker.progress(store.conn)
    assert before.status == 'failed'
    store.init_schema(defer_metadata=True)
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1, 'independent_tv_publication',
        lambda conn, context: conn.execute('CREATE TABLE independent_tv_publication(value TEXT)'),
        lambda conn: conn.execute('SELECT value FROM independent_tv_publication LIMIT 0')),))
    regular.migrate(preparation_ddl_connection)
    assert owner.worker.progress(store.conn) == before
    with pytest.raises(MigrationError, match='retry explicitly'):
        owner.worker.step(store.conn)
    owner.worker.step(store.conn, retry=True)
    owner.publish(store.conn, revision=owner.worker.progress(store.conn).prepared_revision)
    assert store.rec_proposals._proposal_json(owner.source) == originals[owner.source]


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_receipt_budget_numeric_affinity_and_interrupted_cutover(installed, monkeypatch):
    store, _ = installed
    value = {f'{number+1}:stream:request:{number}':{'playlist_id':number+101, 'stream':'next',
        'items':[video(index) for index in range(60)]} for number in range(60)}
    raw = json.dumps(value, indent=2)
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('tv_continuations', raw, 9007199254740993))
    original = tuple(store.conn.execute('SELECT * FROM rec_proposals WHERE surface=?', ('tv_continuations',)).fetchone())
    owner = publication.OWNERS['continuations']
    # Build the complete lower-level input, including its original files, once.
    # Keep every real 512-row SQL batch and both independent publication checks.
    prepare_input = owner.worker.prepare
    plans = {}
    def prepared_input(conn, row):
        key = tuple(row)
        if key not in plans:
            plans[key] = prepare_input(conn, row)
        return plans[key]
    owner = replace(owner, worker=replace(owner.worker, prepare=prepared_input))
    owner.worker.install(store.conn)
    mock_preparation_schema(monkeypatch, owner.worker.definition, store.conn)
    revision = finish(owner.worker, store.conn, size=512).prepared_revision
    assert store.conn.execute('SELECT count(*) FROM '+preparation.relation('tv_continuation_receipts')+
        ' WHERE preparation_revision=?', (revision,)).fetchone()[0] == 60
    assert store.conn.execute('SELECT count(*) FROM '+preparation.relation('tv_continuation_items')+
        ' WHERE preparation_revision=?', (revision,)).fetchone()[0] == 3600
    owner.install(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_tv_interrupt BEFORE INSERT ON '+owner.tables[1]+
            " BEGIN SELECT RAISE(ABORT,'controlled TV interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled TV interruption'):
        owner.publish(store.conn, revision=revision)
    assert not store.conn.execute('SELECT 1 FROM '+owner.tables[0]).fetchone()
    assert owner.selected(store.conn) is None and not store.conn.in_transaction
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_tv_interrupt')
    owner.publish(store.conn, revision=revision)
    assert store.rec_proposals._proposal_json(owner.source) == raw
    assert store.rec_proposals.get_proposals(owner.source) == value
    assert tuple(store.conn.execute('SELECT * FROM rec_proposals WHERE surface=?', (owner.source,)).fetchone()) == original
    assert not store.conn.in_transaction and not store.conn.execute('PRAGMA foreign_key_check').fetchone()
