import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.catalog_migration_support import catalog_input, catalog_reader
from tests.enrichment_support import complete_entries, seed_audio, seed_enrichment
from yt_playlist.core.migrations import Migration, MigrationError, migrate, regular
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations.runner import Context, validate_version
from yt_playlist.core.migrations import v008_recordings
from yt_playlist.core import legacy_evidence
from yt_playlist.core.store import Store


def populate_legacy(store):
    store.conn.executescript((Path(__file__).parents[1] / 'fixtures/migrations/recording_catalog.sql').read_text())


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    # This module starts no HTTP application or periodic jobs.
    pass


def assert_converted_catalog(s):
    centroid = b'\x00\x00\x80\x3f'
    assert s.conn.execute('SELECT COUNT(*) FROM youtube_items').fetchone()[0] == 4
    assert s.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 5
    assert s.conn.execute('SELECT COUNT(*) FROM legacy_catalog_evidence').fetchone()[0] == 7
    assert s.tracks.track_for_waterfall(7)['id'] == 3
    assert s.tracks.track_for_waterfall(7)['video_id'] == 'upload-a'
    assert {'provider':'musicbrainz','value':'Techno'} in s.enrichment_reports.genre_provenance(3)['candidates']
    assert s.playlists.get_playlist_track_ids(1) == [3,8]
    assert s.playlists.get_playlist(1).track_count == 2
    assert {r[0] for r in s.conn.execute('SELECT browse_id FROM album_track_links WHERE recording_id=3')} == {'release-a','release-b','release-c'}
    assert s.conn.execute('SELECT title,orig_title,genre FROM tracks WHERE id=3').fetchone()[:] == ('Corrected Song','Song','')
    assert s.conn.execute('SELECT bpm,typeof(bpm) FROM tracks WHERE id=8').fetchone()[:] == (121.0,'real')
    assert s.conn.execute('SELECT available,video_id FROM tracks WHERE id=10').fetchone()[:] == (0,None)
    assert s.conn.execute('SELECT COUNT(*) FROM recording_identifiers WHERE status="candidate"').fetchone()[0] == 4
    events = list(s.conn.execute('SELECT identity_key,recording_id,raw_identity_key,video_id,is_generated FROM play_event_subjects ORDER BY id'))
    assert len(events) == 3
    assert events[0][1:3] == (8,'song|artist')
    assert events[1][:3] == ('song|artist',None,'song|artist')
    assert events[2][1:] == (None,'old spelling|artist','unknown-upload',1)
    assert s.conn.execute('SELECT COUNT(*) FROM rec_feedback WHERE kind="like"').fetchone()[0] == 1
    assert s.conn.execute('SELECT target_type FROM rec_feedback WHERE kind="like"').fetchone()[0] == 'legacy_unresolved'
    for rid in (3,8,9):
        assert not s.rec_model.mark_graduated_once(s.conn.execute('SELECT identity_key FROM recordings WHERE id=?',(rid,)).fetchone()[0],'like_grad',999)
    assert s.rec_model.weight_records()['genre:house']['weight'] == 1.3
    assert s.enrichment_state.enrichment_attempts('cold|other')['deezer']['fields']['art'] == 'full-cover'
    assert s.conn.execute("SELECT COUNT(*) FROM metadata_legacy_evidence WHERE kind='legacy_enrichment_attempt'").fetchone()[0] == 1
    assert s.conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0] == 4
    canvas = json.loads(s.playlist_recipes.get_cluster_canvas('CLUSTER'))
    key = s.tracks.catalog_subject(3)['identity_key']
    mode = s.conn.execute('SELECT centroid,rep_keys,space FROM taste_modes WHERE mode_id=1').fetchone()
    assert mode['centroid'] == centroid
    assert json.loads(mode['rep_keys']) == [key]
    assert mode['space'] == 'legacy-identity-v1'
    archived_mode = json.loads(legacy_evidence.original(s.conn, 'recommendation', 'taste_modes', '1'))
    assert bytes.fromhex(archived_mode['centroid']['sqlite_blob_hex']) == centroid
    assert canvas['nodes'][0]['key'] == key
    assert canvas['nodes'][0]['seeds'][0]['keys'] == [key]
    assert canvas['trunk'] == [1] and canvas['label'] == 'old spelling|artist'
    assert not s.conn.execute('PRAGMA foreign_key_check').fetchall()
    assert s.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert s.tracks.upsert_track('new-upload','New','Artist',None,None) > 10


@pytest.mark.parametrize('catalog_input', [(7, 79), (79, 88), (88, 102)],
                         indirect=True, ids=['recordings', 'metadata', 'subjects'])
def test_populated_catalog_conversion_preserves_values_and_evidence(catalog_input):
    conn, path, source, target = catalog_input
    before = tuple(conn.execute('SELECT version,name FROM schema_migrations'))
    migrate(conn, registry=MIGRATIONS[:target], clock=lambda: 1.)
    assert tuple(conn.execute('SELECT version,name FROM schema_migrations'))[:source] == before
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == target
    with catalog_reader(path, target) as store:
        assert_converted_catalog(store)


@pytest.mark.parametrize('catalog_input', [(102, 103)], indirect=True)
def test_populated_catalog_account_cutover_preserves_rows(catalog_input, monkeypatch):
    from yt_playlist.core.migrations import v103_entity_names

    conn, _path, _source, target = catalog_input
    tables = ('playlists', 'playlist_scopes', 'playlist_membership_refs', 'play_events',
              'listening_observations', 'recordings', 'catalog_entries', 'rec_feedback')
    before = {name: list(map(tuple, conn.execute(f'SELECT * FROM "{name}"'))) for name in tables}
    restore = Mock()
    monkeypatch.setattr(v103_entity_names, 'restore_programs', restore)
    migrate(conn, registry=MIGRATIONS[:target], clock=lambda: 1.)
    restore.assert_called_once()
    # The restoration owner installs and validates every canonical program.
    # This owner retains actual account/scope renames and populated row IDs.
    assert list(conn.execute('SELECT * FROM accounts'))
    assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='identities'").fetchone()
    for name in tables:
        assert list(map(tuple, conn.execute(f'SELECT * FROM "{name}"'))) == before[name]
    assert {'account_id', 'playlist_scope_id', 'provider_playlist_id'} <= {
        row[1] for row in conn.execute('PRAGMA table_info(playlists)')}
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('catalog_input', [(103, 104), (104, 107)], indirect=True,
                         ids=['domain-contracts', 'native-admission'])
def test_populated_catalog_native_upgrade_preserves_values_and_evidence(catalog_input):
    conn, path, _source, target = catalog_input
    migrate(conn, registry=MIGRATIONS[:target], clock=lambda: 1.)
    with catalog_reader(path, target) as store:
        assert_converted_catalog(store)


def complete_regular_input(conn):
    # Supply unrelated completed regular owners below production admission.
    with conn:
        conn.execute('BEGIN')
        conn.execute(regular.HISTORY_SQL)
        for change in regular.CHANGES:
            change.apply(conn, Context(1.))
            conn.execute('INSERT INTO schema_changes VALUES (?,?,1)', (change.version, change.name))
        regular.validate(conn)


@pytest.mark.parametrize('catalog_input', [(107, 107)], indirect=True)
def test_native_catalog_admission_is_repeatable_without_changing_history(catalog_input):
    conn, _path, _source, _target = catalog_input
    complete_regular_input(conn)
    before = tuple(map(tuple, conn.execute('SELECT * FROM play_events')))
    history = tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations')))
    assert migrate(conn, defer_validation=True) is False
    assert migrate(conn, defer_validation=True) is False
    assert tuple(map(tuple, conn.execute('SELECT * FROM play_events'))) == before
    assert tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations'))) == history
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('catalog_input', [(107, 107)], indirect=True)
def test_native_catalog_reopen_keeps_converted_history_and_originals(catalog_input):
    conn, path, _source, _target = catalog_input
    complete_regular_input(conn)
    before = tuple(map(tuple, conn.execute('SELECT * FROM play_events')))
    history = tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations')))
    original = legacy_evidence.original(conn, 'recommendation', 'taste_modes', '1')
    store = Store(path)
    try:
        store.init_schema()
        assert tuple(map(tuple, store.conn.execute('SELECT * FROM play_events'))) == before
        assert tuple(map(tuple, store.conn.execute('SELECT * FROM schema_migrations'))) == history
        assert legacy_evidence.original(store.conn, 'recommendation', 'taste_modes', '1') == original
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


@pytest.mark.parametrize('catalog_input', [(107, 107)], indirect=True)
def test_converted_catalog_enrichment_queue_deduplicates_membership(catalog_input):
    _conn, path, _source, target = catalog_input
    with catalog_reader(path, target, snapshot_guards=True) as store:
        batch = store.enrichment_queue.next_enrich_batch(20)
        assert len(batch) == 5
        assert {row['id'] for row in batch} == {3, 8, 9, 10, 11}


def test_failure_after_reference_remapping_rolls_back_and_can_resume(legacy_store, monkeypatch):
    s = legacy_store
    populate_legacy(s)
    original = v008_recordings.remap_state
    def fail(*args):
        original(*args)
        raise RuntimeError('after remap')
    monkeypatch.setattr(v008_recordings,'remap_state',fail)
    with pytest.raises(MigrationError,match='after remap'):
        migrate(s.conn, registry=MIGRATIONS[:8])
    assert s.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 7
    assert s.conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0] == 5
    assert not s.conn.execute("SELECT 1 FROM sqlite_master WHERE name='recordings'").fetchone()
    monkeypatch.setattr(v008_recordings,'remap_state',original)
    # Exercise this interrupted owner's retry. The populated conversion and
    # native admission owners above cover the later contracts independently.
    migrate(s.conn, registry=MIGRATIONS[:8])
    validate_version(s.conn, 8)
    assert tuple(s.conn.execute(
        'SELECT entry_id,video_id FROM legacy_track_refs WHERE legacy_id=7'
    ).fetchone()) == (3, 'upload-a')


def test_migration_unknown_video_cannot_borrow_an_existing_recording_key(legacy_store):
    s = legacy_store
    populate_legacy(s)
    s.conn.execute("INSERT INTO play_events(identity_id,identity_key,video_id,played_at) VALUES (1,'cold|other','unknown-upload',400)")
    s.conn.commit()
    migrate(s.conn, registry=MIGRATIONS[:10])
    validate_version(s.conn, 10)
    row = s.conn.execute('SELECT identity_key,raw_identity_key,recording_id FROM play_events WHERE played_at=400').fetchone()
    assert row['raw_identity_key'] == 'cold|other' and row['recording_id'] is None
    assert row['identity_key'] != s.tracks.identity_key_for_video('cold-upload')


def test_exact_uploads_versions_and_verified_grouping_keep_item_choices_and_likes(store):
    s = store
    iid = s.accounts.upsert_account('main','cred',None,True)
    a = s.tracks.upsert_track('a','Song','Artist','Original',200,True)
    assert s.tracks.upsert_track('a','Changed title','Changed credit','Original',201,True) == a
    b = s.tracks.upsert_track('b','Song (Live)','Artist','Live',250,True)
    c = s.tracks.upsert_track('c','Song','Artist','Reissue',202,True)
    assert len({s.tracks.catalog_subject(t)['recording_id'] for t in (a,b,c)}) == 3
    assert s.tracks.match_recording('Song','Artist') is None
    assert s.tracks.match_recording('Song (Live)','Artist') == s.tracks.catalog_subject(b)['identity_key']
    pid = s.playlists.upsert_playlist(iid,'LM','Liked',3,'hash',1)
    s.playlists.set_playlist_tracks(pid,[a,b,c])
    seed_audio(s, a, bpm=123)
    seed_audio(s, c, danceability=.7)
    c_key = s.tracks.catalog_subject(c)['identity_key']
    s.enrichment_state.record_enrichment_attempt(c_key,'provider',{'danceability':.7},video_id='c',now=1)
    s.tracks.set_track_genre(c,'')
    s.tracks.set_track_year(a,'1990')
    s.tracks.set_track_year(c,'2020')
    s.tracks.associate_youtube_item('c',s.tracks.catalog_subject(a)['recording_id'],source='verified',evidence={'isrc':'independently verified'},now=2,same_recording=True)
    assert s.tracks.resolve_identity_key(c_key) == s.tracks.catalog_subject(a)['identity_key']
    assert s.playlists.get_playlist_track_ids(pid) == [a,b,c]
    assert s.tracks.get_track_audio(c)[0] == 123
    assert s.enrichment_state.enrichment_attempts(s.tracks.catalog_subject(a)['identity_key'])['provider']['fields'] == {'danceability':.7}
    assert s.conn.execute('SELECT danceability FROM catalog_tracks WHERE id=?',(a,)).fetchone()[0] == .7
    seed_enrichment(s, a, 'House', None)
    seed_audio(s, c, energy=.5)
    assert s.tracks.get_track_enrichment(a) == ('House','1990')
    assert s.tracks.get_track_enrichment(c) == ('','2020')
    s.playlists.set_song_liked(iid,'a',False)
    assert s.playlists.get_playlist_track_ids(pid) == [b,c]
    assert len(s.enrichment_queue.next_enrich_batch(20)) == 2
    assert not s.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_discovery_promotion_expiry_and_attempts_share_catalog_without_ownership(store):
    s = store
    key = s.discovery.upsert_discovered_track('cold|artist','cold','Cold','Artist','Album',None,None,None,'radio:a',1)
    s.discovery.upsert_discovered_track('new spelling|artist','cold','Cold','Artist','Album',None,None,None,'radio:b',2)
    assert not s.rec_query.library_keys()
    s.enrichment_state.record_enrichment_attempt(key,'provider',{'art':'cover','isrc':['ONE']},video_id='cold',now=3)
    s.enrichment_state.record_enrichment_attempt(key,'provider',{},video_id='cold',now=4)
    assert s.conn.execute('SELECT COUNT(*) FROM provider_attempts').fetchone()[0] == 2
    tid = s.tracks.upsert_track('cold','Cold','Artist','Album',200)
    assert len(s.enrichment_queue.next_enrich_batch(20)) == 1
    assert key in s.rec_query.library_keys()
    assert key in s.discovery.discovered_tracks_by_keys([key])
    s.discovery.gc_discovery_pool(100*86400,30,set())
    assert not s.discovery.get_discovered_tracks()
    assert s.tracks.identity_key_for_video('cold') == key
    assert s.enrichment_state.enrichment_attempts(key)['provider']['status'] == 'no_match'
    assert s.conn.execute('SELECT COUNT(*) FROM youtube_items').fetchone()[0] == 1


def test_missing_video_entries_are_repeatable_album_scoped_and_unplayable(store):
    a = store.tracks.upsert_track(None,'Song','Artist','Album A',200,album_browse_id='A')
    assert store.tracks.upsert_track('', 'Song','Artist','Album A',200,album_browse_id='A') == a
    b = store.tracks.upsert_track(None,'Song','Artist','Album B',200,album_browse_id='B')
    assert a != b
    assert not store.conn.execute('SELECT * FROM youtube_items').fetchall()
    assert store.conn.execute('SELECT SUM(available) FROM tracks').fetchone()[0] == 0


def test_catalog_constraints_and_current_schema_are_checked(store):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('',1)")
    store.conn.rollback()
    store.conn.execute('DROP TRIGGER playlist_items_position_insert')
    store.conn.commit()
    with pytest.raises(MigrationError,match='playlist_items_position_insert'):
        store.init_schema()


def test_confirmed_recording_has_one_coverage_count_and_a_coherent_playable_item(store):
    a = store.tracks.upsert_track('a','Song','Artist','First release',200,False,thumbnail='first-cover')
    b = store.tracks.upsert_track('b','Song','Artist','Second release',201,True,thumbnail='second-cover')
    store.tracks.set_track_year(a,'1990')
    store.tracks.set_track_year(b,'2020')
    store.tracks.associate_youtube_item('b',store.tracks.catalog_subject(a)['recording_id'],
                                source='verified',evidence={'review':'same recording'},now=2)
    assert store.enrichment_reports.coverage_stats()['total'] == 1
    assert store.enrichment_reports.coverage_stats()['year'] == 1
    songs = store.rec_query.tracks_by_keys([store.tracks.catalog_subject(a)['identity_key']])
    chosen = next(iter(songs.values()))
    assert chosen['video_id'] == 'b'
    assert chosen['thumbnail'] == 'second-cover'
    assert chosen['album'] == 'Second release'


def test_alias_disambiguation_and_verified_association_preserve_event_counts_and_state(store):
    iid = store.accounts.upsert_account('main','credential',None,True)
    a = store.tracks.upsert_track('a','Song','Artist',None,200)
    original = store.tracks.catalog_subject(a)['identity_key']
    store.history.record_play_event(iid,original,'a',10)
    store.history.record_history_plays(iid,10,[original])
    store.rec_model.record_feedback('for_you',original,'less',now=11)
    store.playlist_recipes.set_cluster_canvas('saved',json.dumps({'nodes':[{'id':1,'keys':[original]}],'label':original}))
    b = store.tracks.upsert_track('b','Song','Artist',None,200)
    key = store.tracks.catalog_subject(a)['identity_key']
    assert key != original
    assert store.tracks.resolve_identity_key(original) is None
    assert store.conn.execute('SELECT identity_key,raw_identity_key FROM play_event_subjects').fetchone()[:] == (key,original)
    assert store.conn.execute('SELECT item_key FROM rec_feedback').fetchone()[0] == key
    store.tracks.associate_youtube_item('b',store.tracks.catalog_subject(a)['recording_id'],
                                source='verified',evidence={'review':'same recording'},now=12)
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 1
    assert store.conn.execute('SELECT COUNT(*) FROM listening_observations').fetchone()[0] == 1
    saved = json.loads(store.playlist_recipes.get_cluster_canvas('saved'))
    assert saved['nodes'][0]['keys'] == [key] and saved['label'] == original
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_repeated_observation_of_an_ambiguous_alias_keeps_rebuilt_vectors(store):
    a = store.tracks.upsert_track('a','Song','Artist',None,200)
    store.tracks.upsert_track('b','Song','Artist',None,200)
    key = store.tracks.catalog_subject(a)['identity_key']
    vector = b'\x00\x00\x80\x3f'
    store.rec_model.replace_rec_vectors([(key,vector)])
    store.tracks.upsert_track('a','Song','Artist',None,200)
    assert store.rec_model.get_rec_vectors() == [(key,vector)]


@pytest.mark.parametrize('retire_source', [False, True])
def test_upload_correction_invalidates_both_derived_owners_atomically(store, monkeypatch, retire_source):
    from yt_playlist.core import catalog, prepared_inputs

    old_entry = store.tracks.upsert_track('old-upload', 'Old', 'Artist', None, 200)
    target_entry = store.tracks.upsert_track('target-upload', 'Target', 'Artist', None, 200)
    old = store.tracks.catalog_subject(old_entry)
    target = store.tracks.catalog_subject(target_entry)
    if not retire_source:
        store.tracks.upsert_track('retained-upload', 'Retained', 'Artist', None, 200)
        with store.conn:
            catalog.associate_item(store.conn, 'retained-upload', old['recording_id'],
                                   source='fixture', evidence={'verified': True}, now=201)
    vector = b'\x00\x00\x80\x3f'
    expected = sorted([(old['identity_key'], vector), (target['identity_key'], vector)])
    store.rec_model.replace_rec_vectors(expected)
    with store.conn:
        for subject in (old, target):
            prepared_inputs.upsert(store.conn, subject['identity_key'],
                {'library': {'genre': 'Rock', 'year': None, 'audio': {}}, 'discovered': None},
                'a' * 64, 202, 1)
    serial = store.taste_groups.serial()
    finish = catalog.finish_reassociation
    def interrupted(*args, **kwargs):
        raise RuntimeError('controlled correction interruption')
    monkeypatch.setattr(catalog, 'finish_reassociation', interrupted)
    with pytest.raises(RuntimeError, match='controlled correction interruption'), store.conn:
        catalog.associate_item(store.conn, 'old-upload', target['recording_id'],
                               source='fixture', evidence={'corrected': True}, now=203)
    assert store.rec_model.get_rec_vectors() == expected
    assert store.conn.execute('SELECT count(*) FROM prepared_recording_inputs').fetchone()[0] == 2
    assert store.taste_groups.serial() == serial
    assert store.tracks.catalog_subject(old_entry)['recording_id'] == old['recording_id']
    monkeypatch.setattr(catalog, 'finish_reassociation', finish)
    with store.conn:
        catalog.associate_item(store.conn, 'old-upload', target['recording_id'],
                               source='fixture', evidence={'corrected': True}, now=203)
    assert store.rec_model.get_rec_vectors() == []
    assert not store.conn.execute('SELECT 1 FROM prepared_recording_inputs').fetchone()
    assert store.taste_groups.serial() == serial + 2
    assert store.tracks.catalog_subject(old_entry)['recording_id'] == target['recording_id']
    assert bool(store.conn.execute('SELECT 1 FROM recording_redirects WHERE retired_id=?',
                                  (old['recording_id'],)).fetchone()) == retire_source


def test_enrichment_priority_counts_confirmed_recording_once_per_artist(store):
    iid = store.accounts.upsert_account('main','credential',None,True)
    a = store.tracks.upsert_track('a','Song','Artist',None,200,created_at=1)
    b = store.tracks.upsert_track('b','Song','Artist',None,200,created_at=1)
    other = store.tracks.upsert_track('other','Other song','Other artist',None,200,created_at=1)
    store.tracks.associate_youtube_item('b',store.tracks.catalog_subject(a)['recording_id'],
                                source='verified',evidence={'review':'same recording'},now=2)
    key = store.tracks.catalog_subject(a)['identity_key']
    other_key = store.tracks.catalog_subject(other)['identity_key']
    store.history.record_history_plays(iid,86400,[key,other_key])
    store.history.record_history_plays(iid,172800,[other_key])
    complete_entries(store, [a, b, other], 3)
    preferred = store.tracks.upsert_track('other-new','New song','Other artist',None,200,created_at=10)
    store.tracks.upsert_track('artist-new','New song','Artist',None,200,created_at=20)
    store.settings.set_setting('enrich_caught_up_at','30')
    assert store.enrichment_queue.next_enrich_batch(1)[0]['id'] == preferred


def test_identity_changes_preserve_but_refuse_incompatible_model_rollback(store):
    run = store.model_selection.start_selection(1,{'status':'applied'})
    store.tracks.upsert_track('a','Song','Artist',None,200)
    store.tracks.upsert_track('b','Song','Artist',None,200)
    assert store.model_selection.selection_run(run)['status'] == 'applied'
    with pytest.raises(ValueError,match='catalog identity change'):
        store.model_selection.restore_model_selection(run,3)


def test_retired_keys_deduplicate_ingestion_without_losing_exact_upload_plays(store):
    iid = store.accounts.upsert_account('main','credential',None,True)
    a = store.tracks.upsert_track('a','Song','Artist',None,200)
    b = store.tracks.upsert_track('b','Song','Artist',None,200)
    retired = store.tracks.catalog_subject(b)['identity_key']
    store.tracks.associate_youtube_item('b',store.tracks.catalog_subject(a)['recording_id'],
                                source='verified',evidence={'review':'same recording'},now=1)
    assert store.history.import_play_events(iid,[(retired,'b',100)]) == 1
    assert store.history.import_play_events(iid,[(retired,'b',100)]) == 0
    assert not store.history.record_play_event(iid,retired,'b',101)
    assert store.history.record_play_event(iid,retired,'a',102)
    assert store.history.import_play_events(iid,[(retired,'b',104)]) == 1
    assert store.history.import_plays(iid,[(retired,86400)]) == 1
    assert store.history.import_plays(iid,[(retired,86400)]) == 0
    assert store.history.record_history_plays(iid,86400,[retired],source_timezone='UTC') == 0
    assert store.conn.execute('SELECT raw_identity_key FROM listening_observations').fetchone()[0] == retired
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 4
    assert store.history.listening_day_counts()=={store.tracks.catalog_subject(a)['identity_key']:2}


@pytest.mark.parametrize('video_id',[None,'',' ','unknown-upload'])
def test_play_observations_only_use_text_when_video_is_missing(store,video_id):
    iid = store.accounts.upsert_account('main','credential',None,True)
    tid = store.tracks.upsert_track('known','Song','Artist',None,200)
    subject = store.tracks.catalog_subject(tid)
    store.history.import_play_events(iid,[(subject['identity_key'],video_id,100)])
    expected = None if video_id == 'unknown-upload' else subject['recording_id']
    assert store.conn.execute('SELECT recording_id FROM play_events').fetchone()[0] == expected
    if video_id == 'unknown-upload':
        assert subject['identity_key'] not in dict(store.history.recent_plays_with_ts())
        assert store.history.import_play_events(iid,[(subject['identity_key'],video_id,100)]) == 0


def test_later_catalog_observation_only_resolves_history_for_the_exact_video(store):
    iid = store.accounts.upsert_account('main','credential',None,True)
    store.history.import_play_events(iid,[('song|artist','unknown-upload',100)])
    known = store.tracks.upsert_track('known','Song','Artist',None,200)
    assert store.tracks.catalog_subject(known)['identity_key'] not in dict(store.history.recent_plays_with_ts())
    observed = store.tracks.upsert_track('unknown-upload','Song','Artist',None,200)
    row = store.conn.execute('SELECT identity_key,raw_identity_key,recording_id FROM play_event_subjects').fetchone()
    assert row[:] == (store.tracks.catalog_subject(observed)['identity_key'],'song|artist',store.tracks.catalog_subject(observed)['recording_id'])
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 1
