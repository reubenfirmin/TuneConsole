"""Held choice identities and ordered demand survive selection and continuing writes."""
import json
import sqlite3
from contextlib import closing, ExitStack

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.independent_schema_support import independent_regular_ddl
from tests.core.test_demand_preparation import VALUES, seed, finish
from tests.catalog_migration_support import catalog_reader
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import prepared_template
from tests.store_fixture_support import install_numbered_input, publication_consumer_reads
from yt_playlist.core import recommendation_requests
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import recommendation_demand as preparation, demand_publication as publication
from yt_playlist.core.store import Store


EXPECTED = {**VALUES, 'choice_window:mixes':None}


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/publication owners never start an application lifespan."""


@pytest.fixture(scope='module')
def demand_inputs(tmp_path_factory):
    root, templates = tmp_path_factory.mktemp('demand-inputs'), {}
    with ExitStack() as resources:
        def admitted(version):
            if version not in templates:
                with closing(sqlite3.connect(':memory:')) as schema, pytest.MonkeyPatch.context() as patch:
                    bound_catalog(patch)
                    install_numbered_input(schema, version, regular_owners=regular.CHANGES)
                    templates[version] = resources.enter_context(prepared_template(
                        schema, lambda store: None, database=root / f'{version}.db', defer_metadata=True))
            return templates[version]
        yield admitted


@pytest.mark.parametrize('version', [79, 86])
def test_demand_input_admission_keeps_numbered_history_and_real_guards(demand_inputs, version):
    template = demand_inputs(version)
    source = template.source
    with closing(sqlite3.connect(source._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, version)
        regular.validate(reader)
    assert source.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert template.revision_guards


def test_demand_consumer_input_keeps_pending_ddl_and_real_guards(_bounded_installed_schema):
    template = _bounded_installed_schema(86)
    source = template.source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert list(map(tuple, source.conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:86]]
    assert regular.validate(source.conn) == 0
    assert template.revision_guards
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture
def demand_installed(request, tmp_path, demand_inputs):
    with demand_inputs(request.param).open(tmp_path / 'demand.db') as store:
        yield store, request.param


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


@pytest.mark.parametrize('demand_installed', [79, 86], indirect=True)
def test_selected_requests_preserve_identity_order_sources_and_pinned_reads(demand_installed):
    store, version = demand_installed
    seed(store)
    originals = list(map(tuple,store.conn.execute('SELECT * FROM rec_proposals ORDER BY surface')))
    history = list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations')))
    with store.read_snapshot() as snapshot:
        pinned = snapshot.store
        assert pinned.rec_proposals.get_proposals('choice_window:mixes') == {}
        publication.publish(store.conn, identity=ready(store.conn))
        assert publication.selected(pinned.conn) is None
        assert pinned.rec_proposals.get_proposals('choice_window:mixes') == {}
    with store.read_view() as reader:
        reader.conn.set_authorizer(lambda action, owner, column, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and owner == 'rec_proposals' and column == 'payload'
            else sqlite3.SQLITE_OK)
        try:
            for surface,value in EXPECTED.items():
                assert reader.rec_proposals.get_proposals(surface) == value
                assert reader.rec_proposals.has_proposals(surface) == bool(value)
        finally:
            reader.conn.set_authorizer(None)
    regular.migrate(store.conn)
    assert list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations'))) == history
    assert list(map(tuple,store.conn.execute('SELECT * FROM rec_proposals ORDER BY surface'))) == originals
    with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, version)
        regular.validate(reader)


def seed_native_demand(conn):
    """Completed v87 input for later edges; the actual v87 owner checks it."""
    from yt_playlist.core import albums

    originals = [(surface, json.dumps(value, indent=2), 100) for surface, value in VALUES.items()]
    with conn:
        conn.execute('BEGIN')
        albums.observe(conn, 'release', title='Release', artist='Artist')
        conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('known-genre','Known label','custom')")
        for surface, value in VALUES.items():
            recommendation_requests.write(conn, surface, value, 100)
        # Retained originals are the exact opaque predecessor text, including
        # the empty mixes object which has no native choice-window row.
        conn.execute(preparation.JOB.destinations[0][1])
        conn.executemany('INSERT INTO prepared_demand_originals VALUES (?,?,?)', originals)
    return sorted(originals)


@pytest.fixture
def demand_upgrade_input(request, tmp_path, monkeypatch):
    bound_catalog(monkeypatch)
    version, target = request.param
    path = tmp_path / 'demand-upgrade.db'
    with closing(sqlite3.connect(':memory:')) as schema, closing(sqlite3.connect(path)) as conn:
        install_numbered_input(schema, version, regular_owners=regular.CHANGES if version == 107 else ())
        conn.execute('PRAGMA synchronous=OFF')
        schema.backup(conn)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        if version < 87:
            with catalog_reader(path, version) as store:
                store.conn.execute('PRAGMA synchronous=OFF')
                seed(store)
                originals = list(map(tuple, store.conn.execute('SELECT * FROM rec_proposals ORDER BY surface')))
                publication.publish(store.conn, identity=ready(store.conn))
        else:
            originals = seed_native_demand(conn)
        yield conn, path, version, target, originals


@pytest.mark.parametrize('demand_upgrade_input', [
    (79, 86), (86, 87), (87, 98), (98, 102), (103, 104), (104, 107)
], indirect=True, ids=['79-to-86', '86-to-87', '87-to-98', '98-to-102', '103-to-104', '104-to-107'])
def test_registered_demand_upgrades_preserve_identity_order_and_opaque_sources(demand_upgrade_input):
    conn, path, version, target, originals = demand_upgrade_input
    history = list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version')))
    runner.migrate(conn, registry=registry.MIGRATIONS[:target])
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        *history, *[(item.version, item.name) for item in registry.MIGRATIONS[version:target]]]
    assert (publication.selected(conn) is None) == (target >= 87)
    if target >= 87:
        for surface, value in EXPECTED.items():
            assert recommendation_requests.read(conn, surface) == value
    else:
        assert list(map(tuple, conn.execute('SELECT * FROM rec_proposals ORDER BY surface'))) == originals
    with catalog_reader(path, target) as reader:
        for surface, value in EXPECTED.items():
            assert reader.rec_proposals.get_proposals(surface) == value
            assert reader.rec_proposals.has_proposals(surface) == bool(value)
    assert list(map(tuple, conn.execute('SELECT * FROM prepared_demand_originals ORDER BY surface'))) == originals
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('demand_upgrade_input', [(103, 103)], indirect=True, ids=['renamed-input'])
def test_name_cutover_restores_exact_demand_readers(demand_upgrade_input):
    from yt_playlist.core.migrations import v103_entity_names

    conn, path, _version, target, originals = demand_upgrade_input
    programs = list(conn.execute("SELECT type,name,sql FROM sqlite_schema WHERE type IN ('view','trigger') ORDER BY rowid"))
    with conn:
        conn.execute('BEGIN')
        for kind, name, _sql in reversed(programs):
            conn.execute('DROP ' + kind + ' "' + name.replace('"', '""') + '"')
        v103_entity_names.restore_programs(conn, programs, retention_progress=False)
    runner.validate_version(conn, target)
    with catalog_reader(path, target) as reader:
        for surface, value in EXPECTED.items():
            assert reader.rec_proposals.get_proposals(surface) == value
    assert list(map(tuple, conn.execute('SELECT * FROM prepared_demand_originals ORDER BY surface'))) == originals


@pytest.mark.parametrize('demand_upgrade_input', [(107, 107)], indirect=True, ids=['completed-native'])
def test_native_admission_and_reopen_preserve_demand_and_opaque_sources(demand_upgrade_input):
    _conn, path, _version, target, originals = demand_upgrade_input
    native = Store(path)
    try:
        native.conn.execute('PRAGMA synchronous=OFF')
        native.init_schema(defer_metadata=True)
        assert publication.selected(native.conn) is None
        for surface, value in EXPECTED.items():
            assert native.rec_proposals.get_proposals(surface) == value
        assert list(map(tuple, native.conn.execute('SELECT * FROM prepared_demand_originals ORDER BY surface'))) == originals
        regular.validate(native.conn)
    finally:
        native.conn.close()
    with catalog_reader(path, target) as reopened:
        for surface, value in EXPECTED.items():
            assert reopened.rec_proposals.get_proposals(surface) == value


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
def test_independent_verification_rejects_damaged_prepared_members(installed,damage):
    store,_ = installed
    seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    with store.conn:
        if damage=='wrong':
            store.conn.execute("UPDATE prepared_home_genre_items SET genre_key=NULL,resolution_reason='unknown_genre' WHERE kind='include' AND position=0")
        elif damage=='extra':
            store.conn.execute("INSERT INTO prepared_album_menu_exclusions VALUES (1,99,'extra',NULL,'missing_album')")
        else:
            store.conn.execute("DELETE FROM prepared_listening_choice_windows WHERE format='albums'")
    for _ in range(30):
        stage = publication.step(store.conn,batch_size=16)
        if stage.complete:
            break
    with pytest.raises(MigrationError,match='differs'):
        publication.publish(store.conn,identity=stage.id)
    assert publication.selected(store.conn) is None
    assert store.rec_proposals.get_proposals('choice_window:albums')==VALUES['choice_window:albums']


@pytest.mark.parametrize('installed',[86],indirect=True)
def test_interruption_resume_and_publication_fence_keep_atomic_selection(installed,monkeypatch, independent_regular_ddl):
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
    regular.migrate(store.conn)
    final = ready(store.conn)
    publication.publish(store.conn,identity=final)
    assert store.rec_proposals.get_proposals('home_genre_request')=={}
    with pytest.raises(sqlite3.IntegrityError,match='retained|cleanup'):
        with store.conn:
            store.conn.execute('DELETE FROM '+publication.table('prepared_demand_originals')+' WHERE stage=?',(final,))


def finite_requests(store, now):
    value = {'epoch':99,'include_genres':['known-genre']*100,'avoid_genres':['Missing']*100,'held_by_vibe':False}
    store.rec_proposals.put_proposals('home_genre_request',value,now)
    store.rec_proposals.put_proposals('album_menu_refresh',{'epoch':98,'exclude':['release']*100},now)
    return value


@pytest.mark.parametrize('installed',[86],indirect=True)
def test_full_finite_demand_uses_bounded_staging(installed, publication_consumer_reads):
    store,_ = installed
    seed(store)
    value = finite_requests(store, 300)
    identity = ready(store.conn,size=16)
    assert publication.stage(store.conn,identity).copied==309
    publication.publish(store.conn,identity=identity)
    assert store.rec_proposals.get_proposals('home_genre_request')==value


@pytest.mark.parametrize('installed',[86],indirect=True)
def test_failed_demand_keeps_regular_changes_and_large_retry_usable(
        installed, independent_regular_ddl, publication_consumer_reads):
    store,_ = installed
    seed(store)
    publication.publish(store.conn,identity=ready(store.conn,size=16))
    with store.conn:
        store.conn.execute("UPDATE rec_proposals SET payload='{bad' WHERE surface='home_genre_request'")
    with pytest.raises(ValueError):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status=='failed'
    assert publication.selected(store.conn) is None
    regular.migrate(store.conn)
    value = finite_requests(store, 301)
    preparation.JOB.step(store.conn,retry=True)
    final = ready(store.conn,size=16)
    assert publication.stage(store.conn,final).copied==309
    publication.publish(store.conn,identity=final)
    assert store.rec_proposals.get_proposals('home_genre_request')==value


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_empty_predecessor_selection_and_native_fresh_bootstrap(installed,tmp_path):
    store,_ = installed
    identity = ready(store.conn)
    publication.publish(store.conn,identity=identity)
    assert publication.stage(store.conn,identity).copied==0
    assert all(store.rec_proposals.get_proposals(surface) is None for surface in VALUES)
    current = Store(tmp_path/'fresh.db')
    try:
        current.init_schema(defer_metadata=True)
        assert publication.selected(current.conn) is None
        for surface,value in VALUES.items():
            current.rec_proposals.put_proposals(surface,value,100)
            assert current.rec_proposals.get_proposals(surface)==EXPECTED[surface]
    finally:
        current.conn.close()
