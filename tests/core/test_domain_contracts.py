"""Direct SQL constraints and input validation before writing."""
import sqlite3

import pytest

from yt_playlist.core import domain_contracts as domains
from yt_playlist.core.store import Store
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import use_native_sql_calls, without_query_timing


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(scope='module', autouse=True)
def domain_catalog():
    # SQL domain checks only need a small genre catalog.
    with pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        use_native_sql_calls(patch)
        yield


@pytest.fixture
def disable_periodic_library_sync():
    # Pure SQL owners never construct the web app or start periodic sync.
    pass


def seed(conn):
    statements = (
        "INSERT INTO accounts(id,label,is_master) VALUES (1,'Main',1)",
        "INSERT INTO artists(id,name,provisional) VALUES (1,'Artist',0)",
        "INSERT INTO albums(id,title,metadata_rank) VALUES (1,'Album',0)",
        "INSERT INTO recordings(id,identity_key) VALUES (1,'recording')",
        "INSERT INTO track_sources(id,recording_id,video_id,available) VALUES (1,1,'video',1)",
        "INSERT INTO track_sources(id,recording_id,source_ref) VALUES (2,1,'unknown')",
        "INSERT INTO entry_artists VALUES (1,0,1,'Artist','test')",
        "INSERT INTO album_artists VALUES (1,0,1,'Artist')",
        "INSERT INTO playlist_scopes(id,provider_playlist_id,kind) VALUES ('PL','PL','shared')",
        "INSERT INTO playlists(id,account_id,playlist_scope_id,track_count) VALUES (1,1,'PL',0)",
        "INSERT INTO taste_groups(external_key,legacy_mode_id,created_at,mode_label,mode_families,mode_centroid,"
        "mode_size,mode_rep_keys,mode_active,mode_first_seen,mode_last_seen,mode_space) "
        "VALUES ('legacy:1',1,-1,'Mode','[]',X'',0,'[]',1,-1,0,'')",
        "UPDATE recordings SET discovery_entry_id=1 WHERE id=1",
        "UPDATE albums SET discovery_found_at=-1 WHERE id=1",
        "UPDATE artists SET discovery_candidate=1 WHERE id=1",
        "UPDATE recordings SET discovery_views=json_object('view',-1) WHERE id=1",
        "INSERT INTO rec_response_events(event_id,kind,identity_key,at) VALUES ('event','historical_kind','recording',-1)",
        "INSERT INTO rec_served_impressions(request_id,surface,lane,identity_key,rank,model_version,provenance,served_at) VALUES ('request','home','lane','recording',0,'model','{}',-1)",
        "INSERT INTO rec_musical_sources(source_key,provider,scope,entity_id) VALUES ('test','test','recording','recording')",
        "INSERT OR IGNORE INTO rec_vibe_pending(group_key) VALUES ('*')",
    )
    for sql in statements:
        conn.execute(sql)
    conn.commit()


@pytest.fixture(scope='module')
def constrained(tmp_path_factory, _pristine_current_schema):
    store = Store(tmp_path_factory.mktemp('domains')/'current.db')
    try:
        store.conn.execute('PRAGMA synchronous=OFF')
        _pristine_current_schema.backup(store.conn)
        store.init_schema()
        seed(store.conn)
        yield store.conn
    finally:
        store.conn.close()


CURRENT_CONTRACTS = {table: fields.copy() for table, fields in domains.CONTRACTS.items()}
for owner, previous in (('recordings', 'discovery_memberships'), ('artists', 'discovered_artists')):
    CURRENT_CONTRACTS.setdefault(owner, {}).update(
        (f'discovery_{column}', expression.replace(column, f'discovery_{column}'))
        for column, expression in CURRENT_CONTRACTS.pop(previous).items())
CURRENT_CONTRACTS.pop('unresolved_discovery_impressions', None)
CURRENT_CONTRACTS.pop('discovery_impressions')
CURRENT_CONTRACTS.pop('rec_content_pending')
CURRENT_CONTRACTS['taste_groups'] = {
    f'mode_{column}': expression.replace(column, f'mode_{column}')
    for column, expression in CURRENT_CONTRACTS.pop('taste_modes').items()
}
old_musical_queue = CURRENT_CONTRACTS.pop('rec_musical_pending')
CURRENT_CONTRACTS['rec_musical_sources'] = {
    'revision': old_musical_queue['revision'],
    **{f'pending_{column}': old_musical_queue[column].replace(column, f'pending_{column}')
       for column in ('position', 'cursor')},
}

INVALID = []
for _table,_columns in CURRENT_CONTRACTS.items():
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
        for table,fields in CURRENT_CONTRACTS.items():
            for column,expression in fields.items():
                if (table, column) == ('rec_vibe_pending', 'position'):
                    # Only the singleton maintenance row can occupy position zero.
                    assert conn.execute("UPDATE rec_vibe_pending SET position=0 WHERE group_key='*maintenance'").rowcount == 1
                    continue
                value = -1 if 'abs(' in expression else 1 if '>=1' in expression else 0
                conn.execute(f'UPDATE {table} SET {column}=?',(value,))
                if 'IS NULL OR' in expression:
                    conn.execute(f'UPDATE {table} SET {column}=NULL')
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
            conn.execute("INSERT INTO accounts(label,is_master) VALUES ('Other',1)")
        conn.execute('UPDATE accounts SET is_master=0')
        conn.execute("INSERT INTO accounts(label,is_master) VALUES ('Other',1)")
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


def captured(conn):
    return {name: sorted((tuple(row) for row in conn.execute('SELECT * FROM "'+name+'"')), key=repr)
            for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}


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
