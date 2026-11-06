"""Genuine installed naming cutover, stable identifiers and external formats."""
import json
import sqlite3

import pytest

from yt_playlist.core import entity_names, genre_identity, playlist_scopes, schema_transforms
from yt_playlist.core.migrations import current, registry, migrate, Migration, MigrationError
from yt_playlist.core.migrations import v103_entity_names
from yt_playlist.core.migrations.v103_entity_names import COLUMNS, transform
from yt_playlist.core.store import Store
from tests.regular_schema_support import admit_regular_schema


def rows(conn):
    result = {}
    for table, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('schema_migrations','schema_changes')"):
        result['accounts' if table=='identities' else table] = sorted(
            (tuple(row) for row in conn.execute('SELECT * FROM "'+table+'"')),key=repr)
    return result


def test_installed_names_upgrade_preserves_all_rows_ids_and_matches_fresh(tmp_path):
    store = Store(tmp_path/'installed.db')
    try:
        for sql in current.statements(version=102):
            store.conn.execute(sql)
        genre_identity.seed_catalog(store.conn)
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
        store.conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                              [(m.version,m.name) for m in registry.MIGRATIONS[:102]])
        store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (3,'Main','credential',1),(4,'Other','credential',0)")
        store.conn.execute("INSERT INTO playlist_scopes(ref,provider_id,identity_id,kind) VALUES ('@account:3:LM','LM',3,'account'),('PL','PL',NULL,'shared')")
        store.conn.execute("INSERT INTO playlist_membership_refs(id,scope_ref,identity_id) VALUES (55,'@account:3:LM',3),(100,'PL',4)")
        store.conn.execute("INSERT INTO playlists(id,identity_id,ytm_playlist_id,scope_ref,title) VALUES (55,3,'LM','@account:3:LM','Liked Music')")
        store.conn.execute("INSERT INTO cleanup_ignored VALUES ('PL','empty',1)")
        store.conn.execute("INSERT INTO suppressed_overlaps VALUES ('@account:3:LM','PL',2)")
        store.conn.execute("INSERT INTO overlap_kept VALUES ('@account:3:LM','PL',3)")
        store.conn.commit()
        before = rows(store.conn)
        vibes = {row[0]:row[1] for row in store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE name IN ('sample_attributes','measurements','measurement_samples','predictions','prediction_artists','prediction_inputs','recording_vibes')")}
        migrate(store.conn, registry=registry.MIGRATIONS)
        # The name-only historical transaction preserves the complete installed
        # population. Regular startup then adds separately owned progress rows.
        assert rows(store.conn)==before
        admit_regular_schema(store)
        store.init_schema()
        after = rows(store.conn)
        assert {owner:after[owner] for owner in before}==before
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        assert {row[0]:row[1] for row in store.conn.execute("SELECT name,sql FROM sqlite_schema WHERE name IN ('sample_attributes','measurements','measurement_samples','predictions','prediction_artists','prediction_inputs','recording_vibes')")}==vibes
        for table,mapping in COLUMNS.items():
            columns = {row[1] for row in store.conn.execute('PRAGMA table_info('+table+')')}
            assert not columns & mapping.keys()
            assert set(mapping.values()) <= columns
        assert store.accounts.get_master_account().id==3
        assert store.playlists.get_playlist(55).playlist_scope_id=='@account:3:LM'
        assert playlist_scopes.resolve(store.conn,'LM',3)=='@account:3:LM'
        assert store.playlists.upsert_playlist(4,'PL','Recreated',0,'hash',1)>100
        assert tuple(store.conn.execute('SELECT playlist_scope_id,account_id FROM playlist_membership_refs WHERE id=100').fetchone())==('PL',4)
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='identities'").fetchone()
        assert any(row[2]=='accounts' and row[3]=='account_id' for row in store.conn.execute('PRAGMA foreign_key_list(playlists)'))
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            store.conn.execute("UPDATE playlist_scopes SET account_id=4 WHERE id='@account:3:LM'")
        store.conn.rollback()
        with pytest.raises(sqlite3.IntegrityError,match='account'):
            store.conn.execute("INSERT INTO playlist_membership_refs(playlist_scope_id,account_id) VALUES ('@account:3:LM',4)")
        store.conn.rollback()
    finally:
        store.conn.close()


@pytest.mark.parametrize('transform', [transform, entity_names.predecessor_definition,
                                     schema_transforms.entity_names])
def test_name_transform_keeps_literal_payloads_and_unrelated_provider_ids(transform):
    assert transform("CREATE VIEW names AS SELECT 'identity_id''s',identity_id FROM identities -- identity_id\n")==(
        "CREATE VIEW names AS SELECT 'identity_id''s',account_id FROM accounts -- identity_id\n")
    assert transform('CREATE TABLE "playlist_scopes" ([ref] TEXT,`provider_id` TEXT,"IDENTITY_ID" INTEGER)')==(
        'CREATE TABLE "playlist_scopes" ([id] TEXT,`provider_playlist_id` TEXT,"account_id" INTEGER)')
    assert transform('CREATE INDEX ix_playlist_scope ON playlists(scope_ref,ytm_playlist_id)')==(
        'CREATE INDEX ix_playlist_scope_account ON playlists(playlist_scope_id,provider_playlist_id)')
    assert transform('CREATE TABLE ignored_merge_members(playlist_ref TEXT)')==(
        'CREATE TABLE ignored_merge_members(playlist_scope_id TEXT)')
    for table in ('cleanup_ignored','quarantined_playlists'):
        assert transform(f'CREATE TABLE {table}(ytm TEXT)')==f'CREATE TABLE {table}(playlist_scope_id TEXT)'
    for table in ('suppressed_overlaps','overlap_kept'):
        assert transform(f'CREATE TABLE {table}(a TEXT,b TEXT)')==(
            f'CREATE TABLE {table}(first_playlist_scope_id TEXT,second_playlist_scope_id TEXT)')
    assert transform('CREATE TABLE rec_mode_picks(playlist_id INTEGER)')==(
        'CREATE TABLE rec_mode_picks(membership_ref_id INTEGER)')
    assert transform('CREATE TABLE artist_provider_ids(provider_id TEXT)')=='CREATE TABLE artist_provider_ids(provider_id TEXT)'
    assert transform('CREATE TABLE playlist_items(playlist_id INTEGER REFERENCES playlists(id))')=='CREATE TABLE playlist_items(playlist_id INTEGER REFERENCES playlists(id))'
    assert transform("CREATE TABLE providers(provider_id TEXT DEFAULT 'playlist_scopes') /* playlist_scopes */")==(
        "CREATE TABLE providers(provider_id TEXT DEFAULT 'playlist_scopes') /* playlist_scopes */")


def test_pretyped_album_context_keeps_historical_document_and_current_account_field():
    from yt_playlist.core import album_playback_context
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        value = {'playlist':'PL','account_id':3}
        album_playback_context.put(conn,value)
        assert json.loads(conn.execute('SELECT value FROM settings').fetchone()[0])=={'playlist':'PL','identity_id':3}
        assert album_playback_context.load(conn)==value
        album_playback_context.put(conn,None)
        assert album_playback_context.load(conn) is None
    finally:
        conn.close()


def installed_predecessor(path):
    store = Store(path)
    for sql in current.statements(version=102):
        store.conn.execute(sql)
    genre_identity.seed_catalog(store.conn)
    store.conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
    store.conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                          [(m.version,m.name) for m in registry.MIGRATIONS[:102]])
    store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (3,'Main','credential',1)")
    store.conn.execute("INSERT INTO playlist_scopes(ref,provider_id,identity_id,kind) VALUES ('@account:3:LM','LM',3,'account')")
    store.conn.execute("INSERT INTO playlist_membership_refs(id,scope_ref,identity_id) VALUES (55,'@account:3:LM',3)")
    store.conn.execute("INSERT INTO playlists(id,identity_id,ytm_playlist_id,scope_ref,title) VALUES (55,3,'LM','@account:3:LM','Liked Music')")
    store.conn.commit()
    return store


def test_interrupted_rename_rolls_back_schema_rows_and_history(tmp_path):
    store = installed_predecessor(tmp_path/'interrupted.db')
    try:
        before = list(store.conn.iterdump())
        def interrupted(conn,context):
            v103_entity_names.apply(conn,context)
            raise RuntimeError('interrupted naming cutover')
        with pytest.raises(MigrationError,match='interrupted naming cutover'):
            migrate(store.conn,registry=(*registry.MIGRATIONS[:102],Migration(103,'entity_names',interrupted)))
        assert list(store.conn.iterdump())==before
        migrate(store.conn, registry=registry.MIGRATIONS)
        store.init_schema()
        current.validate(store.conn)
    finally:
        store.conn.close()


def test_predecessor_keeps_read_projections_and_physical_writers_after_regular_ddl(tmp_path):
    from yt_playlist.core import entity_names
    from yt_playlist.core.storage_maintenance import StorageMaintenance
    setup = installed_predecessor(tmp_path/'deferred.db')
    setup.conn.execute('PRAGMA journal_mode=WAL')
    setup.conn.close()
    store = Store(tmp_path/'deferred.db')
    maintenance = StorageMaintenance(store)
    try:
        store.init_schema(defer_metadata=True)
        assert store._schema_compatibility and not store.activity_paused
        assert store.accounts.get_master_account().id==3
        assert store.playlists.get_playlist(55).playlist_scope_id=='@account:3:LM'
        with store.read_view() as reader:
            assert reader.accounts.get_master_account().id==3
            assert reader.playlists.get_playlist(55).account_id==3
            with pytest.raises(sqlite3.OperationalError):
                reader.conn.execute("UPDATE accounts SET label='Hidden write'")
        with pytest.raises(sqlite3.OperationalError,match='view'):
            store.conn.execute("UPDATE accounts SET label='Hidden write'")
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==102
        maintenance.start()
        maintenance.join(30)
        assert not maintenance.active and not store._schema_recovery_pending
        assert not store.activity_paused
        assert getattr(store.conn,'_entity_read_views',())
        assert store.conn.execute("SELECT 1 FROM sqlite_temp_schema WHERE name='accounts'").fetchone()
        assert entity_names.accounts(store.conn)=='accounts'
        assert entity_names.owner(store.conn, 'accounts').table=='main.identities'
        assert store.playlists.get_playlist(55).provider_playlist_id=='LM'
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==102
        assert store.accounts.upsert_account('Second','other',None,False)>3
    finally:
        maintenance.stop()
        maintenance.join(10)
        store.conn.close()


def test_runtime_reconciles_installed_accounts_without_changing_wire_names(tmp_path,monkeypatch):
    from yt_playlist.core.runtime import Runtime
    from yt_playlist.core.setup import write_config
    setup = installed_predecessor(tmp_path/'runtime.db')
    setup.conn.close()
    config = tmp_path/'config.toml'
    write_config([{'id':3,'label':'Main','credential_ref':'credential','is_master':True}],config)
    provider = object()
    monkeypatch.setattr('yt_playlist.core.runtime.build_client',lambda cfg,bridge:provider)
    store = Store(tmp_path/'runtime.db')
    try:
        store.init_schema(defer_metadata=True)
        runtime = Runtime(store,config,tmp_path)
        runtime.load()
        assert runtime.configured and runtime.clients()=={3:provider}
        assert store.accounts.get_master_account().id==3
        assert '[[identity]]' in config.read_text()
    finally:
        store.conn.close()


def test_installed_custom_sql_moves_with_its_owners_without_losing_literals(tmp_path):
    store = installed_predecessor(tmp_path/'custom.db')
    try:
        store.conn.execute('CREATE TABLE installed_receipts(account_id INTEGER REFERENCES identities(id),message TEXT)')
        store.conn.execute("INSERT INTO installed_receipts VALUES (3,'identity_id')")
        store.conn.execute('CREATE VIEW installed_accounts AS SELECT id,label FROM identities')
        store.conn.execute("CREATE TRIGGER installed_label AFTER UPDATE OF label ON identities "
                           "BEGIN UPDATE installed_receipts SET message=NEW.label WHERE account_id=NEW.id; END")
        store.conn.commit()
        migrate(store.conn, registry=registry.MIGRATIONS)
        store.init_schema()
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM installed_accounts')]==[(3,'Main')]
        assert store.conn.execute('SELECT message FROM installed_receipts').fetchone()[0]=='identity_id'
        with store.conn:
            store.conn.execute("UPDATE accounts SET label='Renamed' WHERE id=3")
        assert store.conn.execute('SELECT message FROM installed_receipts').fetchone()[0]=='Renamed'
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


def test_backup_v2_still_encodes_account_and_remote_using_historical_fields(store,tmp_path,monkeypatch):
    from yt_playlist.library.playlist_backups import backup_remote_playlist
    from yt_playlist.core import paths
    account = store.accounts.upsert_account('Main','credential',None,True)
    path = backup_remote_playlist(store,account,'PL','Playlist',1,
                                 {'title':'Playlist','tracks':[],'trackCount':0})
    from yt_playlist.core import fao
    saved = json.loads(fao.read_text(path))
    assert saved['snapshot_version']==2
    assert saved['identity_id']==account and saved['ytm_playlist_id']=='PL'
    assert 'account_id' not in saved and 'provider_playlist_id' not in saved
