import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.historical_captures import retained_capture
from yt_playlist.core.migrations import Migration,MigrationError,migrate
from yt_playlist.core.migrations import registry,v026_listening_observations as upgrade
from yt_playlist.core.migrations.runner import Context
from yt_playlist.core.store import Store


def stamp(local,zone):
    return dt.datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(zone)).timestamp()


@pytest.fixture
def listening_store():
    store = Store(':memory:')
    store.init_schema()
    store.identities.upsert_identity('main','bridge',None,True)
    store.identities.upsert_identity('other','bridge','other',False)
    yield store
    store.conn.close()


@pytest.mark.parametrize('zone',['America/Costa_Rica','Pacific/Kiritimati'])
def test_temporal_cutoffs_keep_exact_times_instead_of_account_date_keys(listening_store,zone):
    s = listening_store
    ts = stamp('2026-10-20T00:30:00',zone)
    for video,title in [('past','Past'),('later','Later')]:
        s.tracks.upsert_track(video,title,'Artist '+video,None,200,created_at=0)
    past,later = (s.tracks.identity_key_for_video(v) for v in ('past','later'))
    s.history.import_listening_events(1,[(past,'past',ts),(later,'later',ts+600)],source_timezone=zone)
    cutoff = ts+300
    assert s.history.history_keys_before(cutoff)=={past}
    assert s.history.history_keys_between(cutoff,ts+1200)=={later}
    assert s.history.play_counts_before(cutoff)=={past:1}
    baskets,_ = s.rec_evidence.rec_baskets_as_of(cutoff,max_album=0)
    assert not any({past,later}<=set(basket) for basket in baskets)
    assert s.history.listening_day_counts()=={past:1,later:1}


def test_exclusion_keeps_actual_morning_time_when_coarse_same_day_exists(listening_store):
    s = listening_store
    ts = stamp('2026-10-20T08:00:00','UTC')
    s.history.record_play_event(1,'song',None,ts,source_timezone='UTC')
    s.history.record_history_plays(1,ts+60,['song'],source_timezone='UTC')
    assert s.history.recent_plays_with_ts(exclude_list_ids={'unrelated'})==[('song',ts)]


def test_event_generation_policy_does_not_change_with_later_playlist_curation(listening_store):
    s = listening_store
    s.playlists.upsert_playlist(1,'PL','Mix',0,'h',1)
    ts = stamp('2026-10-20T12:00:00','UTC')
    s.history.record_play_event(1,'ordinary',None,ts,playlist_ytm_id='PL',source_timezone='UTC')
    s.playlists.mark_generated_playlist('PL')
    s.history.record_play_event(1,'generated',None,ts+3600,playlist_ytm_id='PL')
    s.playlists.adopt_generated_playlist('PL')
    assert s.trends.ledger_track_plays(ts-1,ts+7200)=={'ordinary':1}
    assert s.history.listening_day_counts()=={'ordinary':1}
    assert s.rec_evidence.generated_only_play_days()=={(int(ts//86400),'generated')}


def test_installed_layout_retains_raw_evidence_and_unknown_precision():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript((Path(__file__).parents[1]/'fixtures/migrations/listening_v25.sql').read_text())
    raw = {name:list(map(tuple,conn.execute(f'SELECT * FROM {name}'))) for name in
           ('history_items','history_snapshots','play_events','player_events')}
    with conn:
        upgrade.apply(conn,Context(1))
    upgrade.validate(conn)
    for name,rows in raw.items():
        assert list(map(tuple,conn.execute(f'SELECT * FROM {name}')))==rows
    legacy = conn.execute('SELECT precision,timezone FROM legacy_listening_observations').fetchall()
    assert all(tuple(r)==('legacy_unknown',None) for r in legacy)
    assert conn.execute('SELECT COUNT(*) FROM listening_days WHERE identity_id=1').fetchone()[0]==1
    assert conn.execute('SELECT COUNT(*) FROM listening_days WHERE identity_id=2').fetchone()[0]==0
    assert not conn.execute('SELECT * FROM trend_first_play').fetchall()
    assert not conn.execute('SELECT * FROM settings').fetchall()
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    conn.close()


@pytest.mark.parametrize('zone,local',[
    ('America/Costa_Rica','2026-10-20T23:30:00'),
    ('Pacific/Kiritimati','2026-10-20T00:30:00'),
    ('America/New_York','2026-11-01T01:30:00'),
    ('America/New_York','2026-03-08T03:30:00'),
])
@pytest.mark.parametrize('first',['import','capture'])
def test_local_calendar_overlap_in_both_arrival_orders(listening_store,zone,local,first):
    s = listening_store
    ts = stamp(local,zone)
    def imported():
        s.history.import_listening_events(1,[('song',None,ts)],source='takeout')
    def capture():
        s.history.record_play_event(1,'song',None,ts,source_timezone=zone)
        s.history.record_history_plays(1,ts,[('song','Today')],source_timezone=zone)
    (imported if first=='import' else capture)()
    (capture if first=='import' else imported)()
    assert s.history.import_listening_events(1,[('song',None,ts+.5)])==(0,0)
    assert s.history.listening_day_counts(1)=={'song':1}
    assert s.history.event_play_counts(1)=={'song':1}
    assert s.conn.execute('SELECT day FROM listening_days').fetchone()[0]==local[:10]
    assert {r[0] for r in s.conn.execute('SELECT source FROM play_event_sources')}=={'live','takeout'}
    assert s.conn.execute('SELECT COUNT(*) FROM listening_observations').fetchone()[0]==1
    assert s.conn.execute('SELECT COUNT(*) FROM listening_capture_receipts').fetchone()[0]==0


def test_repeated_events_and_coarse_days_are_distinct_units(listening_store):
    s = listening_store
    ts = stamp('2026-10-20T12:00:00','UTC')
    assert s.history.import_listening_events(1,[('song',None,ts),('song',None,ts+3600)])==(1,2)
    s.history.record_history_plays(1,ts,[('song','Today')],source_timezone='UTC')
    s.history.record_history_plays(1,ts+86400,[('song','Today')],source_timezone='UTC')
    assert s.history.listening_day_counts()=={'song':2}
    assert s.history.event_play_counts()=={'song':2}
    assert s.conn.execute('SELECT COUNT(*) FROM listening_activity').fetchone()[0]==3


def test_removing_one_source_preserves_overlaps_and_unknown_legacy(listening_store):
    s = listening_store
    ts = stamp('2026-10-20T12:00:00','UTC')
    s.history.import_listening_events(1,[('both',None,ts),('spotify-only',None,ts)],source='spotify')
    s.history.import_listening_events(1,[('both',None,ts)],source='takeout')
    retained_capture(s.conn,1,ts,['unknown'],source='unknown')
    s.history.record_history_plays(1,ts,['independent'],source_timezone='UTC')
    raw = list(map(tuple,s.conn.execute("SELECT * FROM listening_observations WHERE precision='legacy_unknown'")))
    s.history.remove_listening_source('spotify',1)
    assert s.history.event_play_counts()=={'both':1}
    assert s.history.listening_day_counts()=={'both':1,'independent':1,'unknown':1}
    assert list(map(tuple,s.conn.execute("SELECT * FROM listening_observations WHERE precision='legacy_unknown'")))==raw
    assert s.conn.execute('SELECT source FROM play_events').fetchone()[0]=='takeout'
    with pytest.raises(ValueError,match='Unknown legacy'):
        s.history.remove_listening_source('unknown')


def test_foreign_coarse_date_does_not_guess_and_later_ambiguity_clears_projection(listening_store):
    s = listening_store
    ts = stamp('2026-10-20T23:30:00','America/Costa_Rica')
    s.history.record_play_event(1,'song',None,ts,source_timezone='America/Costa_Rica')
    s.history.record_history_plays(1,ts,[('song','Today')],source_timezone='UTC')
    assert s.history.listening_day_counts()=={'song':1}
    assert s.conn.execute('SELECT day FROM listening_observations').fetchone()[0]=='2026-10-20'
    # Another play on that UTC date lies on the following account date.
    s.history.import_listening_events(1,[('song',None,ts+7200)])
    assert s.conn.execute('SELECT day FROM listening_observations').fetchone()[0] is None
    assert s.history.listening_day_counts()=={'song':2}


def test_generated_sync_shadow_is_account_scoped_and_independent_evidence_survives(listening_store):
    s = listening_store
    ts = stamp('2026-10-20T23:30:00','America/Costa_Rica')
    s.history.record_play_event(1,'song',None,ts,source_timezone='America/Costa_Rica')
    s.conn.execute('UPDATE play_events SET is_generated=1')
    s.conn.commit()
    s.history.record_history_plays(1,ts,[('song','Today')],source_timezone='America/Costa_Rica')
    s.history.record_history_plays(2,ts,[('song','Today')],source_timezone='America/Costa_Rica')
    assert s.history.listening_day_counts(1)=={}
    assert s.history.listening_day_counts(2)=={'song':1}
    s.history.record_history_plays(1,ts,['song'],source_timezone='America/Costa_Rica')
    assert s.history.listening_day_counts(1)=={'song':1}
    assert s.history.event_play_counts(1)=={}


def test_failed_draft_upgrade_rolls_back_and_backup_is_installed_v25(tmp_path):
    s = Store(tmp_path/'installed.db')
    migrate(s.conn,registry=registry.MIGRATIONS[:25])
    s.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','bridge',1)")
    s.conn.execute('INSERT INTO history_snapshots VALUES (1,1,100)')
    s.conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'original')")
    s.conn.commit()
    before = list(s.conn.iterdump())
    def fail(conn,context):
        upgrade.apply(conn,context)
        raise RuntimeError('interrupted listening upgrade')
    with pytest.raises(MigrationError,match='interrupted listening'):
        migrate(s.conn,registry=(*registry.MIGRATIONS[:25],Migration(26,'listening_observations',fail)))
    assert list(s.conn.iterdump())==before
    for path in (tmp_path/'backups').glob('*.db'):
        with sqlite3.connect(path) as backup:
            assert backup.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==25
            assert backup.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    s.conn.close()


def test_playlist_counts_and_taste_weights_ignore_confirmed_alternate_upload(listening_store):
    import numpy as np
    from yt_playlist.rec.scoring import _playlist_centroids

    s = listening_store
    a = s.tracks.upsert_track('a','Song','Artist',None,200)
    b = s.tracks.upsert_track('b','Other','Artist',None,200)
    key,other = s.tracks.identity_key_for_video('a'),s.tracks.identity_key_for_video('b')
    first = s.playlists.upsert_playlist(1,'P1','First',1,'h',1)
    second = s.playlists.upsert_playlist(1,'P2','Second',2,'h',1)
    s.playlists.set_playlist_tracks(first,[a])
    s.playlists.set_playlist_tracks(second,[a,b])
    ts = stamp('2026-10-20T12:00:00','UTC')
    s.history.import_listening_events(1,[(key,'a',ts),(key,'a',ts+3600),(other,'b',ts)])
    s.history.record_history_plays(1,ts,[(key,'Today')],source_timezone='UTC')
    s.history.record_history_plays(1,ts-86400,[(key,'Today')],source_timezone='UTC')
    stats = s.charts.get_playlist_listen_stats()
    assert stats[first][1]==3 and stats[second][1]==4
    idx = {key:0,other:1}
    matrix = np.eye(2)
    before = _playlist_centroids(s,matrix,idx)
    alternate = s.tracks.upsert_track('alternate','Alternate upload','Artist',None,200)
    s.tracks.associate_youtube_item('alternate',s.tracks.catalog_subject(a)['recording_id'],
                                   source='review',evidence={'same_recording':True},now=1)
    s.playlists.set_playlist_tracks(first,[a,alternate])
    assert [r[1] for r in s.playlists.get_playlist_tracks_with_meta(first)]==['a','alternate']
    assert s.charts.get_playlist_listen_stats()==stats
    after = _playlist_centroids(s,matrix,idx)
    np.testing.assert_array_equal(after.weights,before.weights)
    np.testing.assert_array_equal(after.centroids,before.centroids)
    assert len(s.charts.get_playlist_track_recency()[first])==1
    health = {row['playlist_id']:row['listens'] for row in s.trends.dead_playlists(10)}
    assert health=={first:3,second:4}
    assert s.history.listening_day_counts(1)[key]==2
    assert s.history.event_play_counts(1)[key]==2


def test_reassociation_preserves_coarse_sources_and_first_play_atomically(listening_store,monkeypatch):
    from yt_playlist.core import catalog

    s = listening_store
    first = s.tracks.upsert_track('a','Old','Artist',None,200)
    target = s.tracks.upsert_track('b','Correct','Artist',None,200)
    old,key = s.tracks.identity_key_for_video('a'),s.tracks.identity_key_for_video('b')
    s.history.record_history_plays(1,10*86400,[(old,'Today'),(key,'Today')],source_timezone='UTC')
    s.history.import_listening_events(1,[(key,'b',20*86400)])
    s.trends.refresh_first_play()
    previous = list(s.conn.iterdump())
    original = catalog.invalidate_derived
    def fail(conn):
        original(conn)
        raise RuntimeError('interrupt association')
    monkeypatch.setattr(catalog,'invalidate_derived',fail)
    with pytest.raises(RuntimeError,match='interrupt association'):
        s.tracks.associate_youtube_item('a',s.tracks.catalog_subject(target)['recording_id'],
                                       source='review',evidence={'same':True},now=1)
    assert list(s.conn.iterdump())==previous
    monkeypatch.setattr(catalog,'invalidate_derived',original)
    s.tracks.associate_youtube_item('a',s.tracks.catalog_subject(target)['recording_id'],
                                   source='review',evidence={'same':True},now=1)
    assert s.settings.get_setting('trend_first_play_watermark') is None
    assert not s.trends.first_play_map('track')
    assert s.history.listening_day_counts()=={key:2}
    evidence = s.conn.execute('SELECT raw_identity_key,identity_key FROM listening_observations').fetchall()
    assert {r[0] for r in evidence}=={old,key} and {r[1] for r in evidence}=={key}
    s.trends.refresh_first_play()
    assert s.trends.first_play_map('track')=={key:10}
    assert s.tracks.catalog_subject(first)['recording_id']==s.tracks.catalog_subject(target)['recording_id']


def test_successful_draft_upgrade_reopens_without_rewriting_data(tmp_path):
    path = tmp_path/'installed.db'
    s = Store(path)
    migrate(s.conn,registry=registry.MIGRATIONS[:25])
    s.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','bridge',1)")
    s.conn.execute('INSERT INTO history_snapshots VALUES (1,1,100)')
    s.conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'original')")
    s.conn.commit()
    draft_registry = (*registry.MIGRATIONS[:25],Migration(26,'listening_observations',upgrade.apply))
    migrate(s.conn,registry=draft_registry)
    upgrade.validate(s.conn)
    before = list(s.conn.iterdump())
    s.conn.close()
    reopened = Store(path)
    migrate(reopened.conn,registry=draft_registry)
    upgrade.validate(reopened.conn)
    assert list(reopened.conn.iterdump())==before
    assert reopened.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert list((tmp_path/'backups').glob('*.db'))
    reopened.conn.close()


def test_import_failure_rolls_back_event_source_and_calendar(listening_store,monkeypatch):
    from yt_playlist.core import listening

    s = listening_store
    before = list(s.conn.iterdump())
    original = listening.project_event
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('interrupt event projection')
    monkeypatch.setattr(listening,'project_event',fail)
    with pytest.raises(RuntimeError,match='interrupt event projection'):
        s.history.import_listening_events(1,[('song',None,1000)],source_timezone='America/Costa_Rica')
    assert list(s.conn.iterdump())==before


def test_partial_reassociation_keeps_unattributed_coarse_evidence_with_recording(listening_store):
    s = listening_store
    a = s.tracks.upsert_track('a','First','Artist',None,200)
    sibling = s.tracks.upsert_track('sibling','Sibling','Artist',None,200)
    b = s.tracks.upsert_track('b','Second','Artist',None,200)
    first_id = s.tracks.catalog_subject(a)['recording_id']
    second_id = s.tracks.catalog_subject(b)['recording_id']
    s.tracks.associate_youtube_item('sibling',first_id,source='review',evidence={'same':True},now=1)
    old,key = s.tracks.identity_key_for_video('a'),s.tracks.identity_key_for_video('b')
    s.history.record_history_plays(1,10*86400,[(old,'Today')],source_timezone='UTC')
    s.history.import_listening_events(1,[(old,'a',20*86400)])
    s.tracks.associate_youtube_item('a',second_id,source='review',evidence={'correction':True},now=2)
    assert s.tracks.catalog_subject(sibling)['recording_id']==first_id
    assert s.history.listening_day_counts()=={old:1,key:1}
    assert s.history.event_play_counts()=={key:1}
    assert s.conn.execute('SELECT raw_identity_key FROM play_events').fetchone()[0]==old
    s.trends.refresh_first_play()
    assert s.trends.first_play_map('track')=={old:10,key:20}
