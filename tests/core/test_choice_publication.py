"""Independent acceptance of selected learning inputs and exact retention roots."""
import json
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_choice_learning_preparation import seed,finish,head,originals
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import group_evidence,choice_learning_state
from yt_playlist.core.migrations import regular,registry,runner,MigrationError
from yt_playlist.core.preparation import choice_learning as preparation,choice_publication as publication
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/file owners start no web application or periodic worker."""


@pytest.fixture
def choice_installed(request,tmp_path,monkeypatch):
    bound_catalog(monkeypatch)
    version=request.param
    store=Store(tmp_path/'choice.db')
    try:
        store.conn.execute('PRAGMA synchronous=OFF')
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        with closing(sqlite3.connect(':memory:')) as source:
            install_numbered_input(source,version,regular_owners=regular.CHANGES)
            source.backup(store.conn)
        store.init_schema(defer_metadata=True)
        yield store,version
    finally:
        store.conn.close()


def seed_choice_input(conn,*,native=False):
    """Completed catalog inputs, without provider/credit ingestion producers."""
    documents=originals()
    with conn:
        conn.execute("INSERT INTO recordings(id,identity_key,title,artist) VALUES (1,'song|artist','Song','Artist')")
        conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,artist,album,duration_s) "
                     "VALUES ('video',1,'Song','Artist','Album',100)")
        conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,1,'video')")
        conn.execute("INSERT INTO library_memberships VALUES (1,'fixture',NULL)")
        conn.execute("INSERT INTO albums(id,title,artist,metadata_rank) VALUES (1,'Album','Artist',0)")
        conn.execute("INSERT INTO album_provider_ids(provider,provider_id,album_id,is_primary) VALUES ('youtube','release',1,1)")
        for surface,raw in documents.items():
            if native:
                choice_learning_state.write(conn,surface,choice_learning_state.decode(surface,raw),None,historical=True)
            else:
                conn.execute('INSERT INTO rec_proposals VALUES (?,?,NULL)',(surface,raw))
    return documents


@pytest.fixture
def choice_upgrade_input(request,tmp_path,monkeypatch):
    bound_catalog(monkeypatch)
    version,target=request.param
    path=tmp_path/'choice-upgrade.db'
    with closing(sqlite3.connect(':memory:')) as schema, closing(sqlite3.connect(path)) as conn:
        # Regular admission/order is owned by choice_installed. These cases
        # retain the numbered conversion and independent publication contracts.
        install_numbered_input(schema,version)
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA wal_autocheckpoint=0')
        schema.backup(conn)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        original=seed_choice_input(conn,native=version>=93)
        if version<93:
            preparation.JOB.install(conn)
            finish(conn)
            publish(conn)
        yield conn,path,version,target,original


@contextmanager
def choice_reader(path,version):
    store=Store(path)
    try:
        store._schema_compatibility=version<103
        store._rebuild_source_version=None
        store._schema_recovery_pending=False
        store._bind_repositories()
        yield store
    finally:
        store.conn.close()


def publish(conn,size=16):
    for _ in range(300):
        stage = publication.step(conn,batch_size=size)
        if stage['complete']:
            publication.OWNER.publish(conn,stage=stage['id'])
            return stage['id']
    pytest.fail('Learning publication did not finish')


@pytest.mark.parametrize('choice_installed',[79,92],indirect=True)
def test_exact_selected_values_do_not_read_originals(choice_installed):
    store,version = choice_installed
    conn = store.conn
    original = seed_choice_input(conn)
    history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    preparation.JOB.install(conn)
    finish(conn)
    publish(conn)
    denied = {'rec_proposals','prepared_choice_learning_original_parts'}
    def authorize(action,owner,column,*rest):
        if action==sqlite3.SQLITE_READ and owner in denied and (owner!='rec_proposals' or column=='payload'):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    with store.read_view() as selected:
        selected.conn.set_authorizer(authorize)
        try:
            for surface,raw in original.items():
                assert selected.rec_proposals.get_proposals(surface)==json.loads(raw)
                assert selected.rec_proposals.has_proposals(surface)
        finally:
            selected.conn.set_authorizer(None)
    assert list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))==history
    assert dict(conn.execute('SELECT surface,payload FROM rec_proposals WHERE surface LIKE \'%choice%\''))==original
    regular.migrate(conn)
    regular.validate(conn)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('choice_upgrade_input',[
    (79,92),(92,93),(93,98),(98,102),(103,104),(104,107)
],indirect=True,ids=['79-to-92','92-to-93','93-to-98','98-to-102','103-to-104','104-to-107'])
def test_registered_choice_upgrade_preserves_exact_selected_values(choice_upgrade_input):
    conn,path,version,target,original=choice_upgrade_input
    history=list(map(tuple,conn.execute('SELECT version,name FROM schema_migrations ORDER BY version')))
    runner.migrate(conn,registry=registry.MIGRATIONS[:target])
    assert list(map(tuple,conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        *history,*[(item.version,item.name) for item in registry.MIGRATIONS[version:target]]]
    with choice_reader(path,target) as upgraded:
        if target<93:
            assert publication.selected(upgraded.conn) is not None
        else:
            assert publication.selected(upgraded.conn) is None
        for surface,raw in original.items():
            assert upgraded.rec_proposals.get_proposals(surface)==json.loads(raw)
        regular.validate(upgraded.conn)
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('choice_upgrade_input',[(103,103)],indirect=True,ids=['renamed-input'])
def test_name_cutover_restores_choice_readers_and_immutable_inputs(choice_upgrade_input):
    from yt_playlist.core.migrations import v103_entity_names

    conn,path,_version,target,original=choice_upgrade_input
    programs=list(conn.execute("SELECT type,name,sql FROM sqlite_schema WHERE type IN ('view','trigger') ORDER BY rowid"))
    with conn:
        conn.execute('BEGIN')
        for kind,name,_sql in reversed(programs):
            conn.execute('DROP '+kind+' "'+name.replace('"','""')+'"')
        v103_entity_names.restore_programs(conn,programs,retention_progress=False)
    runner.validate_version(conn,103,foreign_keys=False,data=False)
    with choice_reader(path,target) as reader:
        for surface,raw in original.items():
            assert reader.rec_proposals.get_proposals(surface)==json.loads(raw)
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        with conn:
            conn.execute('UPDATE choice_interest_features SET value=.99')
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed',[92],indirect=True)
def test_continuing_source_and_reference_writes_withdraw_selection(installed):
    store,_ = installed
    seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    finish(conn)
    old = publish(conn)
    store.rec_proposals.put_proposals('choice_interests',{'contexts':[{'keys':['new|artist'],'feature':None}]},20)
    assert publication.selected(conn) is None
    assert store.rec_proposals.get_proposals('choice_interests')=={'contexts':[{'keys':['new|artist'],'feature':None}]}
    with conn:
        conn.execute("INSERT INTO recordings(identity_key) VALUES ('new|artist')")
    finish(conn)
    new = publish(conn)
    assert new!=old
    assert conn.execute('SELECT recording_id IS NOT NULL FROM prepared_choice_interest_tracks WHERE publication_id=?',
        (head(conn)['id'],)).fetchone()[0]
    with conn:
        conn.execute("DELETE FROM rec_proposals WHERE surface='choice_interests'")
    assert publication.selected(conn) is None
    finish(conn)
    publish(conn)
    assert store.rec_proposals.get_proposals('choice_interests') is None
    assert not store.rec_proposals.has_proposals('choice_interests')


@pytest.mark.parametrize('installed',[92],indirect=True)
def test_exact_group_roots_are_bounded_retained_and_unknowns_explicit(installed):
    from tests.group_support import publish as groups_publish,recordings
    store,_ = installed
    conn = store.conn
    groups = groups_publish(store,recordings([('Jazz',16,.2)]),now=10)
    group = groups['groups'][0]
    rows = [{'group_id':group['group_id'] if index==0 else 'unknown-group-'+str(index),
        'version':group['version'] if index==0 else 'unknown-version-'+str(index),'shared_from':'unknown-'+str(index),'menus':[]}
        for index in range(25)]
    value = {'generation':groups['generation'],'rows':rows}
    store.rec_proposals.put_proposals('group_choice_bandit',value,11)
    preparation.JOB.install(conn)
    finish(conn)
    prior = 0
    for _ in range(60):
        stage = publication.step(conn,batch_size=3)
        roots = conn.execute('SELECT count(*) FROM '+publication.ROOTS).fetchone()[0]
        assert 0<=roots-prior<=3
        prior = roots
        if stage['complete']:
            break
    else:
        pytest.fail('Bounded group custody did not finish')
    assert prior==51
    publication.OWNER.publish(conn,stage=stage['id'])
    assert store.rec_proposals.get_proposals('group_choice_bandit')==value
    assert conn.execute('SELECT count(*) FROM unresolved_group_evidence WHERE external_key LIKE \'choice-preparation:%\'').fetchone()[0]==49
    root = conn.execute('SELECT kind,external_key FROM '+publication.ROOTS+' ORDER BY position LIMIT 1').fetchone()
    with pytest.raises(sqlite3.IntegrityError,match='retained'):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            group_evidence.release(conn,*root)
    with pytest.raises(sqlite3.IntegrityError,match='retained'):
        with conn:
            conn.execute('UPDATE prepared_choice_group_estimates SET estimate=.9')
    with pytest.raises(sqlite3.IntegrityError,match='retained'):
        with conn:
            conn.execute('DELETE FROM '+publication.ROOTS)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed',[92],indirect=True)
@pytest.mark.parametrize('damage',[
    "UPDATE prepared_choice_interest_tracks SET recording_id=NULL,resolution_reason='missing_recording' WHERE position=0",
    'UPDATE prepared_choice_interest_features SET value=.99 WHERE dimension=0',
    'UPDATE prepared_choice_interest_contexts SET field_mask=0 WHERE position=0',
    "UPDATE prepared_choice_learning_references SET raw_key='invented' WHERE field='version'",
    'DELETE FROM prepared_choice_learning_heads WHERE surface=\'choice_bandit\'',
])
def test_incorrect_preparation_cannot_be_published(installed,damage):
    store,_ = installed
    seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    finish(conn)
    with conn:
        conn.execute(damage)
    with pytest.raises(MigrationError,match='differs|differ|omits'):
        publish(conn)
    assert publication.selected(conn) is None


@pytest.mark.parametrize('installed',[92],indirect=True)
def test_batch_and_selection_interruption_stale_proof_and_file_repair(installed,monkeypatch):
    store,_ = installed
    original = seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    finish(conn)
    stage = publication.step(conn,batch_size=1)
    assert publication.selected(conn) is None
    owner = publication.OWNER
    verify = type(owner).verify
    def stale(self,connection,identity):
        result = verify(self,connection,identity)
        with connection:
            connection.execute("UPDATE rec_proposals SET built_at=20 WHERE surface='choice_bandit'")
        return result
    while not stage['complete']:
        stage = publication.step(conn,batch_size=1)
    monkeypatch.setattr(type(owner),'verify',stale)
    with pytest.raises(MigrationError,match='reconciled|changed'):
        owner.publish(conn,stage=stage['id'])
    monkeypatch.setattr(type(owner),'verify',verify)
    finish(conn)
    # Delete one immutable original file; serving remains on the installed source.
    filename = conn.execute('SELECT filename FROM '+preparation.PARTS+' WHERE publication_id=? LIMIT 1',
        (head(conn)['id'],)).fetchone()[0]
    Path(filename).unlink()
    with pytest.raises(OSError):
        publish(conn)
    assert store.rec_proposals.get_proposals('choice_interests')==json.loads(original['choice_interests'])
    publication.repair(conn,'choice_interests')
    identity = publish(conn)
    # Abort after the final head statement; the old selected generation survives.
    conn.execute('CREATE TEMP TRIGGER abort_learning_head AFTER UPDATE ON main.'+owner.table('head')+
        " BEGIN SELECT RAISE(ABORT,'controlled publication interruption'); END")
    with pytest.raises(sqlite3.IntegrityError,match='controlled'):
        owner.publish(conn,stage=identity)
    assert publication.selected(conn)==identity


@pytest.mark.parametrize('installed',[92],indirect=True)
def test_group_retention_interruption_keeps_committed_prefix(installed,monkeypatch):
    store,_ = installed
    seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    finish(conn)
    first = publication.step(conn,batch_size=1)
    while not conn.execute('SELECT 1 FROM '+publication.ROOTS+' LIMIT 1').fetchone():
        first = publication.step(conn,batch_size=1)
    before = list(map(tuple,conn.execute('SELECT * FROM '+publication.ROOTS)))
    progress = list(map(tuple,conn.execute('SELECT * FROM '+publication.ROOT_PROGRESS)))
    retain = group_evidence.retain
    def interrupted(*args,**kwargs):
        retain(*args,**kwargs)
        raise KeyboardInterrupt('controlled group retention interruption')
    monkeypatch.setattr(group_evidence,'retain',interrupted)
    with pytest.raises(KeyboardInterrupt,match='controlled'):
        publication.step(conn,batch_size=1)
    assert list(map(tuple,conn.execute('SELECT * FROM '+publication.ROOTS)))==before
    assert list(map(tuple,conn.execute('SELECT * FROM '+publication.ROOT_PROGRESS)))==progress
    monkeypatch.setattr(group_evidence,'retain',retain)
    assert publish(conn)==first['id']


def test_fresh_native_bootstrap_does_not_select_preparation(tmp_path):
    store = Store(tmp_path/'fresh.db')
    try:
        store.init_schema()
        assert publication.selected(store.conn) is None
        store.rec_proposals.put_proposals('choice_bandit',{'rows':[]},10)
        assert store.rec_proposals.get_proposals('choice_bandit')=={'rows':[]}
    finally:
        store.conn.close()
