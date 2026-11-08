"""Direct SQL boundaries and lossless installed-domain cutover."""
import sqlite3

import pytest

from yt_playlist.core import genre_identity
from yt_playlist.core.migrations import current, registry, migrate, Migration, MigrationError
from yt_playlist.core.migrations import v104_domain_contracts as domains
from yt_playlist.core.store import Store
from tests.genre_catalog_support import bound_catalog
from tests.regular_schema_support import admit_regular_connection, assert_regular_expectation, captured, regular_expectation
from tests.store_fixture_support import install_regular_input


@pytest.fixture(scope='module', autouse=True)
def domain_catalog():
    # These cases own SQL domains and installed cutover, not vocabulary size.
    with pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        yield


@pytest.fixture
def disable_periodic_library_sync():
    # Pure SQL owners never construct the web app or start periodic sync.
    pass


def seed(conn):
    statements = (
        "INSERT INTO accounts(id,label,credential_ref,is_master) VALUES (1,'Main','credential',1)",
        "INSERT INTO artists(id,name,provisional) VALUES (1,'Artist',0)",
        "INSERT INTO albums(id,title,metadata_rank) VALUES (1,'Album',0)",
        "INSERT INTO recordings(id,identity_key) VALUES (1,'recording')",
        "INSERT INTO youtube_items(video_id,recording_id,available) VALUES ('video',1,1)",
        "INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,1,'video')",
        "INSERT INTO catalog_entries(id,recording_id,source_ref) VALUES (2,1,'unknown')",
        "INSERT INTO unresolved_items(entry_id,available) VALUES (2,NULL)",
        "INSERT INTO entry_artists VALUES (1,0,1,'Artist','test')",
        "INSERT INTO album_artists VALUES (1,0,1,'Artist')",
        "INSERT INTO playlist_scopes(id,provider_playlist_id,kind) VALUES ('PL','PL','shared')",
        "INSERT INTO playlists(id,account_id,provider_playlist_id,playlist_scope_id,track_count) VALUES (1,1,'PL','PL',0)",
        "INSERT INTO taste_modes(mode_id,label,families,centroid,size,rep_keys,active,first_seen,last_seen) VALUES (1,'Mode','[]',X'',0,'[]',1,-1,0)",
        "INSERT INTO discovery_memberships(recording_id,entry_id) VALUES (1,1)",
        "INSERT INTO album_discovery_memberships(album_id) VALUES (1)",
        "INSERT INTO discovered_artists(artist_id) VALUES (1)",
        "INSERT INTO discovery_legacy_offers(recording_id,offered_count) VALUES (1,0)",
        "INSERT INTO discovery_impressions(view_id,recording_id,shown_at) VALUES ('view',1,-1)",
        "INSERT INTO unresolved_discovery_impressions VALUES ('old','track','unknown',-1,'missing_owner')",
        "INSERT INTO rec_response_events(event_id,kind,identity_key,at) VALUES ('event','historical_kind','recording',-1)",
        "INSERT INTO rec_served_impressions(request_id,surface,lane,identity_key,rank,model_version,provenance,served_at) VALUES ('request','home','lane','recording',0,'model','{}',-1)",
        "INSERT OR IGNORE INTO rec_content_pending(identity_key) VALUES ('recording')",
        "INSERT INTO rec_musical_pending(source_key,revision) VALUES ('test',1)",
        "INSERT OR IGNORE INTO rec_vibe_pending(group_key) VALUES ('*')",
    )
    for sql in statements:
        conn.execute(sql)
    conn.commit()


@pytest.fixture(scope='module')
def constrained(tmp_path_factory, _pristine_current_schema):
    store = Store(tmp_path_factory.mktemp('domains')/'current.db')
    try:
        _pristine_current_schema.backup(store.conn)
        store.init_schema()
        seed(store.conn)
        yield store.conn
    finally:
        store.conn.close()


INVALID = []
for _table,_columns in domains.CONTRACTS.items():
    for _column,_expression in _columns.items():
        _bad = float('inf') if 'abs(' in _expression else 2 if ' IN (0,1)' in _expression else 0 if '>=1' in _expression else -1
        INVALID.append(pytest.param(_table,_column,_bad,id=_table+'.'+_column))
        if 'integer' in _expression and 'abs(' not in _expression:
            INVALID.append(pytest.param(_table,_column,1.5,id=_table+'.'+_column+'.fractional'))


@pytest.mark.parametrize('table,column,value',INVALID)
def test_invalid_stored_domains_are_rejected_by_direct_sql(constrained,table,column,value):
    conn = constrained
    assert conn.execute('SELECT count(*) FROM '+table).fetchone()[0]>0
    conn.execute('SAVEPOINT boundary')
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f'UPDATE {table} SET {column}=?',(value,))
    finally:
        conn.execute('ROLLBACK TO boundary')
        conn.execute('RELEASE boundary')


def test_valid_zero_flags_unknowns_and_pre_epoch_times_remain_legal(constrained):
    conn = constrained
    conn.execute('SAVEPOINT boundary')
    try:
        for table,fields in domains.CONTRACTS.items():
            for column,expression in fields.items():
                value = -1 if 'abs(' in expression else 1 if '>=1' in expression else 0
                conn.execute(f'UPDATE {table} SET {column}=?',(value,))
                if 'IS NULL OR' in expression:
                    conn.execute(f'UPDATE {table} SET {column}=NULL')
        domains.preflight(conn)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        conn.execute('ROLLBACK TO boundary')
        conn.execute('RELEASE boundary')


def test_final_account_and_recipe_boundaries_keep_valid_exchange_and_reload(constrained):
    conn = constrained
    conn.execute('SAVEPOINT final_contracts')
    try:
        for flag in ('active','is_master'):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('UPDATE accounts SET '+flag+'=7')
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO accounts(label,credential_ref,is_master) VALUES ('Other','credential',1)")
        conn.execute('UPDATE accounts SET is_master=0')
        conn.execute("INSERT INTO accounts(label,credential_ref,is_master) VALUES ('Other','credential',1)")
        assert conn.execute('SELECT count(*) FROM accounts WHERE active=1 AND is_master=1').fetchone()[0]==1
        recipe = conn.execute('INSERT INTO road_trip_recipes(name,own_pct,familiarity_pct,target_minutes,created_at,updated_at) '
                              "VALUES ('Trip',0,100,15,-1,0)").lastrowid
        for column,value in [('own_pct',140),('own_pct',.5),('familiarity_pct',-5),
                             ('target_minutes',14),('target_minutes',721),('target_minutes',30.5),
                             ('created_at',float('inf')),('updated_at',float('-inf'))]:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('UPDATE road_trip_recipes SET '+column+'=? WHERE id=?',(value,recipe))
        conn.execute('UPDATE road_trip_recipes SET own_pct=100,familiarity_pct=0,target_minutes=720 WHERE id=?',(recipe,))
        assert tuple(conn.execute('SELECT own_pct,familiarity_pct,target_minutes FROM road_trip_recipes WHERE id=?',(recipe,)).fetchone())==(100,0,720)
    finally:
        conn.execute('ROLLBACK TO final_contracts')
        conn.execute('RELEASE final_contracts')


def installed(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        # This constructs one predecessor, not hundreds of separate durable
        # DDL commits. Every test still runs its own admission and upgrade.
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            for sql in current.statements(version=103):
                conn.execute(sql)
            genre_identity.seed_catalog(conn)
            conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
            conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                                  [(m.version,m.name) for m in registry.MIGRATIONS[:103]])
            seed(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def admit_reopened_store(path):
    store = Store(path)
    try:
        store.init_schema()
    finally:
        store.conn.close()


@pytest.fixture(scope='module')
def domain_predecessor(domain_catalog):
    source = installed(':memory:')
    try:
        with source:
            source.execute('CREATE VIEW installed_credit AS SELECT entry_id,position FROM entry_artists')
            source.execute('CREATE TABLE installed_receipts(position INTEGER)')
            source.execute('CREATE TRIGGER installed_position AFTER UPDATE OF position ON entry_artists '
                           'BEGIN INSERT INTO installed_receipts VALUES (NEW.position); END')
            # Preserve deleted high IDs, including an empty autoincrement owner.
            source.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name IN ('recordings','rec_content_pending')")
            source.execute('DELETE FROM rec_musical_pending')
            source.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name='rec_musical_pending'")
        from tests.prepared_fixture_support import coordinates
        assert not coordinates(source), 'SQL-only predecessor retains external files'
        source.execute('PRAGMA query_only=ON')
        yield source
    finally:
        source.close()


def clone_predecessor(path, source):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        source.backup(conn)
        return conn
    except BaseException:
        conn.close()
        raise


@pytest.fixture(scope='module')
def admitted_predecessor(domain_predecessor):
    """Completed regular owners below each real domain conversion/failure."""
    from yt_playlist.core.migrations import regular

    source = clone_predecessor(':memory:', domain_predecessor)
    try:
        expected = regular_expectation(source, clock=1)
        with source:
            source.execute('BEGIN')
            install_regular_input(source, regular.CHANGES)
        assert_regular_expectation(source, expected)
        from tests.prepared_fixture_support import coordinates
        assert not coordinates(source), 'SQL-only predecessor retains external files'
        source.execute('PRAGMA query_only=ON')
        yield source
    finally:
        source.close()


@pytest.fixture(scope='module')
def interrupted_predecessor(admitted_predecessor):
    source = clone_predecessor(':memory:', admitted_predecessor)
    try:
        before = list(source.iterdump())

        def interrupted(conn, context):
            domains.apply(conn, context)
            raise RuntimeError('interrupted domain cutover')

        with pytest.raises(MigrationError, match='interrupted domain cutover'):
            migrate(source, registry=(*registry.MIGRATIONS[:103],
                Migration(104, 'domain_contracts', interrupted, rebuild_tables=True)))
        assert list(source.iterdump()) == before
        source.execute('PRAGMA query_only=ON')
        yield source
    finally:
        source.close()


@pytest.fixture(scope='module')
def converted_predecessor(interrupted_predecessor):
    source = clone_predecessor(':memory:', interrupted_predecessor)
    try:
        before = captured(source)
        programs = dict(source.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger')"))
        migrate(source, registry=registry.MIGRATIONS[:104], clock=lambda: 2000)
        assert captured(source) == {**before, 'schema_migrations': sorted(
            before['schema_migrations'] + [(104, registry.MIGRATIONS[103].name, 2000)], key=repr)}
        actual = dict(source.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger')"))
        assert {name: actual[name] for name in programs} == programs
        from tests.prepared_fixture_support import coordinates
        assert not coordinates(source), 'SQL-only predecessor retains external files'
        source.execute('PRAGMA query_only=ON')
        yield source
    finally:
        source.close()


@pytest.fixture(scope='module')
def invalid_regular_predecessor(domain_predecessor):
    source = clone_predecessor(':memory:', domain_predecessor)
    try:
        with source:
            source.execute('UPDATE recordings SET provisional=7')
        # Real first admission precedes the domain conversion, even with an
        # invalid historical value. The helper verifies every retained row.
        admit_regular_connection(source)
        source.execute('PRAGMA query_only=ON')
        yield source
    finally:
        source.close()


def test_domain_predecessor_input_keeps_unconverted_values_and_history(domain_predecessor):
    assert domain_predecessor.execute('PRAGMA query_only').fetchone()[0] == 1
    assert list(map(tuple, domain_predecessor.execute(
        'SELECT version,name FROM schema_migrations ORDER BY version'))) == [
            (entry.version, entry.name) for entry in registry.MIGRATIONS[:103]]
    assert not domain_predecessor.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='schema_changes'").fetchone()
    assert tuple(domain_predecessor.execute(
        'SELECT id,identity_key,provisional FROM recordings').fetchone()) == (1, 'recording', 1)
    assert not domain_predecessor.execute('PRAGMA foreign_key_check').fetchone()


def test_domain_regular_input_keeps_installed_values_and_complete_history(admitted_predecessor):
    from yt_playlist.core.migrations import regular

    assert admitted_predecessor.execute('PRAGMA query_only').fetchone()[0] == 1
    assert regular.validate(admitted_predecessor) == len(regular.CHANGES)
    assert tuple(admitted_predecessor.execute(
        'SELECT id,identity_key,provisional FROM recordings').fetchone()) == (1, 'recording', 1)
    assert not admitted_predecessor.execute('PRAGMA foreign_key_check').fetchone()


def test_interrupted_replacement_rolls_back_every_owner_then_restarts(interrupted_predecessor, admitted_predecessor):
    assert captured(interrupted_predecessor) == captured(admitted_predecessor)
    assert list(interrupted_predecessor.iterdump()) == list(admitted_predecessor.iterdump())
    assert not interrupted_predecessor.execute('PRAGMA foreign_key_check').fetchone()


def test_domain_conversion_input_preserves_programs_sequences_and_values(converted_predecessor):
    from yt_playlist.core.migrations import runner

    assert converted_predecessor.execute('PRAGMA query_only').fetchone()[0] == 1
    runner.validate_version(converted_predecessor, 104)
    assert dict(converted_predecessor.execute(
        "SELECT name,seq FROM sqlite_sequence WHERE name IN ('recordings','rec_musical_pending')")) == {
            'recordings': 1000, 'rec_musical_pending': 1000}
    assert not converted_predecessor.execute('PRAGMA foreign_key_check').fetchone()


def test_invalid_domain_regular_input_keeps_original_value_before_conversion(invalid_regular_predecessor):
    from yt_playlist.core.migrations import regular

    assert invalid_regular_predecessor.execute('PRAGMA query_only').fetchone()[0] == 1
    assert regular.validate(invalid_regular_predecessor) == len(regular.CHANGES)
    assert invalid_regular_predecessor.execute('SELECT provisional FROM recordings').fetchone()[0] == 7
    assert invalid_regular_predecessor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 103


@pytest.mark.parametrize('value',[2,-1,1.5,'1'])
def test_catalog_availability_rejects_coercion_before_admitting_an_owner(store,value):
    from yt_playlist.core import catalog
    before = captured(store.conn)
    with pytest.raises(ValueError,match='Availability'):
        catalog.upsert(store.conn,'new','Title','Artist',available=value)
    assert captured(store.conn)==before


@pytest.mark.parametrize('size',[True,-1,1.5,'1'])
def test_mode_size_rejects_coercion_before_partial_publication(store,size):
    before = captured(store.conn)
    valid = {'mode_id':1,'label':'Mode','centroid':[1.0],'size':1,'families':[],'rep_keys':[]}
    with pytest.raises(ValueError,match='Mode size'):
        store.modes.replace_modes([valid,{**valid,'mode_id':2,'size':size}],[],1)
    assert captured(store.conn)==before


def converted_history(before):
    return sorted(before['schema_migrations'] + [
        (entry.version, entry.name, 2000) for entry in registry.MIGRATIONS[103:]], key=repr)


def test_installed_upgrade_preserves_values_sequences_dependencies_and_matches_fresh(
        tmp_path, admitted_predecessor, converted_predecessor):
    path = tmp_path/'upgrade.db'
    conn = clone_predecessor(path, converted_predecessor)
    try:
        before = captured(admitted_predecessor)
        sql = dict(admitted_predecessor.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger')"))
        migrate(conn, registry=registry.MIGRATIONS, clock=lambda: 2000)
        admit_reopened_store(path)
        assert captured(conn)=={**before, 'schema_migrations': converted_history(before)}
        actual = dict(conn.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger')"))
        assert {name:actual[name] for name in sql}==sql
        # Subsequent migrations can add guards without rewriting installed programs.
        earlier = current._schema_contract(current.statements(version=103))
        added = {name for name,(kind,_) in current.contract().items()
                 if kind in ('view','trigger') and name not in earlier}
        assert actual.keys()-sql.keys()==added
        current.validate(conn)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        with conn:
            conn.execute('UPDATE entry_artists SET position=1')
        assert tuple(conn.execute('SELECT * FROM installed_credit').fetchone())==(1,1)
        assert conn.execute('SELECT position FROM installed_receipts').fetchone()[0]==1
        with conn:
            rid = conn.execute("INSERT INTO recordings(identity_key) VALUES ('new')").lastrowid
        assert rid>1000
        with conn:
            position = conn.execute("INSERT INTO rec_musical_pending(source_key,revision) VALUES ('after',1)").lastrowid
        assert position>1000
    finally:
        conn.close()


@pytest.mark.parametrize('table,column,bad',[
    ('recordings','provisional',7),('taste_modes','size',-1),
    ('discovery_memberships','offered_count',1.5),('entry_artists','position',.5),
    ('discovery_impressions','shown_at',float('inf')),
])
def test_invalid_predecessor_stops_with_exact_owner_and_retains_original_database(
        tmp_path,admitted_predecessor,table,column,bad):
    path = tmp_path/'invalid.db'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        admitted_predecessor.backup(conn)
        with conn:
            conn.execute(f'UPDATE {table} SET {column}=?',(bad,))
        assert_invalid_upgrade_retains_database(conn,path,table,column)
    finally:
        conn.close()


def assert_invalid_upgrade_retains_database(conn,path,table,column):
    before = list(conn.iterdump())
    with pytest.raises(MigrationError,match=table+r'\.'+column+r'.*original rows retained'):
        migrate(conn, registry=registry.MIGRATIONS)
    retained = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        assert list(retained.iterdump())==before
    finally:
        retained.close()


def test_first_regular_admission_preserves_invalid_domain_before_failed_conversion(tmp_path, invalid_regular_predecessor):
    path = tmp_path/'first-admission.db'
    conn = clone_predecessor(path, invalid_regular_predecessor)
    try:
        assert_invalid_upgrade_retains_database(conn,path,'recordings','provisional')
    finally:
        conn.close()
