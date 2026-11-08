"""Ordered membership, legacy writers and explicit upgrades keep the same decisions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from tests.core.test_predecessor_writes import admitted_installed as installed
from tests.core.test_recipe_membership_preparation import seed, finish, head
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.core import genre_identity, recipe_memberships, schema_admission, schema_definition
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.migrations.runner import Context
from yt_playlist.core.migrations import v103_entity_names
from yt_playlist.core.policy_documents import _publish_encoded
from yt_playlist.core.preparation import recipe_memberships as preparation, recipe_publication as publication
from yt_playlist.core.store import Store


LISTS = {'artists':['Known artist','Unadmitted artist','Unadmitted artist'],
         'genres':['Known','Unadmitted genre'], 'blacklist_genres':['Known']}
SELECTED = {**LISTS, 'genres':['Canonical known','Unadmitted genre'],
            'blacklist_genres':['Canonical known'], 'id':1, 'name':'Saved trip',
            'own_pct':50, 'familiarity_pct':50, 'target_minutes':90,
            'last_playlist_id':None, 'created_at':10, 'updated_at':10}


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/file owners never start the web lifespan or sync worker."""


@pytest.fixture
def recipe_input(request, tmp_path, monkeypatch):
    """Bounded installed inputs with real admission and snapshot guards."""
    bound_catalog(monkeypatch)
    version = request.param
    store = Store(tmp_path / 'recipe-input.db')
    try:
        store.conn.execute('PRAGMA synchronous=OFF')
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        with closing(sqlite3.connect(':memory:')) as source:
            install_recipe_input(source, version)
            source.backup(store.conn)
        store.init_schema(defer_metadata=True)
        yield store, version
    finally:
        store.conn.close()


def install_recipe_input(conn, version, *, regular_owners=True):
    """Construct completed lower-layer SQL inputs, without historical replay."""
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('BEGIN')
    statements = (schema_admission.reference()['statements'] if version == 79
                  else schema_definition.statements(version=version))
    for sql in statements:
        conn.execute(sql)
    conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                     [(item.version, item.name) for item in registry.MIGRATIONS[:version]])
    conn.executemany('INSERT INTO settings VALUES (?,?)', schema_definition.SEED_SETTINGS.items())
    for sql in schema_definition.INITIAL_STATE:
        conn.execute(sql)
    genre_identity.seed_catalog(conn)
    # Regular migration ordering has dedicated owners. Supply their completed
    # real guards here, rather than readmitting the schema for all 35 changes.
    if regular_owners:
        conn.execute(regular.HISTORY_SQL)
        for change in regular.CHANGES:
            change.apply(conn, Context(1.))
            conn.execute('INSERT INTO schema_changes VALUES (?,?,1)', (change.version, change.name))
        regular.validate(conn)
    conn.commit()


def canonicalize_known(conn):
    with conn:
        conn.execute("UPDATE genre_catalog SET name='Canonical known' WHERE key='known'")
        conn.execute("INSERT INTO genre_alias VALUES ('known','Known','known','custom')")


def seed_native_input(conn):
    """Completed v89 membership and retained v88 publication are upgrade input.

    Publication and the v89 conversion have their own real producer cases below.
    Later edges receive their exact persisted contract, including a private real
    content-addressed file and the genuine immutable map/custody guards.
    """
    original = {field: json.dumps(LISTS[field], indent=2) for field in preparation.FIELDS}
    with conn:
        conn.execute("INSERT INTO artists(id,name,provisional) VALUES (111,'Known artist',0)")
        conn.execute("INSERT INTO artist_aliases VALUES (111,'Known artist','fixture')")
        conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('known','Known','custom')")
        conn.execute("INSERT INTO road_trip_recipes(id,name,own_pct,target_minutes,created_at,updated_at) "
                     "VALUES (1,'Saved trip',50,90,10,10)")
        recipe_memberships.replace(conn, 1, *(LISTS[field] for field in preparation.FIELDS), legacy=True)
    canonicalize_known(conn)
    encoded = json.dumps(original, ensure_ascii=False, separators=(',', ':')).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    filename = _publish_encoded(preparation.directory(conn), encoded, digest)
    definitions = [*preparation.DEFINITION.definitions(conn), *publication.OWNER.definitions()]
    with conn:
        conn.execute('BEGIN')
        for _, sql in definitions:
            if not sql.startswith('CREATE TRIGGER'):
                conn.execute(sql)
        conn.execute('INSERT INTO '+preparation.DEFINITION.state+
            " VALUES (1,88,'prepared',NULL,NULL,6,0,0,1,NULL)")
        conn.execute('INSERT INTO '+preparation.DEFINITION.sources+" VALUES ('road_trip_recipes','[1]')")
        conn.execute('INSERT INTO '+preparation.HEAD+' VALUES (1,0,?,?,?,6)', (filename, digest, len(encoded)))
        conn.execute('INSERT INTO prepared_recipe_heads VALUES (1,0)')
        conn.executemany('INSERT INTO '+preparation.ITEMS+' VALUES (1,0,?,?,?,?,?,?,NULL)', [
            ('artist',0,'Known artist',111,None,None),
            ('artist',1,'Unadmitted artist',None,None,'unknown_or_ambiguous_artist'),
            ('artist',2,'Unadmitted artist',None,None,'unknown_or_ambiguous_artist'),
            ('include',0,'Known',None,'known',None),
            ('include',1,'Unadmitted genre',None,None,'unknown_genre'),
            ('exclude',0,'Known',None,'known',None)])
        owner = publication.OWNER
        conn.execute('INSERT INTO '+owner.table('epoch')+' VALUES (1,0)')
        conn.execute('INSERT INTO '+owner.table('stages')+" VALUES (1,0,0,'1',1,1)")
        conn.execute('INSERT INTO '+owner.table('custody')+" VALUES ('1',0)")
        conn.execute('INSERT INTO '+owner.table('map')+" VALUES (1,'1',0)")
        conn.execute('INSERT INTO '+owner.table('generations')+' VALUES (1,0,1)')
        conn.execute('INSERT INTO '+owner.table('head')+' VALUES (?,1,1)', (owner.name,))
        for _, sql in definitions:
            if sql.startswith('CREATE TRIGGER'):
                conn.execute(sql)
    owner.validate_installed(conn)
    return original


def ready(conn):
    preparation.JOB.install(conn)
    finish(conn)
    for _ in range(20):
        stage = publication.OWNER.step(conn, batch_size=1)
        if stage.complete:
            return stage.id
    pytest.fail('Recipe publication did not stage')


@contextmanager
def upgrade_connection(path):
    """Historical runners own SQL, independently of Store's request diagnostics."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        yield conn


@pytest.mark.parametrize('recipe_input', [79,88], indirect=True)
def test_selected_membership_keeps_sources_and_pinned_reads(recipe_input):
    store, _ = recipe_input
    original = seed(store,LISTS)
    canonicalize_known(store.conn)
    history = list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))
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
        assert selected == SELECTED
        assert reader.road_trip.list_road_trip_recipes()==[selected]
    regular.migrate(store.conn)
    assert list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))==history
    assert preparation.read_original(head(store.conn))==original
    assert dict(zip(preparation.FIELDS,store.conn.execute('SELECT artists,genres,blacklist_genres FROM road_trip_recipes WHERE id=1').fetchone()))==original


@pytest.fixture
def recipe_upgrade_input(request, tmp_path, monkeypatch):
    bound_catalog(monkeypatch)
    version, target = request.param
    path = tmp_path / 'recipe-upgrade.db'
    with upgrade_connection(path) as conn:
        with closing(sqlite3.connect(':memory:')) as source:
            # These historical edges own recipe conversion and archive custody.
            # Complete unrelated regular owners belong to the admission cases.
            install_recipe_input(source, version, regular_owners=False)
            source.backup(conn)
        if version < 89:
            original = seed(SimpleNamespace(conn=conn), LISTS)
            canonicalize_known(conn)
            publication.OWNER.publish(conn, stage=ready(conn))
        else:
            original = seed_native_input(conn)
        yield conn, path, version, target, original


@pytest.mark.parametrize('recipe_upgrade_input', [
    (79,88), (88,89), (89,94), (94,98), (98,102), (103,104), (104,106), (106,107),
], indirect=True, ids=['79-to-88','88-to-89','89-to-94','94-to-98','98-to-102',
                      '103-to-104','104-to-106','106-to-107'])
def test_populated_recipe_upgrade_keeps_membership_and_publication_archive(recipe_upgrade_input):
    conn, path, version, target, original = recipe_upgrade_input
    history = [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations')]
    captured = dict(head(conn))
    runner.migrate(conn, registry=registry.MIGRATIONS[:target])
    assert [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations')][:version] == history
    assert [tuple(row)[:2] for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == [
        (item.version, item.name) for item in registry.MIGRATIONS[:target]]
    expected = {**LISTS, 'genres':['Canonical known','Unadmitted genre'],
                'blacklist_genres':['Canonical known']}
    if target < 89:
        assert dict(zip(preparation.FIELDS, conn.execute(
            'SELECT artists,genres,blacklist_genres FROM road_trip_recipes WHERE id=1').fetchone())) == original
    else:
        assert recipe_memberships.read(conn, 1) == expected
        assert publication.OWNER.selected(conn) is None
        assert [tuple(row) for row in conn.execute('SELECT * FROM recipe_artists')] == [(1,111,0)]
        assert [tuple(row) for row in conn.execute(
            'SELECT position,raw_label FROM recipe_unresolved_artists ORDER BY position')] == [
                (1,'Unadmitted artist'), (2,'Unadmitted artist')]
    assert dict(head(conn)) == captured
    assert preparation.read_original(head(conn)) == original
    publication.OWNER.validate_installed(conn)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('recipe_upgrade_input', [(103,103)], indirect=True, ids=['renamed-input'])
def test_name_cutover_restores_recipe_programs_and_immutable_archive(recipe_upgrade_input):
    conn, _, _, _, original = recipe_upgrade_input
    # Account/scope renames have their own populated migration owners. Recipe
    # tables keep their names; their cutover responsibility is restoring the
    # dependent SQL and custom preparation/custody programs after those renames.
    members = recipe_memberships.read(conn, 1)
    captured = dict(head(conn))
    dependent = list(conn.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE type IN ('view','trigger') ORDER BY rowid"))
    with conn:
        conn.execute('BEGIN')
        for kind, name, _ in reversed(dependent):
            conn.execute('DROP '+kind+' "'+name.replace('"','""')+'"')
        v103_entity_names.restore_programs(conn, dependent, retention_progress=False)
    runner.validate_version(conn, 103, foreign_keys=False, data=False)
    publication.OWNER.validate_installed(conn)
    assert recipe_memberships.read(conn, 1) == members
    assert dict(head(conn)) == captured
    assert preparation.read_original(head(conn)) == original
    with pytest.raises(sqlite3.IntegrityError, match='custody is retained'), conn:
        conn.execute('UPDATE '+preparation.HEAD+" SET filename='lost' WHERE recipe_id=1")
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture
def native_recipe_input(tmp_path, monkeypatch):
    bound_catalog(monkeypatch)
    path = tmp_path / 'native-recipe.db'
    with upgrade_connection(path) as conn:
        with bounded_consumer_schema((('rock','Rock'), ('jazz','Jazz')), snapshot_guards=False) as schema:
            schema.backup(conn)
        original = seed_native_input(conn)
    return path, original


def test_native_recipe_and_original_are_admitted_after_database_reopen(native_recipe_input):
    path, original = native_recipe_input
    with upgrade_connection(path) as conn:
        # Execute the actual production admission owner on the reopened file.
        # HTTP/Store construction and snapshot readers are independent owners.
        history = [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations')]
        captured = dict(head(conn))
        assert runner.migrate(conn, defer_validation=True) is False
        assert [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations')] == history
        assert dict(head(conn)) == captured
        assert preparation.read_original(head(conn)) == original
        assert recipe_memberships.read(conn, 1) == {field:SELECTED[field] for field in preparation.FIELDS}


def test_admitted_native_recipe_reader_matches_published_values(native_recipe_input):
    path, original = native_recipe_input
    reopened = Store(path)
    try:
        # Completed native SQL is this controller/repository consumer's input.
        # The preceding owner checks real admission of the same fixture contract.
        reopened._schema_compatibility = False
        reopened._bind_repositories()
        recipe = reopened.road_trip.get_road_trip_recipe(1)
        assert recipe == SELECTED
        assert preparation.read_original(head(reopened.conn)) == original
    finally:
        reopened.conn.close()


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
