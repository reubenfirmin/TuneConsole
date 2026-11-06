import json
from contextlib import closing
import sqlite3
from pathlib import Path

import pytest

from yt_playlist.core.migrations import Migration,MigrationError,migrate
from yt_playlist.core.migrations import registry,v025_relationships
from yt_playlist.core.store import Store
from yt_playlist.core import legacy_evidence
from tests.prepared_fixture_support import historical_template


def upgrade_relationship_fixture(store):
    # These tests exercise a genuine v24 predecessor. Ordinary admission only
    # accepts supported installed layouts; historical replay stays explicit.
    migrate(store.conn, registry=registry.MIGRATIONS)
    store.init_schema()


@pytest.fixture(scope='module')
def v24_template(tmp_path_factory):
    directory = tmp_path_factory.mktemp('relationships-v24')
    path = directory / 'pristine.db'
    store = Store(path)
    try:
        migrate(store.conn,registry=registry.MIGRATIONS[:24])
        store.conn.executescript((Path(__file__).parents[1] / 'fixtures/migrations/relationships_v24.sql').read_text())
    finally:
        store.conn.close()
    return path


@pytest.fixture
def installed_v24(tmp_path, v24_template):
    store = Store(tmp_path / 'installed.db')
    with closing(sqlite3.connect(v24_template)) as template:
        template.backup(store.conn)
    try:
        yield store
    finally:
        store.conn.close()


@pytest.fixture(scope='module')
def upgraded_relationship_template(v24_template):
    # Build the actual populated upgrade once. Current repository behavior does
    # not need to replay the same historical conversions for every assertion.
    with closing(sqlite3.connect(v24_template)) as predecessor:
        with historical_template(predecessor, upgrade_relationship_fixture,
                                 database=':memory:') as template:
            yield template


@pytest.fixture
def upgraded_relationships(store, upgraded_relationship_template):
    upgraded_relationship_template.restore(store)
    return store


def test_populated_upgrade_reconciles_order_and_retains_unknown_evidence(upgraded_relationships):
    store = upgraded_relationships
    assert [(r['track_id'],r['position']) for r in store.conn.execute(
        'SELECT * FROM playlist_tracks WHERE playlist_id=1 ORDER BY position')]==[(3,0),(1,1),(2,2)]
    assert store.conn.execute('SELECT position FROM playlist_items WHERE playlist_id=2').fetchone()[0]==9
    assert store.playlists.get_playlist_groups()=={'PL':'Keep'}
    assert store.conn.execute('SELECT COUNT(*) FROM enrichment_log').fetchone()[0]==2
    # Later evidence owners retain the finding but allocate their own row IDs.
    assert store.conn.execute("SELECT input_video_id FROM enrichment_log WHERE track_id=2 "
                              "AND provider='lastfm' AND field='genre' AND value='rock'").fetchone()[0] is None
    conflict = store.conn.execute('SELECT * FROM enrichment_conflict WHERE track_id=1').fetchone()
    assert not conflict['resolved'] and conflict['resolved_value'] is None
    assert {v['value'] for v in json.loads(conflict['candidates'])}=={'house','rock'}
    missing = store.conn.execute('SELECT * FROM rec_mode_picks WHERE membership_ref_id=999').fetchone()
    assert missing['mode_id'] is None and missing['legacy_mode_id']==999
    assert store.modes.impression_counts()=={7:1}
    assert store.modes.pick_counts()=={7:1}
    kinds = {r[0] for table in ('metadata_legacy_evidence', 'workflow_legacy_evidence', 'playlist_legacy_evidence')
             for r in store.conn.execute(f"SELECT DISTINCT kind FROM {table} WHERE kind LIKE 'constraint_legacy:%'")}
    assert {'constraint_legacy:enrichment_log','constraint_legacy:enrichment_conflict',
            'constraint_legacy:road_trip_drafts','constraint_legacy:playlist_items',
            'constraint_legacy:unresolved_playlist_items'}<=kinds
    orphan = legacy_evidence.original(store.conn, 'metadata', 'constraint_legacy:enrichment_log', '2')
    assert json.loads(orphan)['value']=='orphan'
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
        migrate(store.conn, registry=registry.MIGRATIONS)
    assert list(store.conn.iterdump())==before
    assert not (tmp_path / 'backups').exists()
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_non_numeric_legacy_positions_are_preserved_and_reconciled(installed_v24):
    store = installed_v24
    store.conn.execute("UPDATE playlist_items SET position='unknown' WHERE playlist_id=1 AND video_id='a'")
    store.conn.commit()
    upgrade_relationship_fixture(store)
    rows = store.conn.execute('SELECT track_id,position FROM playlist_tracks WHERE playlist_id=1 ORDER BY position').fetchall()
    assert [(r['track_id'],r['position']) for r in rows]==[(3,0),(2,1),(1,2)]
    archived = legacy_evidence.original(store.conn, 'playlist', 'constraint_legacy:playlist_items', '1:a')
    assert json.loads(archived)['position']=='unknown'


def test_invalid_direct_writes_are_rejected(installed_v24):
    store = installed_v24
    # Test the constraints on their genuine v25 owners. Later migrations retire
    # some of these write interfaces; replaying those unrelated conversions
    # would turn a foreign-key assertion into a read-only-view error.
    migrate(store.conn, registry=registry.MIGRATIONS[:25])
    accepted = []
    for sql in (
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
    ):
        try:
            store.conn.execute(sql)
        except sqlite3.IntegrityError:
            pass
        else:
            accepted.append(sql)
        finally:
            # Roll back even an unexpectedly accepted write so every case starts
            # from the same pristine state and all fourteen checks still run.
            store.conn.rollback()
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall(), sql
    assert not accepted, f'Invalid writes unexpectedly succeeded: {accepted}'


def test_retained_history_survives_membership_removal_and_mode_refresh(upgraded_relationships):
    store = upgraded_relationships
    store.history.record_play_event(1,'a|artist','a',100,playlist_ytm_id='PL')
    store.player_events.record_player_event(1,'track_exit','a',190,200,'PL',None,101)
    before = (store.history.play_events_since(0), store.player_events.player_events_since(0))
    assert [(r['account_id'],r['playlist_ytm_id'],r['played_at']) for r in before[0]]==[(1,'PL',100)]
    assert [(r['account_id'],r['playlist_ytm_id'],r['position'],r['duration'])
            for r in before[1]]==[(1,'PL',190,200)]
    store.playlists.remove_playlist(1)
    store.conn.execute('DELETE FROM library_memberships WHERE entry_id=1')
    store.conn.commit()
    assert (store.history.play_events_since(0),store.player_events.player_events_since(0))==before
    assert store.conn.execute('SELECT COUNT(*) FROM enrichment_log WHERE track_id=1').fetchone()[0]==1
    assert store.playlists.get_playlist_groups()=={'PL':'Keep'}
    mode = store.modes.list_modes()[0]
    store.modes.replace_modes([mode],[],200)
    store.modes.replace_modes([],[7],300)
    assert store.modes.pick_counts()=={7:1}
    assert store.conn.execute('SELECT mode_id FROM rec_mode_picks WHERE membership_ref_id=1').fetchone()[0]==7
    for sql in ('DELETE FROM catalog_entries WHERE id=1','DELETE FROM taste_modes WHERE mode_id=7',
                "DELETE FROM playlist_scopes WHERE id='PL'"):
        with pytest.raises(sqlite3.IntegrityError),store.conn:
            store.conn.execute(sql)
    store.road_trip.delete_road_trip_recipe(1)
    assert not store.conn.execute('SELECT 1 FROM road_trip_drafts WHERE recipe_id=1').fetchone()


def test_indexes_support_the_affected_queries(store):
    queries = (
        ('SELECT video_id FROM playlist_items WHERE playlist_id=? ORDER BY position',(1,),'ix_playlist_item_position'),
        ('SELECT playlist_id FROM unresolved_playlist_items WHERE entry_id=?',(1,),'ix_unresolved_playlist_entry'),
        ('SELECT capture_id FROM listening_observations WHERE recording_id=? AND day>=?',(1,'2026-01-01'),'ix_listening_observation_recording_day'),
        ('SELECT id FROM listening_capture_receipts WHERE account_id=? AND captured_at>=?',(1,1),'ix_listening_capture_account_time'),
        ('SELECT id FROM play_events WHERE account_id=? AND playlist_ytm_id=? AND played_at>=?',(1,'PL',1),'ix_play_event_account_playlist_time'),
        ('SELECT recording_id FROM youtube_items WHERE video_id=?',('a',),'sqlite_autoindex_youtube_items_1'),
        ('SELECT recording_id FROM recording_identifiers WHERE scheme=? AND entity_type=? AND value=?',('musicbrainz','recording','id'),'ix_recording_identifier'),
        ('SELECT album_id FROM album_provider_ids WHERE provider=? AND provider_id=?',('youtube','id'),'sqlite_autoindex_album_provider_ids_1'),
    )
    for sql,args,index in queries:
        plan = ' '.join(r[3] for r in store.conn.execute('EXPLAIN QUERY PLAN '+sql,args))
        assert index in plan,(sql,plan)


def test_unknown_legacy_modes_cannot_become_feedback_for_a_new_mode(upgraded_relationships):
    from tests.mode_fixtures import seed_modes
    store = upgraded_relationships
    seed_modes(store,999)
    assert store.modes.impression_counts()=={7:1}
    assert store.modes.pick_counts()=={7:1}
    assert all(r['mode_id']!=999 for r in store.modes.choice_learning_rows())


def test_account_scope_guard_rejects_another_accounts_system_playlist(store):
    a = store.accounts.upsert_account('a','c','brand',False)
    b = store.accounts.upsert_account('b','c',None,True)
    first = store.playlists.upsert_playlist(a,'LM','A',0,'h',1)
    second = store.playlists.upsert_playlist(b,'LM','B',0,'h',1)
    ref = store.playlists.get_playlist(second).playlist_scope_id
    with pytest.raises(sqlite3.IntegrityError),store.conn:
        store.conn.execute('UPDATE playlists SET playlist_scope_id=? WHERE id=?',(ref,first))


def test_deleted_playlist_ids_never_attach_old_feedback_to_new_memberships(store):
    from tests.mode_fixtures import seed_modes
    seed_modes(store,1,2)
    owner = store.accounts.upsert_account('Main','c',None,True)
    first = store.playlists.upsert_playlist(owner,'FIRST','First',0,'h',1)
    store.modes.log_pick(first,1,10)
    store.playlists.remove_playlist(first)
    second = store.playlists.upsert_playlist(owner,'SECOND','Second',0,'h',20)
    assert second!=first
    store.modes.log_pick(second,2,30)
    store.history.record_play_event(owner,'song|artist','video',40,playlist_ytm_id='SECOND')
    retained = store.conn.execute(
        'SELECT p.membership_ref_id,p.mode_id,m.account_id,s.provider_playlist_id '
        'FROM rec_mode_picks p JOIN playlist_membership_refs m ON m.id=p.membership_ref_id '
        'JOIN playlist_scopes s ON s.id=m.playlist_scope_id ORDER BY p.membership_ref_id')
    assert [tuple(row) for row in retained]==[(first,1,owner,'FIRST'),(second,2,owner,'SECOND')]
    events = store.history.play_events_since(0)
    assert [(r['account_id'],r['playlist_ytm_id']) for r in events]==[(owner,'SECOND')]
    with pytest.raises(sqlite3.IntegrityError),store.conn:
        store.conn.execute('UPDATE playlist_membership_refs SET playlist_scope_id=? WHERE id=?',('SECOND',first))


def test_implicit_rowid_allocation_cannot_retarget_a_retained_membership(store):
    from yt_playlist.core.playlist_scopes import ensure
    owner = store.accounts.upsert_account('Main','c',None,True)
    first = store.playlists.upsert_playlist(owner,'FIRST','First',0,'h',1)
    store.playlists.remove_playlist(first)
    ensure(store.conn,'NEXT',owner)
    store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError),store.conn:
        store.conn.execute("INSERT INTO playlists(account_id,provider_playlist_id,title,playlist_scope_id) VALUES (?,'NEXT','Next','NEXT')",(owner,))
    second = store.playlists.upsert_playlist(owner,'NEXT','Next',0,'h',2)
    assert second>first
