"""Direct SQL boundaries and lossless installed-domain cutover."""
import sqlite3

import pytest

from yt_playlist.core import genre_identity
from yt_playlist.core.migrations import current, registry, migrate, Migration, MigrationError
from yt_playlist.core.migrations import v104_domain_contracts as domains
from yt_playlist.core.store import Store
from tests.regular_schema_support import admit_regular_schema, captured


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
    store = Store(path)
    try:
        # This constructs one predecessor, not hundreds of separate durable
        # DDL commits. Every test still runs its own admission and upgrade.
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            for sql in current.statements(version=103):
                store.conn.execute(sql)
            genre_identity.seed_catalog(store.conn)
            store.conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
            store.conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                                  [(m.version,m.name) for m in registry.MIGRATIONS[:103]])
            seed(store.conn)
    except BaseException:
        store.conn.close()
        raise
    return store


@pytest.fixture(scope='module')
def admitted_predecessor(tmp_path_factory):
    """Build regular DDL once; each domain failure still owns its database."""
    source = installed(tmp_path_factory.mktemp('domain-predecessor')/'installed.db')
    snapshot = sqlite3.connect(':memory:')
    try:
        admit_regular_schema(source)
        from tests.prepared_fixture_support import coordinates
        assert not coordinates(source.conn), 'SQL-only predecessor retains external files'
        source.conn.backup(snapshot)
        snapshot.execute('PRAGMA query_only=ON')
        yield snapshot
    finally:
        snapshot.close()
        source.conn.close()


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


def test_installed_upgrade_preserves_values_sequences_dependencies_and_matches_fresh(tmp_path):
    store = installed(tmp_path/'upgrade.db')
    try:
        store.conn.execute('CREATE VIEW installed_credit AS SELECT entry_id,position FROM entry_artists')
        store.conn.execute('CREATE TABLE installed_receipts(position INTEGER)')
        store.conn.execute('CREATE TRIGGER installed_position AFTER UPDATE OF position ON entry_artists '
                           'BEGIN INSERT INTO installed_receipts VALUES (NEW.position); END')
        # Preserve deleted high IDs, including an empty autoincrement owner.
        store.conn.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name IN ('recordings','rec_content_pending')")
        store.conn.execute('DELETE FROM rec_musical_pending')
        store.conn.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name='rec_musical_pending'")
        store.conn.commit()
        before = admit_regular_schema(store)
        sql = dict(store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger')"))
        migrate(store.conn, registry=registry.MIGRATIONS, clock=lambda: 2000)
        store.init_schema()
        assert captured(store.conn)=={**before, 'schema_migrations': converted_history(before)}
        actual = dict(store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE type IN ('view','trigger')"))
        assert {name:actual[name] for name in sql}==sql
        # Subsequent migrations can add guards without rewriting installed programs.
        earlier = current._schema_contract(current.statements(version=103))
        added = {name for name,(kind,_) in current.contract().items()
                 if kind in ('view','trigger') and name not in earlier}
        assert actual.keys()-sql.keys()==added
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        with store.conn:
            store.conn.execute('UPDATE entry_artists SET position=1')
        assert tuple(store.conn.execute('SELECT * FROM installed_credit').fetchone())==(1,1)
        assert store.conn.execute('SELECT position FROM installed_receipts').fetchone()[0]==1
        with store.conn:
            rid = store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('new')").lastrowid
        assert rid>1000
        with store.conn:
            position = store.conn.execute("INSERT INTO rec_musical_pending(source_key,revision) VALUES ('after',1)").lastrowid
        assert position>1000
    finally:
        store.conn.close()


@pytest.mark.parametrize('table,column,bad',[
    ('recordings','provisional',7),('taste_modes','size',-1),
    ('discovery_memberships','offered_count',1.5),('entry_artists','position',.5),
    ('discovery_impressions','shown_at',float('inf')),
])
def test_invalid_predecessor_stops_with_exact_owner_and_retains_original_database(
        tmp_path,admitted_predecessor,table,column,bad):
    store = Store(tmp_path/'invalid.db')
    try:
        admitted_predecessor.backup(store.conn)
        with store.conn:
            store.conn.execute(f'UPDATE {table} SET {column}=?',(bad,))
        assert_invalid_upgrade_retains_database(store,table,column)
    finally:
        store.conn.close()


def assert_invalid_upgrade_retains_database(store,table,column):
    before = list(store.conn.iterdump())
    with pytest.raises(MigrationError,match=table+r'\.'+column+r'.*original rows retained'):
        migrate(store.conn, registry=registry.MIGRATIONS)
    retained = sqlite3.connect(store._db_path.as_uri()+'?mode=ro',uri=True)
    try:
        assert list(retained.iterdump())==before
    finally:
        retained.close()


def test_first_regular_admission_preserves_invalid_domain_before_failed_conversion(tmp_path):
    store = installed(tmp_path/'first-admission.db')
    try:
        with store.conn:
            store.conn.execute('UPDATE recordings SET provisional=7')
        # Keep the independent first-admission boundary covered with invalid
        # data. The five owner-specific failures above reuse only its setup.
        admit_regular_schema(store)
        assert_invalid_upgrade_retains_database(store,'recordings','provisional')
    finally:
        store.conn.close()


def test_interrupted_replacement_rolls_back_every_owner_then_restarts(tmp_path):
    store = installed(tmp_path/'interrupted.db')
    try:
        admitted = admit_regular_schema(store)
        before = list(store.conn.iterdump())
        def interrupted(conn,context):
            domains.apply(conn,context)
            raise RuntimeError('interrupted domain cutover')
        with pytest.raises(MigrationError,match='interrupted domain cutover'):
            migrate(store.conn,registry=(*registry.MIGRATIONS[:103],Migration(104,'domain_contracts',interrupted,rebuild_tables=True)))
        assert list(store.conn.iterdump())==before
        migrate(store.conn, registry=registry.MIGRATIONS, clock=lambda: 2000)
        store.init_schema()
        assert captured(store.conn)=={**admitted, 'schema_migrations': converted_history(admitted)}
        current.validate(store.conn)
    finally:
        store.conn.close()
