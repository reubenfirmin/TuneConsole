"""Held choice identities and ordered demand survive selection and continuing writes."""
import json
import sqlite3

import pytest

from tests.store_fixture_support import numbered_store as installed, guarded_numbered_store as guarded_installed, publication_consumer_reads
from tests.store_fixture_support import native_sql_calls, preparation_ddl_connection, without_query_timing
from tests.core.test_demand_preparation import VALUES, seed, finish
from yt_playlist.core import recommendation_requests
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import recommendation_demand as preparation, demand_publication as publication
from yt_playlist.core.store import Store


EXPECTED = {**VALUES, 'choice_window:mixes':None}


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Publication checks do not construct an HTTP application."""


def ready(conn, size=1):
    preparation.JOB.install(conn)
    finish(conn)
    previous = 0
    for _ in range(350):
        stage = publication.step(conn, batch_size=size)
        assert 0<=stage.copied-previous<=size
        previous = stage.copied
        if stage.complete:
            return stage.id
    pytest.fail('Demand publication did not finish')


@pytest.mark.parametrize('installed', [86], indirect=True)
def test_selected_presence_reads_no_demand_document(installed, monkeypatch):
    store, _ = installed
    seed(store)
    publication.publish(store.conn, identity=ready(store.conn))
    def forbidden(*args, **kwargs):
        pytest.fail('Presence read a selected demand document')
    monkeypatch.setattr(publication, 'read', forbidden)
    for surface, value in EXPECTED.items():
        assert bool(store.rec_proposals.has_proposals(surface)) == bool(value)


@pytest.mark.parametrize('guarded_installed',[79,86],indirect=True)
def test_selected_requests_preserve_identity_order_sources_and_supported_upgrade(guarded_installed,tmp_path):
    store,_ = guarded_installed
    seed(store)
    originals = list(map(tuple,store.conn.execute('SELECT * FROM rec_proposals ORDER BY surface')))
    history = list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))
    native = Store(tmp_path/'native-demand.db')
    try:
        with store.read_snapshot() as snapshot:
            pinned = snapshot.store
            assert pinned.rec_proposals.get_proposals('choice_window:mixes')=={}
            identity = ready(store.conn)
            publication.publish(store.conn,identity=identity)
            assert publication.selected(pinned.conn) is None
            assert pinned.rec_proposals.get_proposals('choice_window:mixes')=={}
        with store.read_view() as reader:
            reader.conn.set_authorizer(lambda action,owner,column,*_:sqlite3.SQLITE_DENY
                if action==sqlite3.SQLITE_READ and owner=='rec_proposals' and column=='payload'
                else sqlite3.SQLITE_OK)
            for surface,value in EXPECTED.items():
                assert reader.rec_proposals.get_proposals(surface)==value
                assert reader.rec_proposals.has_proposals(surface)==bool(value)
        regular.migrate(store.conn)
        assert list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))==history
        assert list(map(tuple,store.conn.execute('SELECT * FROM rec_proposals ORDER BY surface')))==originals
        native.conn.execute('PRAGMA synchronous=OFF')
        store.conn.backup(native.conn)
        runner.migrate(native.conn,registry=registry.MIGRATIONS[:87])
        assert publication.selected(native.conn) is None
        for surface,value in EXPECTED.items():
            assert recommendation_requests.read(native.conn,surface)==value
        runner.migrate(native.conn,registry=registry.MIGRATIONS)
        regular.migrate(native.conn)
        native.init_schema(defer_metadata=True)
        for surface,value in EXPECTED.items():
            assert native.rec_proposals.get_proposals(surface)==value
        assert list(map(tuple,native.conn.execute('SELECT * FROM prepared_demand_originals ORDER BY surface')))==originals
    finally:
        native.conn.close()


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_compatible_writes_reference_edits_and_deletions_withdraw_without_stale_overwrite(installed):
    store,_ = installed
    seed(store)
    first = ready(store.conn)
    publication.publish(store.conn,identity=first)
    changed = {'opportunity_id':'continuing-paid-choice','window':'held until accepted'}
    store.rec_proposals.put_proposals('choice_window:albums',changed,200)
    assert publication.selected(store.conn) is None
    assert store.rec_proposals.get_proposals('choice_window:albums')==changed
    second = ready(store.conn)
    publication.publish(store.conn,identity=second)
    with store.conn:
        store.conn.execute("INSERT INTO genre_alias VALUES ('unknown captured','Unknown captured','known-genre','custom')")
    assert publication.selected(store.conn) is None
    third = ready(store.conn)
    publication.publish(store.conn,identity=third)
    assert store.conn.execute('SELECT genre_key FROM '+publication.table('prepared_home_genre_items')+
        " WHERE stage=? AND kind='include' AND position=1",(third,)).fetchone()[0]=='known-genre'
    with store.conn:
        store.conn.execute("DELETE FROM rec_proposals WHERE surface='album_menu_refresh'")
    assert publication.selected(store.conn) is None
    assert store.rec_proposals.get_proposals('album_menu_refresh') is None
    fourth = ready(store.conn)
    publication.publish(store.conn,identity=fourth)
    assert store.rec_proposals.get_proposals('album_menu_refresh') is None
    assert store.rec_proposals.get_proposals('choice_window:albums')==changed
    assert publication.read(store.conn,'album_menu_refresh',first)==VALUES['album_menu_refresh']
    assert publication.read(store.conn,'choice_window:albums',first)==VALUES['choice_window:albums']


@pytest.mark.parametrize('installed',[86],indirect=True)
@pytest.mark.parametrize('damage',['wrong','extra','missing'])
def test_independent_verification_rejects_damaged_prepared_members(installed,preparation_ddl_connection,damage):
    store,_ = installed
    conn = preparation_ddl_connection
    seed(store)
    preparation.JOB.install(conn)
    finish(conn)
    with conn:
        if damage=='wrong':
            conn.execute("UPDATE prepared_home_genre_items SET genre_key=NULL,resolution_reason='unknown_genre' WHERE kind='include' AND position=0")
        elif damage=='extra':
            conn.execute("INSERT INTO prepared_album_menu_exclusions VALUES (1,99,'extra',NULL,'missing_album')")
        else:
            conn.execute("DELETE FROM prepared_listening_choice_windows WHERE format='albums'")
    for _ in range(30):
        stage = publication.step(conn,batch_size=16)
        if stage.complete:
            break
    with pytest.raises(MigrationError,match='differs'):
        publication.publish(conn,identity=stage.id)
    assert publication.selected(conn) is None
    assert store.rec_proposals.get_proposals('choice_window:albums')==VALUES['choice_window:albums']
    regular.migrate(conn)


@pytest.mark.parametrize('installed',[86],indirect=True)
def test_interruption_resume_and_publication_fence_keep_atomic_selection(installed,monkeypatch,preparation_ddl_connection):
    store,_ = installed
    seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    first = publication.step(store.conn,batch_size=1)
    with store.conn:
        store.conn.execute('CREATE TRIGGER interrupt_demand_stage BEFORE INSERT ON '+publication.table('prepared_album_menu_exclusions')+
                           " BEGIN SELECT RAISE(ABORT,'controlled batch interruption'); END")
    with pytest.raises(sqlite3.IntegrityError,match='controlled'):
        publication.step(store.conn,batch_size=1)
    assert publication.stage(store.conn)==first
    with store.conn:
        store.conn.execute('DROP TRIGGER interrupt_demand_stage')
    for _ in range(30):
        stage = publication.step(store.conn,batch_size=16)
        if stage.complete:
            break
    def interrupted():
        raise KeyboardInterrupt('controlled publication interruption')
    with pytest.raises(KeyboardInterrupt):
        publication.publish(store.conn,identity=stage.id,clock=interrupted)
    assert not store.conn.in_transaction
    assert publication.selected(store.conn) is None
    verify = publication.verify
    def changed(conn,identity):
        result = verify(conn,identity)
        store.rec_proposals.put_proposals('home_genre_request',{},300)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(publication,'verify',changed)
        with pytest.raises(MigrationError,match='reconciled|changed'):
            publication.publish(store.conn,identity=stage.id)
    assert publication.selected(store.conn) is None
    assert store.rec_proposals.get_proposals('home_genre_request')=={}
    regular.migrate(preparation_ddl_connection)
    final = ready(store.conn)
    publication.publish(store.conn,identity=final)
    assert store.rec_proposals.get_proposals('home_genre_request')=={}
    with pytest.raises(sqlite3.IntegrityError,match='retained|cleanup'):
        with store.conn:
            store.conn.execute('DELETE FROM '+publication.table('prepared_demand_originals')+' WHERE stage=?',(final,))


@pytest.mark.parametrize('installed',[86],indirect=True)
@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_full_finite_demand_uses_bounded_staging_and_failed_preparation_leaves_regular_changes_usable(installed,preparation_ddl_connection):
    store,_ = installed
    seed(store)
    value = {'epoch':99,'include_genres':['known-genre']*100,'avoid_genres':['Missing']*100,'held_by_vibe':False}
    store.rec_proposals.put_proposals('home_genre_request',value,300)
    store.rec_proposals.put_proposals('album_menu_refresh',{'epoch':98,'exclude':['release']*100},300)
    identity = ready(store.conn,size=16)
    assert publication.stage(store.conn,identity).copied==309
    publication.publish(store.conn,identity=identity)
    assert store.rec_proposals.get_proposals('home_genre_request')==value
    with store.conn:
        store.conn.execute("UPDATE rec_proposals SET payload='{bad' WHERE surface='home_genre_request'")
    with pytest.raises(ValueError):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status=='failed'
    assert publication.selected(store.conn) is None
    regular.migrate(preparation_ddl_connection)
    store.rec_proposals.put_proposals('home_genre_request',value,301)
    preparation.JOB.step(store.conn,retry=True)
    final = ready(store.conn,size=16)
    publication.publish(store.conn,identity=final)
    assert store.rec_proposals.get_proposals('home_genre_request')==value


@pytest.mark.parametrize('installed',[79],indirect=True)
@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_empty_predecessor_selection_and_native_fresh_bootstrap(installed,tmp_path):
    store,_ = installed
    identity = ready(store.conn)
    publication.publish(store.conn,identity=identity)
    assert publication.stage(store.conn,identity).copied==0
    assert all(store.rec_proposals.get_proposals(surface) is None for surface in VALUES)
    current = Store(tmp_path/'fresh.db')
    try:
        current.conn.execute('PRAGMA synchronous=OFF')
        current.init_schema(defer_metadata=True)
        assert publication.selected(current.conn) is None
        for surface,value in VALUES.items():
            current.rec_proposals.put_proposals(surface,value,100)
            assert current.rec_proposals.get_proposals(surface)==EXPECTED[surface]
    finally:
        current.conn.close()
