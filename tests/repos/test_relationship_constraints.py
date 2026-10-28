import json
import sqlite3
from pathlib import Path

import pytest

from yt_playlist.core.migrations import Migration,MigrationError,migrate
from yt_playlist.core.migrations import registry,v025_relationships
from yt_playlist.core.store import Store


@pytest.fixture
def installed_v24(tmp_path):
    store = Store(tmp_path / 'installed.db')
    migrate(store.conn,registry=registry.MIGRATIONS[:24])
    store.conn.executescript((Path(__file__).parents[1] / 'fixtures/migrations/relationships_v24.sql').read_text())
    yield store
    store.conn.close()


def test_populated_upgrade_reconciles_order_and_retains_unknown_evidence(installed_v24):
    store = installed_v24
    store.init_schema()
    assert [(r['track_id'],r['position']) for r in store.conn.execute(
        'SELECT * FROM playlist_tracks WHERE playlist_id=1 ORDER BY position')]==[(3,0),(1,1),(2,2)]
    assert store.conn.execute('SELECT position FROM playlist_items WHERE playlist_id=2').fetchone()[0]==9
    assert store.playlists.get_playlist_groups()=={'PL':'Keep'}
    assert store.conn.execute('SELECT COUNT(*) FROM enrichment_log').fetchone()[0]==2
    assert store.conn.execute('SELECT input_video_id FROM enrichment_log WHERE id=3').fetchone()[0] is None
    conflict = store.conn.execute('SELECT * FROM enrichment_conflict WHERE track_id=1').fetchone()
    assert not conflict['resolved'] and conflict['resolved_value'] is None
    assert {v['value'] for v in json.loads(conflict['candidates'])}=={'house','rock'}
    missing = store.conn.execute('SELECT * FROM rec_mode_picks WHERE playlist_id=999').fetchone()
    assert missing['mode_id'] is None and missing['legacy_mode_id']==999
    assert store.modes.impression_counts()=={7:1}
    assert store.modes.pick_rows()==[(1,7)]
    kinds = {r[0] for r in store.conn.execute("SELECT DISTINCT kind FROM identity_state_archive WHERE kind LIKE 'constraint_legacy:%'")}
    assert {'constraint_legacy:enrichment_log','constraint_legacy:enrichment_conflict',
            'constraint_legacy:road_trip_drafts','constraint_legacy:playlist_items',
            'constraint_legacy:unresolved_playlist_items'}<=kinds
    orphan = store.conn.execute("SELECT payload FROM identity_state_archive WHERE kind='constraint_legacy:enrichment_log' AND source_id='2'").fetchone()
    assert json.loads(orphan[0])['value']=='orphan'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    before = list(store.conn.iterdump())
    store.init_schema()
    assert list(store.conn.iterdump())==before


def test_failed_upgrade_rolls_back_without_backups(installed_v24,tmp_path,monkeypatch):
    store = installed_v24
    before = list(store.conn.iterdump())
    current = registry.MIGRATIONS
    def fail(conn,context):
        v025_relationships.apply(conn,context)
        raise RuntimeError('interrupt relationship upgrade')
    monkeypatch.setattr(registry,'MIGRATIONS',(*current[:24],Migration(25,current[24].name,fail)))
    with pytest.raises(MigrationError,match='interrupt relationship upgrade'):
        migrate(store.conn)
    assert list(store.conn.iterdump())==before
    assert not (tmp_path / 'backups').exists()
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_non_numeric_legacy_positions_are_preserved_and_reconciled(installed_v24):
    store = installed_v24
    store.conn.execute("UPDATE playlist_items SET position='unknown' WHERE playlist_id=1 AND video_id='a'")
    store.conn.commit()
    store.init_schema()
    rows = store.conn.execute('SELECT track_id,position FROM playlist_tracks WHERE playlist_id=1 ORDER BY position').fetchall()
    assert [(r['track_id'],r['position']) for r in rows]==[(3,0),(2,1),(1,2)]
    archived = store.conn.execute("SELECT payload FROM identity_state_archive WHERE kind='constraint_legacy:playlist_items' AND source_id='1:a'").fetchone()
    assert json.loads(archived[0])['position']=='unknown'


@pytest.mark.parametrize('sql',[
    "INSERT INTO playlist_items VALUES (1,'b',0)",
    'INSERT INTO unresolved_playlist_items VALUES (1,3,0)',
    "UPDATE playlist_items SET position=0 WHERE playlist_id=1 AND video_id='a'",
    "UPDATE unresolved_playlist_items SET position=1 WHERE playlist_id=1",
    "UPDATE playlist_items SET position=-1 WHERE playlist_id=1",
    "UPDATE playlist_items SET position=1.5 WHERE playlist_id=1",
    "INSERT INTO playlist_items VALUES (999,'a',10)",
    "INSERT INTO enrichment_log(track_id,run_id,provider,field) VALUES (999,'r','p','genre')",
    "INSERT INTO rec_mode_picks(playlist_id,mode_id,created_at) VALUES (888,999,1)",
    "INSERT INTO rec_mode_picks(playlist_id,mode_id,created_at) VALUES (888,7,1)",
    "INSERT INTO rec_mode_impressions(epoch,lane,mode_id,created_at) VALUES (88,'a',999,1)",
    "INSERT INTO road_trip_drafts VALUES (888,'{}',1)",
    "UPDATE playlists SET scope_ref='OTHER' WHERE id=1",
    'UPDATE playlists SET scope_ref=NULL WHERE id=1',
])
def test_invalid_direct_writes_are_rejected(installed_v24,sql):
    store = installed_v24
    store.init_schema()
    with pytest.raises(sqlite3.IntegrityError),store.conn:
        store.conn.execute(sql)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_retained_history_survives_membership_removal_and_mode_refresh(installed_v24):
    store = installed_v24
    store.init_schema()
    store.history.record_play_event(1,'a|artist','a',100,playlist_ytm_id='PL')
    store.player_events.record_player_event(1,'track_exit','a',190,200,'PL',None,101)
    before = store.modes.playlist_engagement()[1]
    store.playlists.remove_playlist(1)
    store.conn.execute('DELETE FROM library_memberships WHERE entry_id=1')
    store.conn.commit()
    assert store.modes.playlist_engagement()[1]==before==dict(plays=1,last_play=100,skips=0,completions=1)
    assert store.conn.execute('SELECT COUNT(*) FROM enrichment_log WHERE track_id=1').fetchone()[0]==1
    assert store.playlists.get_playlist_groups()=={'PL':'Keep'}
    mode = store.modes.list_modes()[0]
    store.modes.replace_modes([mode],[],200)
    store.modes.replace_modes([],[7],300)
    assert store.modes.pick_rows()==[(1,7)]
    for sql in ('DELETE FROM catalog_entries WHERE id=1','DELETE FROM taste_modes WHERE mode_id=7',
                "DELETE FROM playlist_scopes WHERE ref='PL'"):
        with pytest.raises(sqlite3.IntegrityError),store.conn:
            store.conn.execute(sql)
    store.road_trip.delete_road_trip_recipe(1)
    assert not store.conn.execute('SELECT 1 FROM road_trip_drafts WHERE recipe_id=1').fetchone()


def test_indexes_support_the_affected_queries(store):
    queries = (
        ('SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position',(1,),'ix_playlist_item_position'),
        ('SELECT playlist_id FROM unresolved_playlist_items WHERE entry_id=?',(1,),'ix_unresolved_playlist_entry'),
        ('SELECT capture_id FROM listening_observations WHERE recording_id=? AND day>=?',(1,'2026-01-01'),'ix_listening_observation_recording_day'),
        ('SELECT id FROM listening_capture_receipts WHERE identity_id=? AND captured_at>=?',(1,1),'ix_listening_capture_account_time'),
        ('SELECT id FROM play_events WHERE identity_id=? AND playlist_ytm_id=? AND played_at>=?',(1,'PL',1),'ix_play_event_account_playlist_time'),
        ('SELECT recording_id FROM youtube_items WHERE video_id=?',('a',),'sqlite_autoindex_youtube_items_1'),
        ('SELECT recording_id FROM recording_identifiers WHERE scheme=? AND entity_type=? AND value=?',('musicbrainz','recording','id'),'ix_recording_identifier'),
        ('SELECT album_id FROM album_provider_ids WHERE provider=? AND provider_id=?',('youtube','id'),'sqlite_autoindex_album_provider_ids_1'),
    )
    for sql,args,index in queries:
        plan = ' '.join(r[3] for r in store.conn.execute('EXPLAIN QUERY PLAN '+sql,args))
        assert index in plan,(sql,plan)


def test_unknown_legacy_modes_cannot_become_feedback_for_a_new_mode(installed_v24):
    from tests.mode_fixtures import seed_modes
    store = installed_v24
    store.init_schema()
    seed_modes(store,999)
    assert store.modes.impression_counts()=={7:1}
    assert store.modes.pick_rows()==[(1,7)]
    assert all(r['mode_id']!=999 for r in store.modes.choice_learning_rows())


def test_account_scope_guard_rejects_another_accounts_system_playlist(store):
    a = store.identities.upsert_identity('a','c','brand',False)
    b = store.identities.upsert_identity('b','c',None,True)
    first = store.playlists.upsert_playlist(a,'LM','A',0,'h',1)
    second = store.playlists.upsert_playlist(b,'LM','B',0,'h',1)
    ref = store.playlists.get_playlist(second).scope_ref
    with pytest.raises(sqlite3.IntegrityError),store.conn:
        store.conn.execute('UPDATE playlists SET scope_ref=? WHERE id=?',(ref,first))


def test_deleted_playlist_ids_never_attach_old_feedback_to_new_memberships(store):
    from tests.mode_fixtures import seed_modes
    seed_modes(store,1,2)
    owner = store.identities.upsert_identity('Main','c',None,True)
    first = store.playlists.upsert_playlist(owner,'FIRST','First',0,'h',1)
    store.modes.log_pick(first,1,10)
    store.playlists.remove_playlist(first)
    second = store.playlists.upsert_playlist(owner,'SECOND','Second',0,'h',20)
    assert second!=first
    store.modes.log_pick(second,2,30)
    store.history.record_play_event(owner,'song|artist','video',40,playlist_ytm_id='SECOND')
    stats = store.modes.playlist_engagement()
    assert stats[first]['plays']==0 and stats[second]['plays']==1
    assert store.modes.pick_rows()==[(first,1),(second,2)]
    with pytest.raises(sqlite3.IntegrityError),store.conn:
        store.conn.execute('UPDATE playlist_membership_refs SET scope_ref=? WHERE id=?',('SECOND',first))


def test_implicit_rowid_allocation_cannot_retarget_a_retained_membership(store):
    from yt_playlist.core.playlist_scopes import ensure
    owner = store.identities.upsert_identity('Main','c',None,True)
    first = store.playlists.upsert_playlist(owner,'FIRST','First',0,'h',1)
    store.playlists.remove_playlist(first)
    ensure(store.conn,'NEXT',owner)
    store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError),store.conn:
        store.conn.execute("INSERT INTO playlists(identity_id,ytm_playlist_id,title,scope_ref) VALUES (?,'NEXT','Next','NEXT')",(owner,))
    second = store.playlists.upsert_playlist(owner,'NEXT','Next',0,'h',2)
    assert second>first
