"""Canonical listening owners preserve raw evidence, precision and publication isolation."""
import sqlite3

import pytest

from yt_playlist.core import listening, listening_summaries, artists
from yt_playlist.core.migrations import current


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
