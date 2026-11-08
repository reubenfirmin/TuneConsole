"""Independent selection checks for retained metadata and operational inputs."""
import json
from tests.enrichment_support import choose_field
from tests.metadata_provenance_support import metadata_provenance
import sqlite3

import pytest

from tests.core.test_metadata_owner_cutover import predecessor as metadata_predecessor, seed
from tests.core.test_metadata_preparation import finish
from yt_playlist.core import catalog, genre_identity, metadata
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import metadata_evidence as preparation, metadata_publication as publication


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Metadata publication does not construct the HTTP application."""


@pytest.fixture
def predecessor(metadata_predecessor):
    # These cases check committed SQL and file publication, without simulating
    # a machine power failure between WAL synchronization points.
    metadata_predecessor.conn.execute('PRAGMA synchronous=NORMAL')
    return metadata_predecessor


def publish(conn):
    for _ in range(200):
        current = publication.step(conn,batch_size=2)
        if current.complete:
            publication.publish(conn,identity=current.id)
            return current.id
    pytest.fail('Metadata publication did not finish')


def rejection_inputs(store):
    seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    assert publication.selected(store.conn) is None


def test_selected_sources_keep_exact_values_and_operational_reads_use_typed_fields(predecessor):
    store,conn = predecessor,predecessor.conn
    entry,recording,key = seed(store)
    attempts = store.enrichment_state.enrichment_attempts(key)
    states = store.enrichment_state.enrichment_provider_state(recording,include_previous=True)
    provenance = metadata_provenance(store, entry)
    native = store.enrichment_state.metadata_observations(key)
    originals = {owner:list(map(tuple,conn.execute('SELECT * FROM '+owner))) for owner in preparation.KEYS}
    preparation.JOB.install(conn)
    finish(conn)
    publish(conn)
    denied = {'metadata_findings','provider_attempts','enrichment_provider_state','metadata_decisions',
        'recording_field_sources','item_field_sources'}
    def authorize(action,owner,column,*rest):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and owner in denied else sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    try:
        assert store.enrichment_state.enrichment_attempts(key)==attempts
        assert store.enrichment_state.enrichment_provider_state(recording,include_previous=True)==states
        assert store.enrichment_state.metadata_observations(key)==native
        assert metadata_provenance(store, entry)==provenance
        assert metadata.selected_source(conn,entry,'bpm')=='a'
    finally:
        conn.set_authorizer(None)
    for owner,expected in originals.items():
        order = ','.join(preparation.KEYS[owner])
        assert list(map(tuple,conn.execute('SELECT * FROM '+publication.read_table(conn,owner)+' ORDER BY '+order))) == list(
            map(tuple,conn.execute('SELECT * FROM '+owner+' ORDER BY '+order)))
    with pytest.raises(sqlite3.IntegrityError,match='[Rr]etained'):
        with conn:
            conn.execute('UPDATE '+publication.table('operational_fields')+" SET raw_value='null'")
    with pytest.raises(sqlite3.IntegrityError,match='[Rr]etained'):
        with conn:
            conn.execute('UPDATE '+preparation.ORIGINALS+" SET source_snapshot='{}'")


def test_continuing_provider_choice_reference_and_delete_updates_withdraw_selection(predecessor):
    store,conn = predecessor,predecessor.conn
    entry,recording,key = seed(store)
    preparation.JOB.install(conn)
    finish(conn)
    old = publish(conn)
    store.enrichment_state.record_enrichment_attempt(key,'a',{'bpm':'','genre':None,'isrc':['same','same']},video_id='video',now=20)
    assert publication.selected(conn) is None
    assert store.enrichment_state.enrichment_attempts(key)['a']['fields']['bpm']==''
    finish(conn)
    assert publish(conn)!=old
    assert store.enrichment_state.enrichment_attempts(key)['a']['fields']['isrc']==['same','same']
    with conn:
        catalog.choose_field(conn,entry,'genre',None,'user')
    assert publication.selected(conn) is None
    finish(conn)
    publish(conn)
    assert metadata_provenance(store, entry)['fields']['genre']['value'] is None
    with conn:
        genre_identity.add_custom(conn,'New publication label')
    assert publication.selected(conn) is None
    finish(conn)
    publish(conn)
    with conn:
        conn.execute('DELETE FROM provider_attempts')
    assert publication.selected(conn) is None
    finish(conn)
    publish(conn)
    assert store.enrichment_state.enrichment_attempts(key)=={}


@pytest.mark.parametrize('damage',[
    "UPDATE prepared_metadata_evidence SET recording_id=NULL WHERE owner='metadata_findings'",
    "UPDATE prepared_metadata_operational_fields SET raw_value='null' WHERE field='bpm'",
    "UPDATE prepared_metadata_operational_fields SET position=99 WHERE field='bpm'",
    "DELETE FROM prepared_metadata_evidence WHERE owner='entry_field_choices'",
])
def test_corrupt_preparation_is_not_publishable_and_regular_schema_remains_independent(
        predecessor, damage):
    conn = predecessor.conn
    rejection_inputs(predecessor)
    with conn:
        conn.execute(damage)
    with pytest.raises(MigrationError,match='differ|missing|extra'):
        publish(conn)
    assert publication.selected(conn) is None
    regular.migrate(conn)


def test_stale_verification_and_interrupted_selection_preserve_owner(predecessor,monkeypatch):
    conn = predecessor.conn
    seed(predecessor)
    preparation.JOB.install(conn)
    finish(conn)
    identity = publish(conn)
    conn.execute('CREATE TEMP TRIGGER abort_metadata_selection AFTER UPDATE ON main.'+publication.table('head')+
        " BEGIN SELECT RAISE(ABORT,'controlled publication interruption'); END")
    with pytest.raises(sqlite3.IntegrityError,match='controlled'):
        publication.publish(conn,identity=identity)
    assert publication.selected(conn)==identity
    conn.execute('DROP TRIGGER abort_metadata_selection')
    verify = publication.verify
    def stale(connection,identity):
        captured = verify(connection,identity)
        with connection:
            connection.execute('UPDATE provider_attempts SET at=20')
        return captured
    monkeypatch.setattr(publication,'verify',stale)
    with pytest.raises(MigrationError,match='reconciled|changed'):
        publication.publish(conn,identity=identity)
    assert publication.selected(conn) is None


@pytest.mark.parametrize('owner,column', [('metadata_findings','value_json'),('enrichment_log','value'),
    ('metadata_decisions','legacy_ref'),('enrichment_provider_state','error')])
def test_all_source_payloads_are_clipped_before_transfer_and_fail_without_truncation(
        predecessor, owner, column):
    conn = predecessor.conn
    seed(predecessor)
    raw = 'x'*(preparation.MAX_SOURCE_BYTES+1)
    keys = preparation.KEYS[owner]
    key = conn.execute('SELECT '+','.join(keys)+' FROM '+owner+' LIMIT 1').fetchone()
    with conn:
        conn.execute('UPDATE '+owner+' SET '+column+'=? WHERE '+
            ' AND '.join(name+'=?' for name in keys),(raw,*key))
    preparation.JOB.install(conn)
    with pytest.raises(MigrationError,match='byte budget'):
        finish(conn)
    assert preparation.JOB.progress(conn).status=='failed'
    regular.migrate(conn)
    assert conn.execute('SELECT 1 FROM '+owner+' WHERE '+column+'=?',(raw,)).fetchone()
