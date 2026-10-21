import json
from pathlib import Path

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations import v008_recordings
from yt_playlist.core.store import Store


def populate_legacy(store):
    store.conn.executescript((Path(__file__).parents[1] / 'fixtures/migrations/recording_catalog.sql').read_text())


def test_populated_migration_preserves_identity_membership_choices_and_raw_evidence(legacy_store):
    s = legacy_store
    populate_legacy(s)
    centroid = b'\x00\x00\x80\x3f'
    s.conn.execute("INSERT INTO taste_modes(mode_id,label,families,centroid,size,rep_keys,active,first_seen,last_seen,space) VALUES (1,'House','[]',?,1,?,1,1,1,'old-space')",
                   (centroid,json.dumps(['old spelling|artist'])))
    s.conn.commit()
    s.init_schema()
    assert s.conn.execute('SELECT COUNT(*) FROM youtube_items').fetchone()[0] == 4
    assert s.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 5
    assert s.conn.execute('SELECT COUNT(*) FROM legacy_track_evidence').fetchone()[0] == 7
    assert s.track_for_waterfall(7)['id'] == 3
    assert s.track_for_waterfall(7)['video_id'] == 'upload-a'
    assert {'provider':'musicbrainz','value':'Techno'} in s.genre_provenance(3)['candidates']
    assert s.get_playlist_track_ids(1) == [3,8]
    assert s.get_playlist(1).track_count == 2
    assert {r[0] for r in s.conn.execute('SELECT browse_id FROM album_track_links WHERE recording_id=3')} == {'release-a','release-b','release-c'}
    assert s.conn.execute('SELECT title,orig_title,genre FROM tracks WHERE id=3').fetchone()[:] == ('Corrected Song','Song','')
    assert s.conn.execute('SELECT bpm,typeof(bpm) FROM tracks WHERE id=8').fetchone()[:] == (121.0,'real')
    assert s.conn.execute('SELECT available,video_id FROM tracks WHERE id=10').fetchone()[:] == (0,None)
    assert s.conn.execute('SELECT COUNT(*) FROM recording_identifiers WHERE status="candidate"').fetchone()[0] == 4
    events = list(s.conn.execute('SELECT identity_key,recording_id,raw_identity_key,video_id,is_generated FROM play_events ORDER BY id'))
    assert len(events) == 3
    assert events[0][1:3] == (8,'song|artist')
    assert events[1][:3] == ('song|artist',None,'song|artist')
    assert events[2][1:] == (None,'old spelling|artist','unknown-upload',1)
    assert s.conn.execute('SELECT COUNT(*) FROM rec_feedback WHERE kind="like"').fetchone()[0] == 1
    assert s.conn.execute('SELECT target_type FROM rec_feedback WHERE kind="like"').fetchone()[0] == 'legacy_unresolved'
    for rid in (3,8,9):
        assert not s.mark_graduated_once(s.conn.execute('SELECT identity_key FROM recordings WHERE id=?',(rid,)).fetchone()[0],'like_grad',999)
    assert s.weight_records()['genre:house']['weight'] == 1.3
    assert s.enrichment_attempts('cold|other')['deezer']['fields']['art'] == 'full-cover'
    assert s.conn.execute('SELECT COUNT(*) FROM legacy_enrichment_attempts').fetchone()[0] == 1
    assert s.conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0] == 4
    assert len(s.next_enrich_batch(20)) == 5  # library/discovery overlap is queued once
    canvas = json.loads(s.get_cluster_canvas('CLUSTER'))
    key = s.catalog_subject(3)['identity_key']
    mode = s.conn.execute('SELECT centroid,rep_keys,space FROM taste_modes WHERE mode_id=1').fetchone()
    assert mode['centroid'] == centroid
    assert json.loads(mode['rep_keys']) == [key]
    assert mode['space'] == 'legacy-identity-v1'
    archived_mode = json.loads(s.conn.execute("SELECT payload FROM identity_state_archive WHERE kind='taste_modes'").fetchone()[0])
    assert bytes.fromhex(archived_mode['centroid']['sqlite_blob_hex']) == centroid
    assert canvas['nodes'][0]['key'] == key
    assert canvas['nodes'][0]['seeds'][0]['keys'] == [key]
    assert canvas['trunk'] == [1] and canvas['label'] == 'old spelling|artist'
    before = list(map(tuple,s.conn.execute('SELECT * FROM play_events')))
    s.init_schema(); s.init_schema()
    assert list(map(tuple,s.conn.execute('SELECT * FROM play_events'))) == before
    assert not s.conn.execute('PRAGMA foreign_key_check').fetchall()
    assert s.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert s.upsert_track('new-upload','New','Artist',None,None) > 10


def test_failure_after_reference_remapping_rolls_back_and_can_resume(legacy_store, monkeypatch):
    s = legacy_store
    populate_legacy(s)
    original = v008_recordings.remap_state
    def fail(*args):
        original(*args)
        raise RuntimeError('after remap')
    monkeypatch.setattr(v008_recordings,'remap_state',fail)
    with pytest.raises(MigrationError,match='after remap'):
        migrate(s.conn)
    assert s.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 7
    assert s.conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0] == 5
    assert not s.conn.execute("SELECT 1 FROM sqlite_master WHERE name='recordings'").fetchone()
    monkeypatch.setattr(v008_recordings,'remap_state',original)
    s.init_schema()
    assert s.track_for_waterfall(7)['video_id'] == 'upload-a'


def test_migration_unknown_video_cannot_borrow_an_existing_recording_key(legacy_store):
    s = legacy_store
    populate_legacy(s)
    s.conn.execute("INSERT INTO play_events(identity_id,identity_key,video_id,played_at) VALUES (1,'cold|other','unknown-upload',400)")
    s.conn.commit()
    s.init_schema()
    row = s.conn.execute('SELECT identity_key,raw_identity_key,recording_id FROM play_events WHERE played_at=400').fetchone()
    assert row['raw_identity_key'] == 'cold|other' and row['recording_id'] is None
    assert row['identity_key'] != s.identity_key_for_video('cold-upload')


def test_exact_uploads_versions_and_verified_grouping_keep_item_choices_and_likes(store):
    s = store
    iid = s.upsert_identity('main','cred',None,True)
    a = s.upsert_track('a','Song','Artist','Original',200,True)
    assert s.upsert_track('a','Changed title','Changed credit','Original',201,True) == a
    b = s.upsert_track('b','Song (Live)','Artist','Live',250,True)
    c = s.upsert_track('c','Song','Artist','Reissue',202,True)
    assert len({s.catalog_subject(t)['recording_id'] for t in (a,b,c)}) == 3
    assert s.match_recording('Song','Artist') is None
    assert s.match_recording('Song (Live)','Artist') == s.catalog_subject(b)['identity_key']
    pid = s.upsert_playlist(iid,'LM','Liked',3,'hash',1)
    s.set_playlist_tracks(pid,[a,b,c])
    s.set_track_audio(a,bpm=123)
    s.set_track_audio(c,danceability=.7)
    c_key = s.catalog_subject(c)['identity_key']
    s.record_enrichment_attempt(c_key,'provider',{'danceability':.7},video_id='c',now=1)
    s.set_track_genre(c,'')
    s.set_track_year(a,'1990')
    s.set_track_year(c,'2020')
    s.associate_youtube_item('c',s.catalog_subject(a)['recording_id'],source='verified',evidence={'isrc':'independently verified'},now=2)
    assert s.resolve_identity_key(c_key) == s.catalog_subject(a)['identity_key']
    assert s.get_playlist_track_ids(pid) == [a,b,c]
    assert s.get_track_audio(c)[0] == 123
    assert s.enrichment_attempts(s.catalog_subject(a)['identity_key'])['provider']['fields'] == {'danceability':.7}
    assert s.conn.execute('SELECT danceability FROM catalog_tracks WHERE id=?',(a,)).fetchone()[0] == .7
    s.set_track_enrichment(a,'House',None)
    s.set_track_audio(c,energy=.5)
    assert s.get_track_enrichment(a) == ('House','1990')
    assert s.get_track_enrichment(c) == ('','2020')
    s.set_song_liked(iid,'a',False)
    assert s.get_playlist_track_ids(pid) == [b,c]
    assert len(s.next_enrich_batch(20)) == 2
    assert not s.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_discovery_promotion_expiry_and_attempts_share_catalog_without_ownership(store):
    s = store
    key = s.upsert_discovered_track('cold|artist','cold','Cold','Artist','Album',None,None,None,'radio:a',1)
    s.upsert_discovered_track('new spelling|artist','cold','Cold','Artist','Album',None,None,None,'radio:b',2)
    assert not s.library_keys()
    s.record_enrichment_attempt(key,'provider',{'art':'cover','isrc':['ONE']},video_id='cold',now=3)
    s.record_enrichment_attempt(key,'provider',{},video_id='cold',now=4)
    assert s.conn.execute('SELECT COUNT(*) FROM provider_attempts').fetchone()[0] == 2
    tid = s.upsert_track('cold','Cold','Artist','Album',200)
    assert len(s.next_enrich_batch(20)) == 1
    assert s.enrichment_memberships([s.catalog_subject(tid)['recording_id']]) == {'library','discovery'}
    s.gc_discovery_pool(100*86400,30,set())
    assert not s.get_discovered_tracks()
    assert s.identity_key_for_video('cold') == key
    assert s.enrichment_attempts(key)['provider']['status'] == 'no_match'
    assert s.conn.execute('SELECT COUNT(*) FROM youtube_items').fetchone()[0] == 1


def test_missing_video_entries_are_repeatable_album_scoped_and_unplayable(store):
    a = store.upsert_track(None,'Song','Artist','Album A',200,album_browse_id='A')
    assert store.upsert_track('', 'Song','Artist','Album A',200,album_browse_id='A') == a
    b = store.upsert_track(None,'Song','Artist','Album B',200,album_browse_id='B')
    assert a != b
    assert not store.conn.execute('SELECT * FROM youtube_items').fetchall()
    assert store.conn.execute('SELECT SUM(available) FROM tracks').fetchone()[0] == 0


def test_catalog_constraints_and_current_schema_are_checked(store):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('',1)")
    store.conn.rollback()
    store.conn.execute('DROP TRIGGER playlist_tracks_insert')
    store.conn.commit()
    with pytest.raises(MigrationError,match='playlist_tracks_insert'):
        store.init_schema()


def test_confirmed_recording_has_one_coverage_count_and_a_coherent_playable_item(store):
    a = store.upsert_track('a','Song','Artist','First release',200,False,thumbnail='first-cover')
    b = store.upsert_track('b','Song','Artist','Second release',201,True,thumbnail='second-cover')
    store.set_track_year(a,'1990')
    store.set_track_year(b,'2020')
    store.associate_youtube_item('b',store.catalog_subject(a)['recording_id'],
                                source='verified',evidence={'review':'same recording'},now=2)
    assert store.coverage_stats()['total'] == 1
    assert store.coverage_stats()['year'] == 1
    songs = store.tracks_by_keys([store.catalog_subject(a)['identity_key']])
    chosen = next(iter(songs.values()))
    assert chosen['video_id'] == 'b'
    assert chosen['thumbnail'] == 'second-cover'
    assert chosen['album'] == 'Second release'


def test_alias_disambiguation_and_verified_association_preserve_event_counts_and_state(store):
    iid = store.upsert_identity('main','credential',None,True)
    a = store.upsert_track('a','Song','Artist',None,200)
    original = store.catalog_subject(a)['identity_key']
    store.record_play_event(iid,original,'a',10)
    store.record_history_plays(iid,10,[original])
    store.record_feedback('for_you',original,'less',now=11)
    store.conn.execute("INSERT INTO cluster_canvas(playlist_ytm,state) VALUES ('saved',?)",
                       (json.dumps({'nodes':[{'id':1,'keys':[original]}],'label':original}),))
    store.conn.commit()
    b = store.upsert_track('b','Song','Artist',None,200)
    key = store.catalog_subject(a)['identity_key']
    assert key != original
    assert store.resolve_identity_key(original) is None
    assert store.conn.execute('SELECT identity_key,raw_identity_key FROM play_events').fetchone()[:] == (key,original)
    assert store.conn.execute('SELECT item_key FROM rec_feedback').fetchone()[0] == key
    store.associate_youtube_item('b',store.catalog_subject(a)['recording_id'],
                                source='verified',evidence={'review':'same recording'},now=12)
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 1
    assert store.conn.execute('SELECT COUNT(*) FROM history_items').fetchone()[0] == 1
    saved = json.loads(store.get_cluster_canvas('saved'))
    assert saved['nodes'][0]['keys'] == [key] and saved['label'] == original
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_repeated_observation_of_an_ambiguous_alias_keeps_rebuilt_vectors(store):
    a = store.upsert_track('a','Song','Artist',None,200)
    store.upsert_track('b','Song','Artist',None,200)
    key = store.catalog_subject(a)['identity_key']
    store.replace_rec_vectors([(key,b'rebuilt')])
    store.upsert_track('a','Song','Artist',None,200)
    assert store.get_rec_vectors() == [(key,b'rebuilt')]


def test_enrichment_priority_counts_confirmed_recording_once_per_artist(store):
    iid = store.upsert_identity('main','credential',None,True)
    a = store.upsert_track('a','Song','Artist',None,200,created_at=1)
    b = store.upsert_track('b','Song','Artist',None,200,created_at=1)
    other = store.upsert_track('other','Other song','Other artist',None,200,created_at=1)
    store.associate_youtube_item('b',store.catalog_subject(a)['recording_id'],
                                source='verified',evidence={'review':'same recording'},now=2)
    key = store.catalog_subject(a)['identity_key']
    other_key = store.catalog_subject(other)['identity_key']
    store.record_history_plays(iid,86400,[key,other_key])
    store.record_history_plays(iid,172800,[other_key])
    store.mark_enriched([a,b,other],3)
    preferred = store.upsert_track('other-new','New song','Other artist',None,200,created_at=10)
    store.upsert_track('artist-new','New song','Artist',None,200,created_at=20)
    store.set_setting('enrich_caught_up_at','30')
    assert store.next_enrich_batch(1)[0]['id'] == preferred


def test_identity_changes_preserve_but_refuse_incompatible_model_rollback(store):
    run = store.rec.model.start_selection(1,{'status':'applied'})
    store.upsert_track('a','Song','Artist',None,200)
    store.upsert_track('b','Song','Artist',None,200)
    assert store.rec.model.selection_run(run)['status'] == 'applied'
    with pytest.raises(ValueError,match='catalog identity change'):
        store.rec.model.restore_model_selection(run,3)


def test_retired_keys_deduplicate_ingestion_without_losing_exact_upload_plays(store):
    iid = store.upsert_identity('main','credential',None,True)
    a = store.upsert_track('a','Song','Artist',None,200)
    b = store.upsert_track('b','Song','Artist',None,200)
    retired = store.catalog_subject(b)['identity_key']
    store.associate_youtube_item('b',store.catalog_subject(a)['recording_id'],
                                source='verified',evidence={'review':'same recording'},now=1)
    assert store.import_play_events(iid,[(retired,'b',100)]) == 1
    assert store.import_play_events(iid,[(retired,'b',100)]) == 0
    assert not store.record_play_event(iid,retired,'b',101)
    assert store.record_play_event(iid,retired,'a',102)
    assert store.import_play_events(iid,[(retired,'b',104)]) == 1
    assert store.import_plays(iid,[(retired,86400)]) == 1
    assert store.import_plays(iid,[(retired,86400)]) == 0
    assert store.record_history_plays(iid,86400,[retired],source_timezone='UTC') == 0
    assert store.conn.execute('SELECT raw_identity_key FROM history_items').fetchone()[0] == retired
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 3


@pytest.mark.parametrize('video_id',[None,'',' ','unknown-upload'])
def test_play_observations_only_use_text_when_video_is_missing(store,video_id):
    iid = store.upsert_identity('main','credential',None,True)
    tid = store.upsert_track('known','Song','Artist',None,200)
    subject = store.catalog_subject(tid)
    store.import_play_events(iid,[(subject['identity_key'],video_id,100)])
    expected = None if video_id == 'unknown-upload' else subject['recording_id']
    assert store.conn.execute('SELECT recording_id FROM play_events').fetchone()[0] == expected
    if video_id == 'unknown-upload':
        assert subject['identity_key'] not in dict(store.recent_plays_with_ts())
        assert store.import_play_events(iid,[(subject['identity_key'],video_id,100)]) == 0


def test_later_catalog_observation_only_resolves_history_for_the_exact_video(store):
    iid = store.upsert_identity('main','credential',None,True)
    store.import_play_events(iid,[('song|artist','unknown-upload',100)])
    known = store.upsert_track('known','Song','Artist',None,200)
    assert store.catalog_subject(known)['identity_key'] not in dict(store.recent_plays_with_ts())
    observed = store.upsert_track('unknown-upload','Song','Artist',None,200)
    row = store.conn.execute('SELECT identity_key,raw_identity_key,recording_id FROM play_events').fetchone()
    assert row[:] == (store.catalog_subject(observed)['identity_key'],'song|artist',store.catalog_subject(observed)['recording_id'])
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 1
