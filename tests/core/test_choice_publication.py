"""Independent acceptance of selected learning inputs and exact retention roots."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_choice_learning_preparation import seed,finish,head
from yt_playlist.core import group_evidence
from yt_playlist.core.migrations import regular,registry,runner,MigrationError
from yt_playlist.core.preparation import choice_learning as preparation,choice_publication as publication
from yt_playlist.core.store import Store


def publish(conn,size=16):
    for _ in range(300):
        stage = publication.step(conn,batch_size=size)
        if stage['complete']:
            publication.OWNER.publish(conn,stage=stage['id'])
            return stage['id']
    pytest.fail('Learning publication did not finish')


@pytest.mark.parametrize('installed',[79,92],indirect=True)
def test_exact_selected_values_no_original_reads_and_genuine_upgrade(installed,tmp_path):
    store,version = installed
    conn = store.conn
    original = seed(store)
    history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    preparation.JOB.install(conn)
    finish(conn)
    publish(conn,size=1)
    denied = {'rec_proposals','prepared_choice_learning_original_parts'}
    def authorize(action,owner,column,*rest):
        if action==sqlite3.SQLITE_READ and owner in denied and (owner!='rec_proposals' or column=='payload'):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    try:
        for surface,raw in original.items():
            assert store.rec_proposals.get_proposals(surface)==json.loads(raw)
            assert store.rec_proposals.has_proposals(surface)
    finally:
        conn.set_authorizer(None)
    assert list(map(tuple,conn.execute('SELECT * FROM schema_migrations ORDER BY version')))==history
    assert dict(conn.execute('SELECT surface,payload FROM rec_proposals WHERE surface LIKE \'%choice%\''))==original
    regular.migrate(conn)
    upgraded = Store(tmp_path/'upgraded.db')
    try:
        conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn,registry=registry.MIGRATIONS[:107])
        upgraded._bind_repositories()
        assert publication.selected(upgraded.conn) is None
        for surface,raw in original.items():
            assert upgraded.rec_proposals.get_proposals(surface)==json.loads(raw)
        regular.validate(upgraded.conn)
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()


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
    regular.migrate(conn)


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
