import json
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from yt_playlist.core.accounts import client_for
from yt_playlist.core.migrations import Migration,MigrationError,migrate
from yt_playlist.core.migrations import registry,v024_account_scope,v030_account_scope_preservation
from yt_playlist.core.store import Store


@pytest.fixture(scope='module')
def account_predecessors(tmp_path_factory):
    directory = tmp_path_factory.mktemp('account-predecessors')
    paths = {version: directory / f'v{version}.db' for version in (23, 29)}
    source = Store(paths[23])
    with closing(source.conn):
        migrate(source.conn, registry=registry.MIGRATIONS[:23])
        source.conn.executescript((Path(__file__).parents[1] /
                                  'fixtures/migrations/accounts_v23.sql').read_text())
        with closing(sqlite3.connect(paths[29])) as target:
            source.conn.backup(target)
    source = Store(paths[29])
    with closing(source.conn):
        migrate(source.conn, registry=registry.MIGRATIONS[:29])
    return paths


def _installed(tmp_path, account_predecessors, version):
    store = Store(tmp_path / 'installed.db')
    try:
        with closing(sqlite3.connect(account_predecessors[version])) as source:
            source.backup(store.conn)
        yield store
    finally:
        store.conn.close()


@pytest.fixture
def installed_v23(tmp_path, account_predecessors):
    yield from _installed(tmp_path, account_predecessors, 23)


@pytest.fixture
def installed_v29(tmp_path, account_predecessors):
    yield from _installed(tmp_path, account_predecessors, 29)


def test_account_scope_repair_preserves_existing_account_override(installed_v29):
    store = installed_v29
    with store.conn:
        store.conn.execute("INSERT INTO playlist_group VALUES ('@account:1:LM','Already changed')")
    # The two installed-version cases above retain the complete upgrade and
    # current repository workflow. This branch owns the v30 repair itself.
    migrate(store.conn, registry=registry.MIGRATIONS[:30])
    assert dict(store.conn.execute('SELECT ytm,name FROM playlist_group'))=={
        '@account:1:LM':'Already changed','@account:2:LM':'Old likes','shared':'Keep'}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_repair_does_not_restore_albums_already_resolved_by_every_original_account(installed_v29):
    store = installed_v29
    with store.conn:
        store.conn.execute("INSERT INTO albums(id,title,metadata_rank) VALUES (1,'Removed',2)")
        store.conn.execute("INSERT INTO album_provider_ids VALUES ('youtube','removed',1)")
        store.conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
                           ('album_legacy:saved_albums','removed','{"browse_id":"removed"}','original album'))
        store.conn.executemany('INSERT INTO album_library_snapshots VALUES (?,1)', [(1,),(2,)])
    migrate(store.conn, registry=registry.MIGRATIONS[:30])
    assert store.conn.execute('SELECT * FROM album_library_memberships').fetchall()==[]
    assert list(map(tuple, store.conn.execute(
        'SELECT identity_id,observed_at FROM album_library_legacy_cohort ORDER BY identity_id')))==[(1,1),(2,1)]
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_account_scope_repair_failure_keeps_installed_v29_unchanged(installed_v29,tmp_path,monkeypatch):
    store = installed_v29
    before = list(store.conn.iterdump())
    current = registry.MIGRATIONS
    def fail(conn,context):
        v030_account_scope_preservation.apply(conn,context)
        raise RuntimeError('interrupt preservation repair')
    monkeypatch.setattr(registry,'MIGRATIONS',(*current[:29],Migration(30,current[29].name,fail)))
    with pytest.raises(MigrationError,match='interrupt preservation repair'):
        migrate(store.conn, registry=registry.MIGRATIONS)
    assert list(store.conn.iterdump())==before
    assert not (tmp_path / 'backups').exists()
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    monkeypatch.setattr(registry,'MIGRATIONS',current)
    migrate(store.conn, registry=current[:30])
    assert store.conn.execute("SELECT name FROM playlist_group WHERE ytm='@account:2:LM'").fetchone()[0]=='Old likes'
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==30
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_scope_upgrade_failure_rolls_back_without_backups(installed_v23,tmp_path,monkeypatch):
    store = installed_v23
    before = list(store.conn.iterdump())
    current = registry.MIGRATIONS
    def fail(conn,context):
        v024_account_scope.apply(conn,context)
        raise RuntimeError('interrupt account migration')
    monkeypatch.setattr(registry,'MIGRATIONS',(*current[:23],Migration(24,current[23].name,fail)))
    with pytest.raises(MigrationError,match='interrupt account migration'):
        migrate(store.conn, registry=registry.MIGRATIONS)
    assert list(store.conn.iterdump())==before
    monkeypatch.setattr(registry,'MIGRATIONS',current)
    migrate(store.conn, registry=current[:30])
    assert list(map(tuple, store.conn.execute('SELECT id,scope_ref FROM playlists ORDER BY id')))==[
        (1,'@account:1:LM'),(2,'@account:2:LM'),(3,'@account:1:SE'),(4,'shared'),(5,'shared')]
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==30
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    assert not (tmp_path / 'backups').exists()
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_system_curation_is_per_account_and_survives_membership_removal(store):
    first = store.accounts.upsert_account('first','c','brand',False)
    main = store.accounts.upsert_account('main','c',None,True)
    a = store.playlists.upsert_playlist(first,'LM','First likes',0,'h',0)
    b = store.playlists.upsert_playlist(main,'LM','Main likes',0,'h',0)
    ar,br = store.playlists.get_playlist(a).playlist_scope_id,store.playlists.get_playlist(b).playlist_scope_id
    store.playlists.hide_playlist('LM',account_id=first)
    store.playlists.set_playlist_group('LM','Personal',account_id=main)
    store.overlaps.ignore_cleanup(ar,'empty',1)
    store.overlaps.ignore_overlap_playlist(br,1)
    store.playlist_recipes.set_recipe(ar,{'account':'first'},1)
    store.playlists.remove_playlist(a)
    assert store.playlists.get_hidden_playlists()=={ar}
    assert store.playlists.get_playlist_groups()=={br:'Personal'}
    assert store.overlaps.get_cleanup_ignored()=={'empty':{ar}}
    restored = store.playlists.upsert_playlist(first,'LM','First likes again',0,'h',2)
    assert store.playlists.get_playlist(restored).playlist_scope_id==ar
    assert store.playlist_recipes.get_recipe(ar)=={'account':'first'}
    with pytest.raises(ValueError,match='Choose an account'):
        store.playlists.hide_playlist('LM')


def test_shared_curation_survives_each_account_membership_removal(store):
    first = store.accounts.upsert_account('first','c','brand',False)
    main = store.accounts.upsert_account('main','c',None,True)
    ids = [store.playlists.upsert_playlist(i,'shared','Shared',0,'h',0) for i in (first,main)]
    store.playlists.set_playlist_group('shared','Keep')
    store.overlaps.suppress_overlap('shared','other',1)
    store.overlaps.ignore_merge('other|shared',['shared','other'],1)
    for pid in ids:
        store.playlists.remove_playlist(pid)
        assert store.playlists.get_playlist_groups()=={'shared':'Keep'}
        assert store.overlaps.get_suppressed_overlap_pairs()=={frozenset(('shared','other'))}
        assert store.overlaps.get_ignored_merges()[0]['members']==['other','shared']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_saved_snapshots_and_unlikes_retain_other_accounts_and_unknown_legacy(store):
    a = store.accounts.upsert_account('first','c','brand',False)
    b = store.accounts.upsert_account('main','c',None,True)
    album = lambda bid:dict(browse=bid,title='Same',artist='Artist')
    store.collection.add_saved_album(album('unknown'))
    store.collection.replace_saved_album_snapshots({a:[album('shared'),album('a')]},now=1)
    assert store.collection.saved_album_ids()=={'unknown','shared','a'}
    store.collection.replace_saved_album_snapshots({b:[album('shared'),album('b')]},now=2)
    assert store.collection.saved_album_ids()=={'shared','a','b'}
    store.collection.remove_saved_album('shared',account_id=b)
    assert store.collection.saved_album_ids()=={'shared','a','b'}
    store.collection.replace_saved_album_snapshots({a:[]},now=3)
    assert store.collection.saved_album_ids()=={'b'}
    assert store.conn.execute('SELECT account_id FROM saved_album_memberships').fetchone()[0]==b


def test_account_selection_uses_main_explicit_or_owner_and_never_falls_back(store):
    a = store.accounts.upsert_account('first','c','brand',False)
    b = store.accounts.upsert_account('main','c',None,True)
    other,main = object(),object()
    clients = {a:other,b:main}
    assert client_for(store,clients)==(b,main)
    assert client_for(store,clients,account_id=a)==(a,other)
    playlist = store.playlists.upsert_playlist(a,'P','Other',0,'h',0)
    assert client_for(store,clients,playlist_id=playlist)==(a,other)
    with pytest.raises(ValueError,match='not connected'):
        client_for(store,{a:other})
    with pytest.raises(ValueError,match='another account'):
        client_for(store,clients,playlist_id=playlist,account_id=b)


def test_generated_system_context_does_not_quarantine_another_accounts_likes(store):
    a = store.accounts.upsert_account('first','c','brand',False)
    b = store.accounts.upsert_account('main','c',None,True)
    for iid in (a,b):
        store.playlists.upsert_playlist(iid,'LM','Likes',0,'h',1)
    store.playlists.set_playlist_provenance('LM',account_id=a,origin='tuneconsole',adopted=False)
    for iid in (a,b):
        store.history.record_play_event(iid,'song|artist',None,10,playlist_ytm_id='LM',source_timezone='UTC')
    assert [r[0] for r in store.conn.execute('SELECT is_generated FROM play_events ORDER BY id')]==[1,0]
    assert store.trends.ledger_track_plays(0,20)=={'song|artist':1}
    # Recap ranges use calendar day boundaries, including the day's early events.
    assert [r['account_id'] for r in store.recap.snapshot(0,86400)['plays']]==[b]
