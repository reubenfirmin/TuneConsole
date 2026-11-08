"""Metadata delivery custody and lineage never infer equal-value selection."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_metadata_owner_cutover import predecessor, seed
from tests.independent_schema_support import independent_regular_ddl
from tests.core.test_metadata_preparation import finish
from tests.metadata_provenance_support import metadata_provenance
from tests.core.test_metadata_publication import publish as read_publication
from yt_playlist.core import catalog
from yt_playlist.core.migrations import MigrationError, regular, migrate, registry
from yt_playlist.core.preparation import metadata_evidence as preparation, metadata_destinations as publication


def stage(conn):
    finish(conn)
    identity = read_publication(conn)
    for _ in range(100):
        current = publication.step(conn,identity=identity,batch_size=16)
        if current.complete:
            return identity
    pytest.fail('Metadata destinations did not finish')


def test_files_typed_observations_and_explicit_unresolved_selection_preserve_choices(predecessor):
    store,conn = predecessor,predecessor.conn
    entry,recording,key = seed(store)
    preparation.JOB.install(conn)
    expected_native = store.enrichment_state.metadata_observations(key)
    source = conn.execute('SELECT source_key,payload FROM rec_musical_sources LIMIT 1').fetchone()
    identity = stage(conn)
    publication.publish(conn,identity=identity)
    def deny(action,owner,column,*rest):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and (
            owner in ('metadata_findings','provider_attempts','enrichment_provider_state') or
            owner=='rec_musical_sources' and column=='payload') else sqlite3.SQLITE_OK
    conn.set_authorizer(deny)
    try:
        assert store.enrichment_state.metadata_observations(key)==expected_native
        assert publication.source_values(conn,source['source_key'])==json.loads(source['payload'])
        assert publication.publication.fields(conn,'provider_attempts','[1]') is not None
        pending = store.musical_profiles.source_input(source['source_key'])
        assert pending[0]['observations']==json.loads(source['payload'])
    finally:
        conn.set_authorizer(None)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        publication.source_values(conn,source['source_key'])
    finally:
        conn.set_trace_callback(None)
    queries = [query for query in statements if query.startswith('SELECT d.*,n.legacy_id')]
    assert queries
    for query in queries:
        details = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN '+query)]
        assert any(publication.table('native_source') in detail and all(key+'=?' in detail
            for key in ('stage','source_key','field')) for detail in details),details
    lineage = metadata_provenance(store, entry)['lineage']
    assert lineage['bpm']['selection']['attribution_status']=='legacy_unresolved'
    assert lineage['bpm']['selection']['observation_id'] is None
    assert lineage['bpm']['evidence'] is None
    assert lineage['genre']['choice']['observation']['value_kind']=='blank'
    delivery = lineage['genre']['choice']['delivery']
    assert Path(delivery['filename']).is_file()
    body = publication.files.read(delivery['filename'],delivery['digest'],delivery['byte_size'],delivery['byte_offset'])
    assert body['owner']=='entry_field_choices'
    assert json.loads(body['row']['value_json'])==''
    assert conn.execute('SELECT count(*) FROM '+publication.table('fields')+' WHERE stage=?',(identity,)).fetchone()[0]
    assert not conn.execute('SELECT 1 FROM '+publication.table('fields')+' f JOIN '+publication.table('observations')+
        ' o ON o.id=f.observation_id WHERE f.delivery_id<>o.delivery_id').fetchone()
    for owner in ('deliveries','observations','choices','fields','records'):
        with pytest.raises(sqlite3.IntegrityError,match='retained'):
            with conn:
                conn.execute('DELETE FROM '+publication.table(owner))
    with conn:
        catalog.choose_field(conn,entry,'genre',None,'user')
    assert publication.selected(conn) is None
    successor = stage(conn)
    publication.publish(conn,identity=successor)
    assert metadata_provenance(store, entry)['lineage']['genre']['choice']['observation']['value_kind']=='null'
    assert successor!=identity
    assert conn.execute('SELECT 1 FROM '+publication.table('choices')+' WHERE stage=?',(identity,)).fetchone()


def test_failed_file_batch_is_durable_and_retry_resumes_without_duplicates(predecessor,monkeypatch, independent_regular_ddl):
    conn = predecessor.conn
    seed(predecessor)
    preparation.JOB.install(conn)
    finish(conn)
    identity = read_publication(conn)
    publish_files = publication.files.publish_encoded
    def failed(*args):
        raise OSError('interrupted retained delivery write')
    monkeypatch.setattr(publication.files,'publish_encoded',failed)
    with pytest.raises(OSError,match='interrupted'):
        publication.step(conn,identity=identity,batch_size=2)
    error = conn.execute('SELECT error,failed_at FROM '+publication.table('failures')).fetchone()
    assert publication.stage(conn,identity) is None
    regular.migrate(conn)
    with pytest.raises(MigrationError,match='retry explicitly'):
        publication.step(conn,identity=identity,batch_size=2)
    assert tuple(conn.execute('SELECT error,failed_at FROM '+publication.table('failures')).fetchone())==tuple(error)
    monkeypatch.setattr(publication.files,'publish_encoded',publish_files)
    for _ in range(100):
        if publication.step(conn,identity=identity,batch_size=2,retry=True).complete:
            break
    publication.publish(conn,identity=identity)
    assert publication.selected(conn)==identity
    assert not conn.execute('SELECT 1 FROM '+publication.table('failures')).fetchone()
    assert conn.execute('SELECT count(*) FROM '+publication.table('records')).fetchone()[0]==conn.execute(
        'SELECT count(*) FROM '+preparation.TABLE).fetchone()[0]


def test_unknown_active_choice_is_retained_as_failed_input_without_selection(predecessor):
    conn = predecessor.conn
    entry,_,_ = seed(predecessor)
    with conn:
        conn.execute("UPDATE entry_field_choices SET value_json='{}' WHERE entry_id=? AND field='genre'",(entry,))
    preparation.JOB.install(conn)
    with pytest.raises(MigrationError,match='Unsupported active metadata choice'):
        finish(conn)
    assert publication.selected(conn) is None
    assert conn.execute("SELECT value_json FROM entry_field_choices WHERE entry_id=? AND field='genre'",(entry,)).fetchone()[0]=='{}'
    assert preparation.JOB.progress(conn).status=='failed'
    assert preparation.JOB.progress(conn).error


def test_interrupted_head_selection_is_atomic_and_restart_keeps_serving(predecessor):
    from yt_playlist.core.store import Store
    conn = predecessor.conn
    entry,_,key = seed(predecessor)
    preparation.JOB.install(conn)
    identity = stage(conn)
    before = dict(conn.execute('SELECT * FROM catalog_tracks WHERE id=?',(entry,)).fetchone())
    conn.execute('CREATE TEMP TRIGGER interrupt_delivery_cutover AFTER INSERT ON main.'+publication.table('head')+
        " BEGIN SELECT RAISE(ABORT,'interrupted delivery selection'); END")
    with pytest.raises(sqlite3.IntegrityError,match='interrupted'):
        publication.publish(conn,identity=identity)
    assert publication.selected(conn) is None
    conn.execute('DROP TRIGGER interrupt_delivery_cutover')
    reopened = Store(predecessor._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert dict(reopened.conn.execute('SELECT * FROM catalog_tracks WHERE id=?',(entry,)).fetchone())==before
        publication.publish(reopened.conn,identity=identity)
        assert publication.selected(reopened.conn)==identity
        assert reopened.enrichment_state.metadata_observations(key)
    finally:
        reopened.conn.close()


def test_profile_mismatch_blocks_selection_and_source_write_withdraws_it(predecessor):
    conn = predecessor.conn
    seed(predecessor)
    preparation.JOB.install(conn)
    identity = stage(conn)
    source = conn.execute('SELECT source_key,payload FROM rec_musical_sources LIMIT 1').fetchone()
    with conn:
        conn.execute("UPDATE rec_musical_sources SET payload='{}' WHERE source_key=?",(source['source_key'],))
    with pytest.raises(MigrationError,match='reconciled|incomplete|changed'):
        publication.publish(conn,identity=identity)
    identity = stage(conn)
    with pytest.raises(MigrationError,match='source projection differs'):
        publication.publish(conn,identity=identity)
    assert publication.selected(conn) is None
    with conn:
        conn.execute('UPDATE rec_musical_sources SET payload=? WHERE source_key=?',(source['payload'],source['source_key']))
    identity = stage(conn)
    publication.publish(conn,identity=identity)
    with conn:
        conn.execute('UPDATE rec_musical_sources SET revision=revision+1 WHERE source_key=?',(source['source_key'],))
    assert publication.selected(conn) is None


def test_file_failure_and_racing_source_never_select_incomplete_destinations(predecessor,monkeypatch, independent_regular_ddl):
    conn = predecessor.conn
    seed(predecessor)
    preparation.JOB.install(conn)
    identity = stage(conn)
    row = conn.execute('SELECT * FROM '+publication.table('deliveries')+' LIMIT 1').fetchone()
    Path(row['filename']).unlink()
    with pytest.raises(FileNotFoundError):
        publication.publish(conn,identity=identity)
    assert publication.selected(conn) is None
    regular.migrate(conn)
    publication.repair(conn,row['id'])
    assert publication.step(conn,identity=identity,retry=True).complete
    verify = publication.verify
    def changed(connection,identity):
        result = verify(connection,identity)
        with connection:
            connection.execute('UPDATE provider_attempts SET at=at')
        return result
    monkeypatch.setattr(publication,'verify',changed)
    with pytest.raises(MigrationError,match='reconciled|changed'):
        publication.publish(conn,identity=identity)
    assert publication.selected(conn) is None
