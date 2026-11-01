"""Canonical listening owners preserve raw evidence, precision and publication isolation."""
import sqlite3

import pytest

from yt_playlist.core import listening, listening_summaries, genre_identity, artists
from yt_playlist.core.migrations import current, registry, Migration, MigrationError, migrate
from yt_playlist.core.migrations import v100_listening_subjects
from yt_playlist.core.store import Store


def test_fresh_owners_have_no_duplicate_keys_or_scalar_source(store):
    for table in ('play_events','listening_observations'):
        columns = {r[1] for r in store.conn.execute('PRAGMA table_info('+table+')')}
        assert 'identity_key' not in columns
    assert 'source' not in {r[1] for r in store.conn.execute('PRAGMA table_info(play_events)')}
    assert store.conn.execute("SELECT type FROM sqlite_schema WHERE name='trend_first_play'").fetchone()[0]=='view'
    current.validate(store.conn)


def test_exact_coarse_unknown_upload_sources_and_private_summary(store):
    account = store.accounts.upsert_account('Listener','credential',None,False)
    entry = store.tracks.upsert_track('known','Known','Artist',None,10)
    rid,key = store.conn.execute('SELECT recording_id,identity_key FROM catalog_tracks WHERE id=?',(entry,)).fetchone()
    with store.conn:
        listening.calendar(store.conn,account,'America/Costa_Rica')
        listening.observe_day(store.conn,account,key,'2020-01-01','independent','America/Costa_Rica',10)
    store.history.import_play_events(account,[(key,'known',1609488000)],source='takeout')
    store.history.import_play_events(account,[(key,'known',1609488000)],source='live')
    store.history.record_play_event(account,key,'outside',1609588000)
    assert store.conn.execute('SELECT count(*) FROM play_events').fetchone()[0]==2
    unresolved = store.conn.execute("SELECT * FROM play_events WHERE video_id='outside'").fetchone()
    assert unresolved['raw_identity_key']==key and unresolved['recording_id'] is None
    assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='outside'").fetchone()
    store.trends.refresh_first_play()
    summary = store.conn.execute('SELECT * FROM recording_listening_summary').fetchone()
    assert summary['account_id']==account and summary['recording_id']==rid
    assert summary['first_observed_day']=='2020-01-01'
    assert summary['first_exact_play_at']==1609488000
    assert summary['first_exact_day']=='2021-01-01'
    assert summary['first_legacy_unknown_day'] is None
    with store.read_snapshot() as snapshot:
        prepared = snapshot.store.trends.prepare_first_play()
        assert prepared['recording_listening_summary']
        assert snapshot.store.trends.first_play_map('artist')
    with store.conn:
        listening.remove_source(store.conn,'takeout',account)
    assert store.conn.execute('SELECT count(*) FROM play_events').fetchone()[0]==2
    assert store.trends.first_play_map('track')=={}
    store.trends.refresh_first_play()
    assert store.trends.first_play_map('track')[key]==18262
    with pytest.raises(ValueError,match='changed'):
        listening_summaries.publish(store.conn,prepared)


def test_summary_transaction_rolls_back_on_failed_publication(store,monkeypatch):
    account = store.accounts.upsert_account('Listener','credential',None,False)
    store.history.import_play_events(account,[('raw',None,1609488000)])
    store.trends.refresh_first_play()
    before = dict(store.trends.first_play_map('track'))
    original = listening_summaries.rebuild
    def fail(conn,**kwargs):
        original(conn,**kwargs)
        raise sqlite3.OperationalError('interrupted')
    monkeypatch.setattr(listening_summaries,'rebuild',fail)
    with pytest.raises(sqlite3.OperationalError,match='interrupted'):
        store.trends.refresh_first_play()
    assert store.trends.first_play_map('track')==before


def installed_99(path):
    store = Store(path)
    for sql in current.statements(version=99):
        store.conn.execute(sql)
    genre_identity.seed_catalog(store.conn)
    store.conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
    for sql in ('INSERT INTO rec_preparation VALUES (1,0,0)','INSERT INTO rec_group_clock VALUES (1,0)',
                'INSERT INTO group_retention_state VALUES (1,0)'):
        store.conn.execute(sql)
    store.conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',[(m.version,m.name) for m in registry.MIGRATIONS[:99]])
    store.conn.commit()
    store._bind_repositories()
    return store


def test_upgrade_preserves_all_evidence_and_failed_conversion_rolls_back(tmp_path):
    old = installed_99(tmp_path/'predecessor.db')
    account = old.accounts.upsert_account('Listener','c',None,True)
    entry = old.tracks.upsert_track('known','Known','Artist',None,10)
    rid,key = old.conn.execute('SELECT recording_id,identity_key FROM catalog_tracks WHERE id=?',(entry,)).fetchone()
    with old.conn:
        old.conn.execute("INSERT INTO play_events(identity_id,identity_key,video_id,played_at,source) VALUES (?,?,'known',20,'takeout')",(account,key))
        old.conn.execute("INSERT INTO play_events(identity_id,identity_key,video_id,played_at,source) VALUES (?,?,'unknown',30,'live')",(account,key))
        old.conn.execute("INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,recording_id,source_day,day,source,timezone,precision,first_observed_at,last_observed_at) VALUES (?,'Raw title',?,?,'2020-01-01','2020-01-01','independent','UTC','calendar_day',1,2)",(account,key,rid))
    original = {t:[dict(r) for r in old.conn.execute('SELECT * FROM '+t)] for t in
        ('play_events','play_event_sources','play_event_calendar','listening_observations')}
    def interrupted(conn,context):
        v100_listening_subjects.apply(conn,context)
        raise RuntimeError('interrupted conversion')
    with pytest.raises(MigrationError,match='interrupted conversion'):
        migrate(old.conn,registry=(*registry.MIGRATIONS[:99],Migration(100,'listening_subjects',interrupted,rebuild_tables=True)))
    for table,rows in original.items():
        assert [dict(r) for r in old.conn.execute('SELECT * FROM '+table)]==rows
    migrate(old.conn, registry=registry.MIGRATIONS)
    old.init_schema()
    for table,rows in original.items():
        relation = {'play_events':'play_event_subjects','listening_observations':'listening_observation_subjects'}.get(table,table)
        converted = [dict(r) for r in old.conn.execute('SELECT * FROM '+relation)]
        expected = [{('account_id' if k=='identity_id' else k):v for k,v in r.items()
                     if k!='source' or table!='play_events'} for r in rows]
        assert len(converted) == len(expected)
        assert [{key:row[key] for key in original} for row,original in zip(converted,expected)] == expected
    current.validate(old.conn)
    old.conn.close()
    reopened = Store(tmp_path/'predecessor.db')
    reopened.init_schema()
    assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    reopened.conn.close()


def test_current_canonical_collaborations_same_names_and_multiple_accounts(store):
    first = store.accounts.upsert_account('First','c',None,True)
    second = store.accounts.upsert_account('Second','c2',None,False)
    entry = store.tracks.upsert_track('collab','Collaboration','Display credit',None,10)
    other = store.tracks.upsert_track('other','Other','Same name',None,10)
    with store.conn:
        a = artists.ensure(store.conn,'Same name',provider='youtube',provider_id='A')
        b = artists.ensure(store.conn,'Same name',provider='youtube',provider_id='B')
        store.conn.execute('DELETE FROM entry_artists WHERE entry_id IN (?,?)',(entry,other))
        store.conn.executemany("INSERT INTO entry_artists VALUES (?,?,?,?,'controlled')",[(entry,0,a,'Same name'),(entry,1,b,'Same name'),(entry,2,a,'Same name'),(other,0,b,'Same name')])
    key = store.conn.execute('SELECT identity_key FROM catalog_tracks WHERE id=?',(entry,)).fetchone()[0]
    store.history.import_play_events(first,[(key,'collab',1609488000)])
    store.history.import_play_events(second,[(key,'collab',1609574400)])
    store.trends.refresh_first_play()
    assert {(r[0],r[1]) for r in store.conn.execute('SELECT account_id,artist_id FROM artist_listening_summary')}=={
        (first,a),(first,b),(second,a),(second,b)}
    assert store.trends.ledger_artist_plays(0,2000000000)=={'@artist:'+str(a):2,'@artist:'+str(b):2}
    with store.conn:
        store.conn.execute("UPDATE artists SET name='Renamed' WHERE id=?",(a,))
    assert not store.trends.first_play_map('artist')
    store.trends.refresh_first_play()
    assert len(store.trends.first_play_map('artist'))==2
    assert store.trends.artist_labels()['@artist:'+str(a)]=='Renamed'


def test_late_catalog_arrival_resolves_coarse_and_exact_without_rewriting_capture(store):
    account = store.accounts.upsert_account('Listener','c',None,True)
    key = 'known|artist'
    with store.conn:
        listening.calendar(store.conn,account,'UTC')
    store.history.import_play_events(account,[(key,'later',1609488000)])
    with store.conn:
        listening.observe_day(store.conn,account,key,'2020-01-01','independent','UTC',10)
    before = store.conn.execute('SELECT raw_identity_key,video_id,played_at FROM play_events').fetchone()
    store.tracks.upsert_track('later','Known','Artist',None,10)
    assert tuple(store.conn.execute('SELECT raw_identity_key,video_id,played_at FROM play_events').fetchone())==tuple(before)
    assert store.conn.execute('SELECT recording_id FROM play_events').fetchone()[0] is not None
    assert store.conn.execute('SELECT recording_id FROM listening_observations').fetchone()[0] is not None
    store.trends.refresh_first_play()
    assert store.conn.execute('SELECT first_observed_day FROM recording_listening_summary').fetchone()[0]=='2020-01-01'


def test_raw_playlist_provenance_has_optional_verified_retained_membership(store):
    account = store.accounts.upsert_account('Listener','c',None,True)
    playlist = store.playlists.upsert_playlist(account,'PLknown','Known',0,'hash',1)
    store.history.record_play_event(account,'raw','unknown-upload',1000)
    store.history.record_play_event(account,'raw','unknown-upload',1010,playlist_ytm_id='RDAMPLPLknown')
    row = store.conn.execute('SELECT membership_ref_id,playlist_ytm_id FROM play_events').fetchone()
    assert tuple(row)==(playlist,'RDAMPLPLknown')
    store.history.record_play_event(account,'second','another-upload',4000,playlist_ytm_id='outside')
    assert store.conn.execute("SELECT membership_ref_id FROM play_events WHERE playlist_ytm_id='outside'").fetchone()[0] is None
    with pytest.raises(sqlite3.IntegrityError,match='membership'):
        store.conn.execute("UPDATE play_events SET membership_ref_id=? WHERE playlist_ytm_id='outside'",(playlist,))
