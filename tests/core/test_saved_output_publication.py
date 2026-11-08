"""Saved outputs select exact files without blocking their installed writers."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_collection_control_preparation import finish
from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.independent_schema_support import independent_regular_ddl
from tests.core.test_saved_output_preparation import head, seed
from tests.store_fixture_support import assert_installed_consumer_input, publication_consumer_reads
from yt_playlist.core import recommendation_setting_outputs as outputs
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import saved_output_publication as publication, saved_outputs as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture
def disable_periodic_library_sync():
    pass


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
def test_saved_publication_input_keeps_admitted_history_and_private_guards(installed):
    assert_installed_consumer_input(*installed)


def prepare(conn,name):
    job = preparation.JOBS[name]
    job.install(conn)
    return finish(job,conn,size=128).prepared_revision


def publish(conn,name):
    source = preparation.SOURCES[name]
    revision = prepare(conn,name)
    publication.publish(conn,source,revision=revision)
    return revision


@pytest.mark.parametrize('installed',[79,94],indirect=True)
def test_preopened_and_reopened_readers_match_frozen_native_files(installed,tmp_path,monkeypatch):
    store,_ = installed
    sources,_ = seed(store)
    other = Store(store._db_path)
    native = Store(tmp_path/'reference.db')
    try:
        other.init_schema(defer_metadata=True)
        store.conn.backup(native.conn)
        runner.migrate(native.conn,registry=registry.MIGRATIONS[:95])
        history = list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        for name,source in preparation.SOURCES.items():
            revision = publish(store.conn,name)
            publication.publish(store.conn,source,revision=revision)
            assert publication.selected(other.conn,source) == revision
            assert other.settings.get_setting(source) == sources[source] == outputs.get(native.conn,source)
        reads = []
        real_read = other.settings.files.read
        def reading(reference, *, surface, chunk_key):
            assert not other.conn.in_transaction
            reads.append(surface)
            return real_read(reference, surface=surface, chunk_key=chunk_key)
        monkeypatch.setattr(other.settings.files, 'read', reading)
        assert {source:other.settings.get_setting(source) for source in sources} == sources
        assert set(reads) == set(sources)
        assert dict(store.conn.execute('SELECT key,value FROM main.settings')).items() >= sources.items()
        assert not outputs.available(other.conn)
        assert list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        regular.validate(other.conn)
    finally:
        other.conn.close()
        native.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert {source:reopened.settings.get_setting(source) for source in sources} == sources
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_compatible_writes_and_reference_retirement_withdraw_only_affected_owner(installed):
    store,_ = installed
    sources,run = seed(store)
    revisions = {name:publish(store.conn,name) for name in preparation.JOBS}
    old = head(store.conn,'radio_seed_offer')
    store.settings.set_setting('rec_autotune_result','new unreadable historical result')
    assert publication.selected(store.conn,'rec_autotune_result') is None
    assert publication.selected(store.conn,'radio_seed_offer') == revisions['radio-seed']
    assert store.settings.get_setting('rec_autotune_result') == 'new unreadable historical result'
    with store.conn:
        store.conn.execute('DELETE FROM rec_model_selection_runs WHERE id=?',(run,))
    assert publication.selected(store.conn,'radio_seed_offer') is None
    assert store.settings.get_setting('radio_seed_offer') == sources['radio_seed_offer']
    publish(store.conn,'radio-seed')
    assert head(store.conn,'radio_seed_offer')['run_resolution'] == 'missing_run'
    assert preparation.read_original(old) == sources['radio_seed_offer']
    for table in (preparation.tables('radio_seed_offer')[0],preparation.SUBJECTS,preparation.POLICIES):
        with pytest.raises(sqlite3.IntegrityError,match='retained'):
            with store.conn:
                store.conn.execute('DELETE FROM '+table)
    store.settings.set_values({'radio_seed_offer':'{"tracks":[]}','rec_autotune_result':'{"ran_at":15}'})
    assert all(publication.selected(store.conn,key) is None for key in sources)
    assert store.settings.get_setting('radio_seed_offer') == '{"tracks":[]}'
    store.settings.delete_setting('radio_seed_offer')
    publish(store.conn,'radio-seed')
    assert store.settings.get_setting('radio_seed_offer','missing') == 'missing'


@pytest.mark.parametrize('installed',[93],indirect=True)
@pytest.mark.parametrize('timing,mutation',[('file','source'),('file','subject'),('file','head'),
                                         ('verified','source'),('verified','subject')])
def test_verification_rejects_source_and_prepared_custody_races(installed,monkeypatch,timing,mutation):
    store,_ = installed
    seed(store)
    source = 'radio_seed_offer'
    revision = prepare(store.conn,'radio-seed')
    other = sqlite3.connect(store._db_path)
    def change():
        with other:
            if mutation == 'source':
                other.execute('UPDATE main.settings SET value=? WHERE key=?',('{"tracks":[]}',source))
            elif mutation == 'head':
                other.execute('DELETE FROM '+preparation.tables(source)[1])
            else:
                other.execute('DELETE FROM '+preparation.SUBJECTS+' WHERE position=1')
    try:
        if timing == 'file':
            real_read = preparation.read_original
            def reading(row):
                assert not store.conn.in_transaction
                change()
                return real_read(row)
            monkeypatch.setattr(preparation,'read_original',reading)
        else:
            real_verify = publication.verify
            def verified(conn,source,revision):
                result = real_verify(conn,source,revision)
                assert not conn.in_transaction
                change()
                return result
            monkeypatch.setattr(publication,'verify',verified)
        with pytest.raises(MigrationError,match='(changed|reconciled preparation)'):
            publication.publish(store.conn,source,revision=revision)
        assert publication.selected(store.conn,source) is None
        assert not store.conn.execute('SELECT 1 FROM '+publication.tables(source)[0]).fetchone()
        assert not store.conn.in_transaction
    finally:
        other.close()


@pytest.mark.parametrize('installed',[93],indirect=True)
@pytest.mark.parametrize('failure',['file','missing-file','metadata','subject','policy'])
def test_corrupt_custody_does_not_select_a_file_or_block_regular_ddl(installed,failure,independent_regular_ddl):
    store,_ = installed
    sources,_ = seed(store)
    source = 'radio_seed_offer'
    revision = prepare(store.conn,'radio-seed')
    row = head(store.conn,source)
    if failure == 'file':
        Path(row['filename']).write_bytes(b'corrupt')
    elif failure == 'missing-file':
        Path(row['filename']).unlink()
    else:
        with store.conn:
            if failure == 'metadata':
                store.conn.execute('UPDATE '+preparation.tables(source)[0]+" SET menu_id='wrong'")
            elif failure == 'subject':
                store.conn.execute('DELETE FROM '+preparation.SUBJECTS+' WHERE position=1')
            else:
                store.conn.execute('UPDATE '+preparation.POLICIES+" SET policy_version=NULL,resolution_reason='missing_policy'")
    with pytest.raises((ValueError,OSError,MigrationError)):
        publication.publish(store.conn,source,revision=revision)
    assert publication.selected(store.conn,source) is None
    assert store.settings.get_setting(source) == sources[source]
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_interruption_and_failure_after_generation_insert_leave_no_mixed_selection(installed):
    store,_ = installed
    sources,_ = seed(store)
    source = 'radio_seed_offer'
    first = publish(store.conn,'radio-seed')
    old = head(store.conn,source)
    store.settings.set_setting(source,'{"tracks":[]}')
    revision = prepare(store.conn,'radio-seed')
    def interrupt():
        raise KeyboardInterrupt('controlled saved output interruption')
    with pytest.raises(KeyboardInterrupt):
        publication.publish(store.conn,source,revision=revision,clock=interrupt)
    generations,selected,_ = publication.tables(source)
    store.conn.execute('CREATE TEMP TRIGGER abort_saved_output BEFORE INSERT ON main.'+selected+
        " BEGIN SELECT RAISE(ABORT,'controlled publication failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='controlled publication'):
        publication.publish(store.conn,source,revision=revision)
    assert publication.selected(store.conn,source) is None
    assert list(map(tuple,store.conn.execute('SELECT revision FROM '+generations))) == [(first,)]
    assert preparation.read_original(old) == sources[source]
    store.conn.execute('DROP TRIGGER temp.abort_saved_output')
    publication.publish(store.conn,source,revision=revision)
    assert store.settings.get_setting(source) == '{"tracks":[]}'


@pytest.mark.parametrize('installed',[93],indirect=True)
@pytest.mark.parametrize('raw',[None,'','historical non-JSON result','{"ran_at":true}','{"ran_at":1e999}','{"ran_at":0}'])
def test_original_report_semantics_include_null_blank_and_invalid_historical_json(installed,raw):
    store,_ = installed
    source = 'rec_autotune_result'
    publish(store.conn,'autotune-report')
    assert store.settings.get_setting(source,'missing') == 'missing'
    with store.conn:
        store.conn.execute('INSERT INTO main.settings VALUES (?,?)',(source,raw))
    publish(store.conn,'autotune-report')
    assert store.settings.get_setting(source,'missing') == raw
    assert head(store.conn,source)['observed_at'] == (0 if raw == '{"ran_at":0}' else None)


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_long_repeated_subjects_and_policies_publish_without_identity_admission(installed):
    store,_ = installed
    seed(store)
    source = 'radio_seed_offer'
    tracks = [{'video_id':str(index % 9),'pool_policy':'unknown-'+str(index)} for index in range(1000)]
    raw = json.dumps({'menu_id':'long','tracks':tracks})
    store.settings.set_setting(source,raw)
    before = store.conn.execute('SELECT count(*) FROM catalog_entries').fetchone()[0]
    revision = publish(store.conn,'radio-seed')
    assert store.settings.get_setting(source) == raw
    assert store.conn.execute('SELECT count(*) FROM '+preparation.SUBJECTS+' WHERE preparation_revision=?',(revision,)).fetchone()[0] == 1000
    assert store.conn.execute('SELECT count(*) FROM '+preparation.POLICIES+' WHERE preparation_revision=?',(revision,)).fetchone()[0] == 1000
    assert store.conn.execute('SELECT count(*) FROM catalog_entries').fetchone()[0] == before
    generations = publication.tables(source)[0]
    plan = ' '.join(str(tuple(row)) for row in store.conn.execute(
        'EXPLAIN QUERY PLAN SELECT 1 FROM '+generations+' WHERE generation=?',(revision,)))
    assert 'SEARCH' in plan and generations+'_custody' in plan
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting(source) == raw
        store.settings.set_setting(source,'{"tracks":[]}')
        assert not snapshot.publish(lambda:None)


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_explicit_missing_file_recovery_preserves_selected_custody_and_failure_state(installed,monkeypatch,independent_regular_ddl):
    store,_ = installed
    sources,_ = seed(store)
    source = 'radio_seed_offer'
    job = preparation.JOBS['radio-seed']
    revision = publish(store.conn,'radio-seed')
    retained = head(store.conn,source)
    path = Path(retained['filename'])
    path.unlink()
    with pytest.raises(FileNotFoundError):
        store.settings.get_setting(source)
    real_publish = preparation.files.publish
    def failed(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled recovery failure')
    monkeypatch.setattr(preparation.files,'publish',failed)
    with pytest.raises(OSError,match='recovery failure'):
        job.step(store.conn,retry=True)
    failed_progress = job.progress(store.conn)
    assert failed_progress.status == 'failed'
    regular.migrate(store.conn)
    assert job.progress(store.conn) == failed_progress
    monkeypatch.setattr(preparation.files,'publish',real_publish)
    job.step(store.conn,retry=True)
    assert job.progress(store.conn).status == 'prepared'
    assert publication.selected(store.conn,source) == revision
    assert store.settings.get_setting(source) == sources[source]
    assert store.conn.execute('SELECT count(*) FROM '+preparation.SUBJECTS).fetchone()[0] == 3
    assert preparation.read_original(retained) == sources[source]


@pytest.mark.parametrize('installed',[94],indirect=True)
def test_frozen_upgrade_keeps_published_custody_and_prefers_native_owner(installed,tmp_path):
    store,_ = installed
    sources,_ = seed(store)
    for name in preparation.JOBS:
        publish(store.conn,name)
    retained = {source:head(store.conn,source) for source in sources}
    # The explicit historical converter uses a dedicated connection, without
    # the serving repository's TEMP compatibility views or history ownership.
    upgraded = Store(tmp_path/'frozen-native.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn,registry=registry.MIGRATIONS)
        regular.validate(upgraded.conn)
        for source,raw in sources.items():
            assert publication.selected(upgraded.conn,source) is None
            assert outputs.get(upgraded.conn,source) == raw
            assert preparation.read_original(retained[source]) == raw
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()
