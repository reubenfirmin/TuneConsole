"""Ordered membership, legacy writers and explicit upgrades keep the same decisions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.store_fixture_support import numbered_store as installed, guarded_numbered_store as guarded_installed, publication_consumer_reads
from tests.core.test_recipe_membership_preparation import seed, finish, head
from yt_playlist.core import recipe_memberships
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import recipe_memberships as preparation, recipe_publication as publication
from yt_playlist.core.store import Store


LISTS = {'artists':['Known artist','Unadmitted artist','Unadmitted artist'],
         'genres':['Known','Unadmitted genre'], 'blacklist_genres':['Known']}


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Publication checks do not construct an HTTP application."""


def ready(conn):
    preparation.JOB.install(conn)
    finish(conn)
    for _ in range(20):
        stage = publication.OWNER.step(conn, batch_size=1)
        if stage.complete:
            return stage.id
    pytest.fail('Recipe publication did not stage')


@pytest.mark.parametrize('guarded_installed', [79,88], indirect=True)
def test_selected_membership_matches_native_and_keeps_sources_and_pinned_reads(guarded_installed,tmp_path):
    store, _ = guarded_installed
    original = seed(store,LISTS)
    with store.conn:
        store.conn.execute("UPDATE genre_catalog SET name='Canonical known' WHERE key='known'")
        store.conn.execute("INSERT INTO genre_alias VALUES ('known','Known','known','custom')")
    history = list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))
    upgraded = Store(tmp_path/'native-recipe.db')
    try:
        with store.read_snapshot() as snapshot:
            pinned = snapshot.store
            assert pinned.road_trip.get_road_trip_recipe(1)['genres']==LISTS['genres']
            stage=ready(store.conn)
            publication.OWNER.publish(store.conn,stage=stage)
            assert publication.OWNER.selected(pinned.conn) is None
            assert pinned.road_trip.get_road_trip_recipe(1)['genres']==LISTS['genres']
        with store.read_view() as reader:
            reader.conn.set_authorizer(lambda action,owner,column,*_:sqlite3.SQLITE_DENY
                if action==sqlite3.SQLITE_READ and owner=='road_trip_recipes' and column in preparation.FIELDS
                else sqlite3.SQLITE_OK)
            selected=reader.road_trip.get_road_trip_recipe(1)
            assert selected['genres']==['Canonical known','Unadmitted genre']
            assert selected['artists']==LISTS['artists']
            assert reader.road_trip.list_road_trip_recipes()==[selected]
        regular.migrate(store.conn)
        assert list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))==history
        assert preparation.read_original(head(store.conn))==original
        assert dict(zip(preparation.FIELDS,store.conn.execute('SELECT artists,genres,blacklist_genres FROM road_trip_recipes WHERE id=1').fetchone()))==original
        upgraded.conn.execute('PRAGMA synchronous=OFF')
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn,registry=registry.MIGRATIONS[:89])
        assert recipe_memberships.read(upgraded.conn,1)=={field:selected[field] for field in preparation.FIELDS}
        assert publication.OWNER.selected(upgraded.conn) is None
        runner.migrate(upgraded.conn,registry=registry.MIGRATIONS)
        regular.migrate(upgraded.conn)
        upgraded.init_schema(defer_metadata=True)
        assert upgraded.road_trip.get_road_trip_recipe(1)==selected
        assert preparation.read_original(head(upgraded.conn))==original
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed',[88],indirect=True)
def test_duplicate_resolved_identity_stops_selection_without_losing_ordered_intent(installed):
    store,_=installed
    original=seed(store)
    stage=ready(store.conn)
    with pytest.raises(MigrationError,match='same identity twice'):
        publication.OWNER.publish(store.conn,stage=stage)
    assert publication.OWNER.selected(store.conn) is None
    assert store.road_trip.get_road_trip_recipe(1)['artists']==json.loads(original['artists'])
    regular.migrate(store.conn)
    assert preparation.read_original(head(store.conn))==original


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_compatible_replacement_reference_edit_and_delete_withdraw_and_reconcile(installed):
    store,_=installed
    seed(store,LISTS)
    first=ready(store.conn)
    publication.OWNER.publish(store.conn,stage=first)
    original=head(store.conn)
    store.road_trip.save_road_trip_recipe(1,'Changed',40,['Unadmitted artist'],['Known'],75,20,blacklist_genres=[])
    assert publication.OWNER.selected(store.conn) is None
    assert store.road_trip.get_road_trip_recipe(1)['artists']==['Unadmitted artist']
    second=ready(store.conn)
    publication.OWNER.publish(store.conn,stage=second)
    with store.conn:
        store.conn.execute("INSERT INTO artist_aliases VALUES (111,'Unadmitted artist','fixture')")
    assert publication.OWNER.selected(store.conn) is None
    third=ready(store.conn)
    publication.OWNER.publish(store.conn,stage=third)
    assert store.road_trip.get_road_trip_recipe(1)['artists']==['Known artist']
    store.road_trip.delete_road_trip_recipe(1)
    assert publication.OWNER.selected(store.conn) is None
    fourth=ready(store.conn)
    publication.OWNER.publish(store.conn,stage=fourth)
    assert store.road_trip.list_road_trip_recipes()==[]
    assert len({first,second,third,fourth})==4
    assert preparation.read_original(original)['artists']==json.dumps(LISTS['artists'],indent=2)


@pytest.mark.parametrize('installed',[88],indirect=True)
@pytest.mark.parametrize('damage',['wrong','missing','extra'])
def test_independent_verification_rejects_bad_prepared_members(installed,damage):
    store,_=installed
    seed(store,LISTS)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    with store.conn:
        if damage=='wrong':
            store.conn.execute("UPDATE prepared_recipe_members SET raw_label='Different' WHERE role='include' AND position=0")
        elif damage=='missing':
            store.conn.execute("DELETE FROM prepared_recipe_members WHERE role='include' AND position=0")
        else:
            store.conn.execute("INSERT INTO prepared_recipe_members SELECT recipe_id,generation,role,99,'Extra',NULL,NULL,'unknown_genre',NULL FROM prepared_recipe_members WHERE role='include' AND position=0")
    stage=publication.OWNER.step(store.conn).id
    with pytest.raises(MigrationError,match='differs|extra'):
        publication.OWNER.publish(store.conn,stage=stage)
    assert publication.OWNER.selected(store.conn) is None
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_missing_original_repair_and_failed_preparation_do_not_block_regular_schema(installed):
    store,_=installed
    seed(store,LISTS)
    stage=ready(store.conn)
    captured=head(store.conn)
    Path(captured['filename']).unlink()
    regular.migrate(store.conn)
    with pytest.raises(FileNotFoundError):
        publication.OWNER.publish(store.conn,stage=stage)
    publication.repair(store.conn,1)
    publication.OWNER.publish(store.conn,stage=stage)
    with store.conn:
        store.conn.execute("UPDATE road_trip_recipes SET genres='null' WHERE id=1")
    with pytest.raises(MigrationError,match='lists of nonblank'):
        preparation.JOB.step(store.conn)
    failed=preparation.JOB.progress(store.conn)
    regular.migrate(store.conn)
    assert preparation.JOB.progress(store.conn)==failed and failed.status=='failed'
    assert publication.OWNER.selected(store.conn) is None


@pytest.mark.parametrize('installed',[88],indirect=True)
def test_slow_file_check_allows_compatible_write_and_fences_stale_selection(installed,monkeypatch):
    store,_=installed
    seed(store,LISTS)
    stage=ready(store.conn)
    entered,release=threading.Event(),threading.Event()
    read=preparation.read_original
    def slow(*args):
        result=read(*args)
        entered.set()
        assert release.wait(15)
        return result
    monkeypatch.setattr(preparation,'read_original',slow)
    def publish():
        with sqlite3.connect(store._db_path,timeout=.1) as conn:
            conn.row_factory=sqlite3.Row
            publication.OWNER.publish(conn,stage=stage)
    with ThreadPoolExecutor() as pool:
        task=pool.submit(publish)
        try:
            assert entered.wait(15)
            store.road_trip.save_road_trip_recipe(1,'Concurrent',50,[],[],90,30)
            regular.migrate(store.conn)
        finally:
            release.set()
        with pytest.raises(MigrationError,match='differs|changed|reconciled'):
            task.result(timeout=15)
    assert publication.OWNER.selected(store.conn) is None
    assert store.road_trip.get_road_trip_recipe(1)['name']=='Concurrent'
