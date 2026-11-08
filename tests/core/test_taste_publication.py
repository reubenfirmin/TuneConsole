"""Independent folds, canonical aliases and compatible writers preserve taste decisions."""
import sqlite3
from contextlib import closing

import pytest

from tests.core.test_predecessor_writes import installed
from tests.independent_schema_support import independent_regular_ddl
from tests.core.test_taste_axis_preparation import seed, source, finish
from tests.catalog_migration_support import catalog_reader
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import taste_axes
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import taste_axes as preparation, taste_publication as publication
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Taste SQL/publication checks do not start an application lifespan."""


@pytest.fixture
def taste_installed(request, tmp_path, monkeypatch):
    bound_catalog(monkeypatch)
    version = request.param
    store = Store(tmp_path / 'taste.db')
    try:
        store.conn.execute('PRAGMA synchronous=OFF')
        with closing(sqlite3.connect(':memory:')) as schema:
            install_numbered_input(schema, version, regular_owners=regular.CHANGES)
            schema.backup(store.conn)
        store.init_schema(defer_metadata=True)
        yield store, version
    finally:
        store.conn.close()


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


TASTE_FOLDS = (
    ('rec_weights', 'axis,weight,updated_at', 'axis', [('genre:known', 1.4, 11)]),
    ('rec_lean', 'axis,value,updated_at,last_graduated_day', 'axis', [('genre:known', 1.3, 12, None)]),
    ('home_hidden_facet', 'axis', 'axis', [('genre:known',)]),
    ('rec_theme', 'facet,score,updated_at', 'facet', [('genre:known', 1.5, 11)]),
    ('rec_play_grad', 'axis,last_graduated_day', 'axis', [('genre:known', '2026-10-29')]),
    ('rec_grad_log', 'id,axis,source,score,factor,new_weight,created_at', 'axis', [
        (1, 'genre:known', 'play', 2.0, 1.1, 1.1, 10),
        (2, 'genre:known', 'rating', -0.5, 0.9, 0.99, 11)]),
)

TASTE_ORIGINALS = (
    ('rec_weights', 'axis,weight,updated_at', [('genre:Known alias', 1.4, 11)]),
    ('rec_lean', 'axis,value,updated_at,last_graduated_day', [('genre:known', 1.3, 12, None)]),
    ('home_hidden_facet', 'axis', [('genre:Known alias',), ('genre:known',)]),
    ('rec_theme', 'facet,score,updated_at', [('genre:Known alias', -0.5, 11), ('genre:known', 2.0, 10)]),
    ('rec_play_grad', 'axis,last_graduated_day', [('genre:Known alias', '2026-10-29'), ('genre:known', '2026-10-28')]),
    ('rec_grad_log', 'id,axis,source,score,factor,new_weight,created_at', [
        (1, 'genre:known', 'play', 2.0, 1.1, 1.1, 10),
        (2, 'genre:Known alias', 'rating', -0.5, 0.9, 0.99, 11)]),
)


def seed_native_taste(conn):
    """Completed v88 input below later conversion and startup owners.

    The real v88 owner independently compares every fold with publication and
    retains every original contribution; later edges need not rerun it.
    """
    with conn:
        conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('known','Known','custom')")
        conn.execute("INSERT INTO genre_alias VALUES ('known alias','Known alias','known','custom')")
        axis_id = taste_axes.identity(conn, 'genre:known', create=True)
        for owner, columns, token, expected in TASTE_FOLDS:
            names = columns.split(',')
            names[names.index(token)] = 'axis_id'
            values = [tuple(axis_id if names[index] == 'axis_id' else value
                            for index, value in enumerate(row)) for row in expected]
            conn.executemany('INSERT INTO ' + taste_axes.table(conn, owner) + '(' + ','.join(names) +
                             ') VALUES (' + ','.join('?' for _ in names) + ')', values)
        original = {owner: [dict(zip(columns.split(','), row)) for row in values]
                    for owner, columns, values in TASTE_ORIGINALS}
        fields = ('weight', 'value', 'score', 'updated_at', 'last_graduated_day',
                  'source', 'factor', 'new_weight', 'created_at')
        for owner, contributions in original.items():
            for row in contributions:
                token = row['facet' if owner == 'rec_theme' else 'axis']
                conn.execute('INSERT INTO taste_axis_legacy VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (owner, str(row.get('id', token)), token, axis_id, *[row.get(field) for field in fields]))
    return original


def assert_taste_folds(conn):
    for owner, columns, token, expected in TASTE_FOLDS:
        assert rows(conn, owner, columns, token) == expected


def assert_original_taste_contributions(conn, original):
    retained = {}
    for owner, contributions in original.items():
        token = 'facet' if owner == 'rec_theme' else 'axis'
        retained[owner] = []
        for contribution in contributions:
            key = str(contribution.get('id', contribution[token]))
            row = conn.execute('SELECT * FROM taste_axis_legacy WHERE table_name=? AND row_key=?',
                               (owner, key)).fetchone()
            assert row is not None
            assert row['raw_axis'] == contribution[token]
            assert taste_axes.identity(conn, 'genre:known') == row['axis_id']
            assert {column: row['lean_value' if column == 'value' else column]
                    for column in contribution if column not in (token, 'id')} == {
                        column: value for column, value in contribution.items() if column not in (token, 'id')}
            retained[owner].append(key)
    assert conn.execute('SELECT count(*) FROM taste_axis_legacy').fetchone()[0] == sum(map(len, retained.values()))


@pytest.fixture
def taste_upgrade_input(request, tmp_path, monkeypatch):
    bound_catalog(monkeypatch)
    version, target = request.param
    path = tmp_path / 'taste-upgrade.db'
    with closing(sqlite3.connect(':memory:')) as schema, closing(sqlite3.connect(path)) as conn:
        # Admission is covered by taste_installed. Each numbered edge gets the
        # completed lower schema, rather than replaying every earlier edge.
        install_numbered_input(schema, version, regular_owners=regular.CHANGES if version == 107 else ())
        conn.execute('PRAGMA synchronous=OFF')
        schema.backup(conn)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        original = None
        if version < 88:
            with catalog_reader(path, version) as reader:
                reader.conn.execute('PRAGMA synchronous=OFF')
                reconciled_seed(reader)
                original = source(reader.conn)
                publication.publish(reader.conn, identity=ready(reader.conn))
        else:
            original = seed_native_taste(conn)
        yield conn, path, version, target, original


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


@pytest.mark.parametrize('taste_installed', [79, 87], indirect=True)
def test_selected_folds_preserve_exact_sources_and_pinned_reader(taste_installed):
    store, _ = taste_installed
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
    regular.migrate(store.conn)
    assert source(store.conn) == original
    assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations'))) == history


@pytest.mark.parametrize('taste_upgrade_input', [
    (79, 87), (87, 88), (88, 98), (98, 102), (103, 104), (104, 107)
], indirect=True, ids=['79-to-87', '87-to-88', '88-to-98', '98-to-102', '103-to-104', '104-to-107'])
def test_registered_taste_upgrade_preserves_exact_folds(taste_upgrade_input):
    conn, path, version, target, original = taste_upgrade_input
    history = list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version')))
    assert_taste_folds(conn)
    runner.migrate(conn, registry=registry.MIGRATIONS[:target])
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        *history, *[(item.version, item.name) for item in registry.MIGRATIONS[version:target]]]
    assert_taste_folds(conn)
    if target < 88:
        assert publication.selected(conn) is not None
        assert source(conn) == original
    else:
        assert publication.selected(conn) is None
        if original is not None:
            assert_original_taste_contributions(conn, original)
    with catalog_reader(path, target) as reader:
        assert_selected_taste(reader)
        assert reader.rec_model.get_weights(now=11) == {'genre:known': 1.4}
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('taste_upgrade_input', [(103, 103)], indirect=True, ids=['renamed-input'])
def test_name_cutover_restores_taste_readers(taste_upgrade_input):
    from yt_playlist.core.migrations import v103_entity_names

    conn, path, _version, target, original = taste_upgrade_input
    programs = list(conn.execute("SELECT type,name,sql FROM sqlite_schema WHERE type IN ('view','trigger') ORDER BY rowid"))
    with conn:
        conn.execute('BEGIN')
        for kind, name, _sql in reversed(programs):
            conn.execute('DROP ' + kind + ' "' + name.replace('"', '""') + '"')
        v103_entity_names.restore_programs(conn, programs, retention_progress=False)
    runner.validate_version(conn, 103, foreign_keys=False, data=False)
    assert_taste_folds(conn)
    assert_original_taste_contributions(conn, original)
    with catalog_reader(path, target) as reader:
        assert_selected_taste(reader)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('taste_upgrade_input', [(107, 107)], indirect=True, ids=['completed-native'])
def test_native_admission_and_reopen_preserve_exact_taste(taste_upgrade_input):
    _conn, path, _version, _target, original = taste_upgrade_input
    native = Store(path)
    try:
        native.conn.execute('PRAGMA synchronous=OFF')
        native.init_schema(defer_metadata=True)
        assert publication.selected(native.conn) is None
        assert_taste_folds(native.conn)
        assert_original_taste_contributions(native.conn, original)
        assert_selected_taste(native)
        assert native.rec_model.get_weights(now=11) == {'genre:known': 1.4}
        regular.validate(native.conn)
    finally:
        native.conn.close()
    with catalog_reader(path, 107) as reopened:
        assert_selected_taste(reopened)
        assert_original_taste_contributions(reopened.conn, original)


@pytest.mark.parametrize('installed',[87],indirect=True)
def test_competing_configured_values_block_selection_without_losing_history(installed):
    store,_ = installed
    seed(store)
    original = source(store.conn)
    identity = ready(store.conn)
    with pytest.raises(MigrationError,match='Multiple configured'):
        publication.publish(store.conn,identity=identity)
    assert publication.selected(store.conn) is None
    assert source(store.conn)==original


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


@pytest.mark.parametrize('installed',[87],indirect=True)
@pytest.mark.parametrize('damage',['value','axis','missing'])
def test_independent_source_and_identity_verification_rejects_damage(installed,damage):
    store,_ = installed
    reconciled_seed(store)
    preparation.JOB.install(store.conn)
    finish(store.conn)
    with store.conn:
        if damage=='value':
            store.conn.execute("UPDATE prepared_taste_rows SET weight=2.9 WHERE owner='rec_weights'")
        elif damage=='axis':
            store.conn.execute("UPDATE prepared_taste_axis_keys SET genre_key='wrong' WHERE kind='genre'")
        else:
            store.conn.execute("DELETE FROM prepared_taste_rows WHERE owner='rec_weights'")
    for _ in range(20):
        current = publication.step(store.conn)
        if current.complete:
            break
    with pytest.raises(MigrationError,match='differs|missing or extra'):
        publication.publish(store.conn,identity=current.id)
    assert publication.selected(store.conn) is None
    assert store.rec_model.weight_records()['genre:Known alias']['weight']==1.4


@pytest.mark.parametrize('installed',[87],indirect=True)
def test_interruption_stale_publication_and_failed_retry_leave_old_sources_usable(installed,monkeypatch, independent_regular_ddl):
    store,_ = installed
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
    regular.migrate(store.conn)
    first = ready(store.conn)
    publication.publish(store.conn,identity=first)
    with store.conn:
        store.conn.execute("UPDATE rec_lean SET axis='' WHERE axis='genre:known'")
    with pytest.raises(ValueError,match='taste axis'):
        finish(store.conn)
    assert preparation.JOB.progress(store.conn).status=='failed'
    regular.migrate(store.conn)
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


@pytest.mark.parametrize('installed',[87],indirect=True)
def test_distinct_family_and_genre_identity_cannot_hide_each_other_at_api_boundary(installed):
    store,_ = installed
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
