"""Independent folds, canonical aliases and compatible writers preserve taste decisions."""
import sqlite3

import pytest

from tests.store_fixture_support import guarded_numbered_store as installed
from tests.store_fixture_support import numbered_store as unadmitted_installed
from tests.store_fixture_support import preparation_ddl_connection, publication_consumer_reads
from tests.store_fixture_support import without_query_timing
from tests.core.test_taste_axis_preparation import seed, source, finish
from yt_playlist.core import taste_axes
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import taste_axes as preparation, taste_publication as publication
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Taste SQL/publication checks do not start an application lifespan."""


def ready(conn):
    preparation.JOB.install(conn)
    finish(conn)
    for _ in range(100):
        current = publication.step(conn,batch_size=2)
        if current.complete:
            return current.id
    pytest.fail('Taste publication did not stage')


def reconciled_seed(store):
    seed(store)
    with store.conn:
        store.conn.execute("DELETE FROM rec_weights WHERE axis='genre:known'")


def rows(conn,owner,columns,token='axis'):
    return list(map(tuple,conn.execute(taste_axes.projection(conn,taste_axes.table(conn,owner),columns,token_name=token)+' ORDER BY 1')))


def assert_selected_taste(store):
    assert store.rec_model.weight_records() == {'genre:known': {'axis': 'genre:known', 'weight': 1.4, 'updated_at': 11}}
    assert store.rec_model.get_leans() == {'genre:known': 1.3}
    assert store.rec_model.hidden_facets() == {'genre:known'}
    assert [tuple(row) for row in store.rec_signals.theme_rows()] == [('genre:known', 1.5, 11)]
    assert store.rec_model.get_lean('genre:Known alias') == 1.3
    assert store.rec_signals.get_theme('genre:known') == 1.5
    assert store.rec_signals.get_play_graduated_day('genre:known') == '2026-10-29'
    assert [row['id'] for row in store.rec_signals.recent_graduations()] == [2, 1]
    assert store.rec_signals.graduation_log_counts() == {'play': 1, 'rating': 1}
    assert [tuple(row) for row in store.rec_signals.graduation_audit_rows()] == [
        ('genre:known', 'play', 1.1), ('genre:known', 'rating', 0.9)]


@pytest.mark.parametrize('installed', [79, 87], indirect=True)
def test_selected_folds_match_native_and_preserve_exact_contributions(installed, tmp_path):
    store, _ = installed
    reconciled_seed(store)
    original = source(store.conn)
    history = list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))
    with store.read_snapshot() as snapshot:
        pinned = snapshot.store
        assert 'genre:Known alias' in pinned.rec_model.get_weights(now=11)
        identity = ready(store.conn)
        publication.publish(store.conn, identity=identity)
        assert publication.selected(pinned.conn) is None
        assert 'genre:Known alias' in pinned.rec_model.get_weights(now=11)
    with store.read_view() as reader:
        reader.conn.set_authorizer(lambda action, owner, column, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and owner in preparation.KEYS else sqlite3.SQLITE_OK)
        try:
            assert_selected_taste(reader)
        finally:
            reader.conn.set_authorizer(None)
    native = Store(tmp_path/'native-taste.db')
    try:
        native.conn.execute('PRAGMA synchronous=OFF')
        store.conn.backup(native.conn)
        runner.migrate(native.conn,registry=registry.MIGRATIONS[:88])
        assert publication.selected(native.conn) is None
        for owner,columns,token in (
            ('rec_weights','axis,weight,updated_at','axis'),
            ('rec_lean','axis,value,updated_at,last_graduated_day','axis'),
            ('home_hidden_facet','axis','axis'),('rec_theme','facet,score,updated_at','facet'),
            ('rec_play_grad','axis,last_graduated_day','axis'),
            ('rec_grad_log','id,axis,source,score,factor,new_weight,created_at','axis')):
            assert rows(store.conn,owner,columns,token)==rows(native.conn,owner,columns,token)
        regular.migrate(store.conn)
        assert source(store.conn) == original
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations'))) == history
        runner.migrate(native.conn,registry=registry.MIGRATIONS)
        regular.migrate(native.conn)
        native.init_schema(defer_metadata=True)
        assert native.rec_model.get_weights(now=11)==store.rec_model.get_weights(now=11)
        assert native.rec_model.hidden_facets()==store.rec_model.hidden_facets()
    finally:
        native.conn.close()


@pytest.mark.parametrize('unadmitted_installed',[87],indirect=True)
def test_competing_configured_values_block_selection_without_losing_history(unadmitted_installed, preparation_ddl_connection):
    store,_ = unadmitted_installed
    seed(store)
    original = source(store.conn)
    identity = ready(store.conn)
    with pytest.raises(MigrationError,match='Multiple configured'):
        publication.publish(store.conn,identity=identity)
    assert publication.selected(store.conn) is None
    assert source(store.conn)==original
    regular.migrate(preparation_ddl_connection)


@pytest.mark.parametrize('installed',[79,87],indirect=True)
def test_canonical_writes_update_current_physical_aliases_after_selection_and_withdrawal(installed):
    store,_ = installed
    reconciled_seed(store)
    with store.conn:
        store.conn.execute("UPDATE rec_lean SET axis='genre:Known alias' WHERE axis='genre:known'")
    first = ready(store.conn)
    publication.publish(store.conn,identity=first)
    store.rec_model.set_weight('genre:known',2.0,now=20)
    assert publication.selected(store.conn) is None
    assert list(map(tuple,store.conn.execute('SELECT axis,weight FROM rec_weights')))==[('genre:Known alias',2.0)]
    assert store.rec_model.weight_records()=={'genre:known':{'axis':'genre:known','weight':2.0,'updated_at':20}}
    assert store.rec_model.nudge_weight('genre:known',1.1,now=20)==pytest.approx(2.2)
    store.rec_model.set_lean('genre:known',1.7,21)
    assert list(map(tuple,store.conn.execute('SELECT axis,value FROM rec_lean')))==[('genre:Known alias',1.7)]
    assert store.rec_model.get_lean('genre:known')==1.7
    store.rec_model.set_lean_graduated_day('genre:known','2026-10-30')
    assert store.conn.execute('SELECT last_graduated_day FROM rec_lean').fetchone()[0]=='2026-10-30'
    assert store.rec_signals.bump_theme('genre:known',0.5,22)==2.0
    store.rec_signals.discount_theme('genre:known',0.25)
    assert store.rec_signals.get_theme('genre:Known alias')==1.75
    assert [tuple(row) for row in store.rec_signals.theme_rows()]==[('genre:known',1.75,22)]
    store.rec_model.hide_facet('genre:known')
    assert store.conn.execute('SELECT count(*) FROM home_hidden_facet').fetchone()[0]==2
    store.rec_model.unhide_facet('genre:known')
    assert not store.conn.execute('SELECT 1 FROM home_hidden_facet').fetchone()
    store.rec_signals.set_play_graduated_day('genre:known','2026-10-28')
    assert list(map(tuple,store.conn.execute('SELECT last_graduated_day FROM rec_play_grad')))==[
        ('2026-10-28',),('2026-10-28',)]
    assert store.rec_signals.get_play_graduated_day('genre:known')=='2026-10-28'
    store.rec_signals.log_graduation('genre:known','play',1,1.1,2.2,23)
    second = ready(store.conn)
    publication.publish(store.conn,identity=second)
    assert store.rec_model.weight_records()['genre:known']['weight']==pytest.approx(2.2)
    assert store.rec_model.get_leans()=={'genre:known':1.7}
    assert store.rec_signals.get_theme('genre:known')==1.75
    assert store.rec_signals.get_play_graduated_day('genre:known')=='2026-10-28'
    assert [row['id'] for row in store.rec_signals.recent_graduations()]==[3,2,1]
    store.rec_model.clear_lean('genre:known')
    assert store.rec_model.get_lean('genre:known')==1.0
    assert not store.conn.execute('SELECT 1 FROM rec_lean').fetchone()
    assert publication.selected(store.conn) is None
    assert store.rec_model.weight_records()['genre:known']['weight']==pytest.approx(2.2)
    assert publication.stage(store.conn,first).complete==1


@pytest.mark.parametrize('installed',[87],indirect=True)
def test_current_reference_and_unseen_source_aliases_are_used_for_writes(installed):
    store,_ = installed
    reconciled_seed(store)
    first = ready(store.conn)
    publication.publish(store.conn,identity=first)
    # These keys are absent from the retained map. Unicode normalization and
    # registered alias evidence still identify the physical current value.
    with store.conn:
        store.conn.execute("INSERT INTO genre_alias VALUES ('néu alias','Néu Alias','known','custom')")
        store.conn.execute("DELETE FROM rec_weights")
        store.conn.execute("INSERT INTO rec_weights VALUES ('genre:  NÉU ALIAS  ',1.6,30)")
    store.rec_model.set_weight('genre:known',2.1,now=31)
    assert list(map(tuple,store.conn.execute('SELECT axis,weight FROM rec_weights')))==[('genre:  NÉU ALIAS  ',2.1)]
    assert store.rec_model.weight_records()['genre:known']['weight']==2.1
    # A collision is an explicit configured-value conflict, not permission to
    # overwrite either original. A later source rename gets current resolution.
    with store.conn:
        store.conn.execute("INSERT INTO rec_weights VALUES ('genre:known',1.2,32)")
    with pytest.raises(ValueError,match='Multiple configured'):
        store.rec_model.set_weight('genre:known',1.5,now=33)
    assert store.conn.execute('SELECT count(*) FROM rec_weights').fetchone()[0]==2
    assert set(store.rec_model.weight_records())=={'genre:  NÉU ALIAS  ','genre:known'}
    with store.conn:
        store.conn.execute("DELETE FROM rec_weights WHERE axis='genre:known'")
        store.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('other-known','Other known','custom')")
        store.conn.execute("UPDATE genre_alias SET genre_key='other-known' WHERE label_key='néu alias'")
    store.rec_model.set_weight('genre:other-known',1.9,now=34)
    assert list(map(tuple,store.conn.execute('SELECT axis,weight FROM rec_weights')))==[('genre:  NÉU ALIAS  ',1.9)]


@pytest.mark.parametrize('unadmitted_installed',[87],indirect=True)
@pytest.mark.parametrize('damage',['value','axis','missing'])
def test_independent_source_and_identity_verification_rejects_damage(unadmitted_installed,preparation_ddl_connection,damage):
    store,_ = unadmitted_installed
    conn = preparation_ddl_connection
    reconciled_seed(store)
    preparation.JOB.install(conn)
    finish(conn)
    with conn:
        if damage=='value':
            conn.execute("UPDATE prepared_taste_rows SET weight=2.9 WHERE owner='rec_weights'")
        elif damage=='axis':
            conn.execute("UPDATE prepared_taste_axis_keys SET genre_key='wrong' WHERE kind='genre'")
        else:
            conn.execute("DELETE FROM prepared_taste_rows WHERE owner='rec_weights'")
    for _ in range(20):
        current = publication.step(conn)
        if current.complete:
            break
    with pytest.raises(MigrationError,match='differs|missing or extra'):
        publication.publish(conn,identity=current.id)
    assert publication.selected(conn) is None
    assert store.rec_model.weight_records()['genre:Known alias']['weight']==1.4
    regular.migrate(conn)


@pytest.mark.parametrize('unadmitted_installed',[87],indirect=True)
def test_interruption_stale_publication_and_failed_retry_leave_old_sources_usable(unadmitted_installed,monkeypatch,preparation_ddl_connection):
    store,_ = unadmitted_installed
    reconciled_seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    checkpoint = publication.step(store.conn,batch_size=1)
    with store.conn:
        store.conn.execute('CREATE TRIGGER interrupt_taste_stage BEFORE INSERT ON '+publication.table('rows')+
            " BEGIN SELECT RAISE(ABORT,'controlled staging interruption'); END")
    with pytest.raises(sqlite3.IntegrityError,match='controlled'):
        publication.step(store.conn,batch_size=1)
    assert publication.stage(store.conn)==checkpoint
    with store.conn:
        store.conn.execute('DROP TRIGGER interrupt_taste_stage')
    for _ in range(20):
        current = publication.step(store.conn)
        if current.complete:
            break
    def interrupted():
        raise KeyboardInterrupt('controlled selection interruption')
    with pytest.raises(KeyboardInterrupt):
        publication.publish(store.conn,identity=current.id,clock=interrupted)
    assert not store.conn.in_transaction
    assert publication.selected(store.conn) is None
    verify = publication.verify
    def changed(conn,identity):
        captured = verify(conn,identity)
        store.rec_model.set_weight('genre:Known alias',2.3,now=50)
        return captured
    with monkeypatch.context() as patch:
        patch.setattr(publication,'verify',changed)
        with pytest.raises(MigrationError,match='reconciled|changed'):
            publication.publish(store.conn,identity=current.id)
    assert publication.selected(store.conn) is None
    assert store.rec_model.weight_records()['genre:Known alias']['weight']==2.3
    regular.migrate(preparation_ddl_connection)
    first = ready(store.conn)
    publication.publish(store.conn,identity=first)
    with store.conn:
        store.conn.execute("UPDATE rec_lean SET axis='' WHERE axis='genre:known'")
    with pytest.raises(ValueError,match='taste axis'):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status=='failed'
    regular.migrate(preparation_ddl_connection)
    with store.conn:
        store.conn.execute("UPDATE rec_lean SET axis='genre:Known alias' WHERE axis=''")
    preparation.JOB.step(store.conn,retry=True)
    final = ready(store.conn)
    publication.publish(store.conn,identity=final)
    assert store.rec_model.get_leans()=={'genre:known':1.3}
    assert store.rec_model.weight_records()['genre:known']['weight']==2.3
    with pytest.raises(sqlite3.IntegrityError,match='cleanup'):
        with store.conn:
            store.conn.execute('DELETE FROM '+publication.table('rows')+' WHERE stage=?',(first,))


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_family_precedence_unknowns_and_new_empty_bootstrap(installed,tmp_path):
    store,_ = installed
    with store.conn:
        store.conn.execute("INSERT INTO genre_family VALUES ('shared','Shared')")
        store.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('shared','Shared genre','custom')")
        store.conn.executemany('INSERT INTO rec_weights VALUES (?,1.2,10)',[
            ('family:shared',),('family:Unknown saved',),('artist:Exact',)])
    identity = ready(store.conn)
    publication.publish(store.conn,identity=identity)
    assert set(store.rec_model.weight_records())=={'genre:shared','genre:Unknown saved','artist:Exact'}
    store.rec_model.set_weight('genre:shared',1.8,now=20)
    assert store.rec_model.weight_records()['genre:shared']['weight']==1.8
    assert store.conn.execute("SELECT axis FROM rec_weights WHERE weight=1.8").fetchone()[0]=='family:shared'
    assert 'genre:Unknown saved' in store.rec_model.weight_records()
    fresh = Store(tmp_path/'fresh-taste.db')
    try:
        fresh.init_schema(defer_metadata=True)
        fresh.rec_model.set_weight('genre:ambient',1.5,now=10)
        assert fresh.rec_model.weight_records()['genre:ambient']['weight']==1.5
        assert publication.selected(fresh.conn) is None
    finally:
        fresh.conn.close()


@pytest.mark.parametrize('unadmitted_installed',[87],indirect=True)
def test_distinct_family_and_genre_identity_cannot_hide_each_other_at_api_boundary(unadmitted_installed,preparation_ddl_connection):
    store,_ = unadmitted_installed
    with store.conn:
        store.conn.execute("INSERT INTO genre_family VALUES ('shared','Shared')")
        store.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('shared','Specific shared genre','custom')")
        store.conn.execute("INSERT INTO rec_weights VALUES ('family:shared',1.2,10)")
        store.conn.execute("INSERT INTO rec_theme VALUES ('family:shared',2,10)")
    first = ready(store.conn)
    publication.publish(store.conn,identity=first)
    with store.conn:
        store.conn.execute("INSERT INTO rec_weights VALUES ('genre:Specific shared genre',1.8,20)")
        store.conn.execute("INSERT INTO rec_theme VALUES ('genre:Specific shared genre',3,20)")
    assert publication.selected(store.conn) is None
    assert {key:value['weight'] for key,value in store.rec_model.weight_records().items()}=={
        'family:shared':1.2,'genre:Specific shared genre':1.8}
    assert {row['facet']:row['score'] for row in store.rec_signals.theme_rows()}=={
        'family:shared':2,'genre:Specific shared genre':3}
    assert store.rec_signals.get_theme('genre:shared')==2
    assert store.rec_signals.get_theme('genre:Specific shared genre')==3
    store.rec_model.set_weight('genre:Specific shared genre',2.1,now=30)
    assert list(map(tuple,store.conn.execute('SELECT axis,weight FROM rec_weights ORDER BY axis')))==[
        ('family:shared',1.2),('genre:Specific shared genre',2.1)]
    current = ready(store.conn)
    with pytest.raises(MigrationError,match='Distinct taste identities'):
        publication.publish(store.conn,identity=current)
    assert publication.selected(store.conn) is None
    regular.migrate(preparation_ddl_connection)
