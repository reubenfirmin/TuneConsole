"""Genuine installed naming cutover, stable identifiers and external formats."""
from contextlib import ExitStack
import json
import sqlite3

import pytest

from yt_playlist.core import entity_names, genre_identity, schema_transforms
from yt_playlist.core.migrations import current, registry
from yt_playlist.core.migrations.v103_entity_names import transform
from yt_playlist.core.store import Store
from tests.prepared_fixture_support import capture_template
from tests.genre_catalog_support import bound_catalog


def rows(conn):
    result = {}
    for table, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('schema_migrations','schema_changes')"):
        result['accounts' if table=='identities' else table] = sorted(
            (tuple(row) for row in conn.execute('SELECT * FROM "'+table+'"')),key=repr)
    return result


@pytest.fixture(scope='module')
def names_predecessor_template(tmp_path_factory):
    root = tmp_path_factory.mktemp('names-predecessor')
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(root / 'app-home'))
        bound_catalog(patch, entries=(('jazz', 'Jazz'),))
        source = installed_predecessor(root / 'source.db')
        try:
            template = capture_template(source, None)
        except BaseException:
            source.conn.close()
            raise
    try:
        yield template
    finally:
        source.conn.close()


@pytest.fixture
def legacy_names(names_predecessor_template):
    with ExitStack() as resources:
        def restore(path):
            store = Store(path)
            resources.callback(store.conn.close)
            names_predecessor_template.restore(store)
            return store

        yield restore


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
    # The predecessor is one fixture, not a separate durable publication for
    # each of its hundreds of DDL statements. The tested cutover commits itself.
    store.conn.execute('BEGIN IMMEDIATE')
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


def test_name_predecessor_input_keeps_physical_ids_and_original_numbered_history(names_predecessor_template):
    source = names_predecessor_template.source
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 102
    assert tuple(source.conn.execute('SELECT id,label FROM identities').fetchone()) == (3, 'Main')
    assert tuple(source.conn.execute('SELECT id,scope_ref FROM playlist_membership_refs').fetchone()) == (55, '@account:3:LM')
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_predecessor_keeps_read_projections_and_physical_writers_after_regular_ddl(
        tmp_path, legacy_names):
    from yt_playlist.core import entity_names
    from yt_playlist.core.storage_maintenance import StorageMaintenance
    setup = legacy_names(tmp_path/'deferred.db')
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


def test_runtime_reconciles_installed_accounts_without_changing_wire_names(tmp_path,monkeypatch,legacy_names):
    from yt_playlist.core.runtime import Runtime
    from yt_playlist.core.setup import write_config
    setup = legacy_names(tmp_path/'runtime.db')
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


def test_backup_v2_still_encodes_account_and_remote_using_historical_fields(store,tmp_path,monkeypatch):
    from yt_playlist.library.playlist_backups import backup_remote_playlist
    from yt_playlist.core import paths
    account = store.accounts.upsert_account('Main','credential',None,True)
    path = backup_remote_playlist(store,account,'PL','Playlist',1,
                                 {'title':'Playlist','tracks':[],'trackCount':0})
    from yt_playlist.core import fao
    saved = json.loads(fao.read(path).bytes.decode())
    assert saved['snapshot_version']==2
    assert saved['identity_id']==account and saved['ytm_playlist_id']=='PL'
    assert 'account_id' not in saved and 'provider_playlist_id' not in saved
