"""Runtime cutover precedes analytics, with normal writes between the phases."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import sqlite3
import struct
import threading
import time

import pytest

from tests.core.test_db_rebuild_destination import development93, production64
from yt_playlist.core import catalog, db_handles, home_outputs, metadata_native, metadata_runtime, metadata_runtime_files
from yt_playlist.core.db_rebuild import build, job, verification
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.core.store import Store


@pytest.mark.parametrize(('source_fixture', 'active'), [
    ('production64', False), ('development93', False), ('development93', True)])
def test_rebuild_discards_metadata_caches_and_refreshes_without_losing_user_choices(
        request, source_fixture, active, monkeypatch):
    from yt_playlist.core import metadata, metadata_attempts, metadata_conflicts, provider_frontiers
    from yt_playlist.core.preparation import metadata_runtime as preparation
    from yt_playlist.core.preparation.metadata_runtime_verification import verify_selected
    path = request.getfixturevalue(source_fixture)
    at = time.time()
    fields = {'bpm': 120}
    card = {'schema': 1, 'protos': [{'label': 'Keep listening', 'tracks': [{'video_id': 'video-8'}]}]}
    configuration = {'provider': {'fingerprint': 'unchanged', 'status': 'available'},
                     'disabled': {'fingerprint': 'disabled', 'status': 'disabled'}}
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('UPDATE recordings SET first_enriched_at=10,last_enriched_at=10 WHERE id IN (3,8)')
            conn.executemany('INSERT INTO enrichment_provider_config VALUES (?,?,?)',
                             [(key, value['fingerprint'], value['status']) for key,value in configuration.items()])
            if source_fixture == 'production64':
                conn.execute('UPDATE recordings SET bpm=130 WHERE id=8')
                conn.execute("INSERT INTO recording_field_sources VALUES (8,'bpm','user',17)")
                conn.execute("INSERT INTO entry_field_choices VALUES (108,'bpm','130','user')")
                conn.execute('INSERT INTO enrichment_provider_state VALUES (8,\'provider\',10,?,\'found\',?,0,0,\'\')',
                             (at, json.dumps(fields)))
                conn.execute('INSERT INTO enrichment_conflict(track_id,field,candidates,updated_at) VALUES (108,\'bpm\',?,?)',
                             (json.dumps([{'provider': 'provider', 'value': 120}]), at))
            else:
                delivery, _ = metadata_attempts.capture_result(conn, 108, 'provider', fields, run_id='old', now=at)
                metadata.apply_provider(conn, 108, 'provider', fields, ('provider',), delivery_id=delivery, now=at)
                metadata_attempts.record(conn, 108, 'provider', fields, cycle=10, delivery_id=delivery, now=at)
                metadata_conflicts.upsert(conn, 108, 'bpm', [{'provider': 'provider', 'value': 120}], now=at)
                catalog.choose_field(conn, 108, 'bpm', 130, now=at+1)
            conn.execute('INSERT INTO settings VALUES (?,?)', ('enrich_provider_refresh:provider',
                json.dumps({'cursor': 8, 'cutoff': 1, 'fingerprint': 'old'})))
            if source_fixture == 'production64':
                conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('home_choice', json.dumps(card), 77))
            else:
                home_outputs.publish(conn, home_outputs.prepare('home_choice', json.dumps(card), 77))
        if active:
            with conn:
                metadata_runtime.install(conn)
            for stage in preparation.STAGES:
                cursor = None
                while True:
                    with conn:
                        conn.execute('BEGIN IMMEDIATE')
                        cursor, count = preparation.prepare_batch(conn, stage, cursor, 128, at=at+2)
                    if count < 128:
                        break
            with conn:
                metadata_runtime.activate(conn)
    prepare_batch = preparation.prepare_batch
    def selected_only(conn, stage, *args, **kwargs):
        assert stage not in ('providers', 'conflicts'), 'Discarded cache was converted'
        return prepare_batch(conn, stage, *args, **kwargs)
    monkeypatch.setattr(preparation, 'prepare_batch', selected_only)
    from yt_playlist.core.db_rebuild import runtime_retention
    retire = runtime_retention.retired_metadata
    def checked_retirement(reader, target, cursor, limit):
        if cursor is None:
            original = target.execute('SELECT id,number_value FROM metadata_runtime_choices WHERE kind=4').fetchall()
            target.execute('UPDATE metadata_runtime_choices SET number_value=999 WHERE kind=4')
            with pytest.raises(ValueError, match='selected manual choice'):
                verify_selected(target, source=reader if source_fixture == 'development93' else None)
            target.executemany('UPDATE metadata_runtime_choices SET number_value=? WHERE id=?',
                               [(value,identity) for identity,value in original])
        return retire(reader,target,cursor,limit)
    monkeypatch.setattr(runtime_retention,'retired_metadata',checked_retirement)
    store = Store(path)
    try:
        store.init_schema()
        report = job.run(store, rebuild=True)
        assert report['analytics_pending'] and not store.activity_paused
        assert store.rec_proposals.get_proposals('home_choice') == card
        assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=108').fetchone()[0] == 130
        # Explicit entry choices override the selected provider value without
        # rewriting that value's source in the normalized layouts.
        assert metadata_runtime.selected_source(store.conn, 108, 'bpm') == (
            'user' if source_fixture == 'production64' else 'provider')
        assert store.conn.execute('SELECT number_value FROM metadata_runtime_choices '
            'WHERE entry_id=108 AND field=?', (metadata_runtime.code('bpm'),)).fetchone()[0] == 130
        assert not store.conn.execute('SELECT 1 FROM metadata_runtime_conflicts').fetchone()
        assert not store.conn.execute('SELECT 1 FROM metadata_conflicts').fetchone()
        assert not store.conn.execute('SELECT 1 FROM metadata_conflict_candidates').fetchone()
        refresh = provider_frontiers.read(store.conn, 'provider')
        assert refresh['cursor'] == 0 and refresh['fingerprint'] == 'unchanged'
        assert provider_frontiers.read(store.conn, 'disabled') is None
        assert store.conn.execute('SELECT first_enriched_at,last_enriched_at FROM recordings WHERE id=8').fetchone()[:] == (10,10)
        assert not store.conn.execute('SELECT 1 FROM metadata_observations LIMIT 1').fetchone()
        assert not store.conn.execute('SELECT 1 FROM metadata_deliveries LIMIT 1').fetchone()
        now = time.time()+2
        store.enrichment_state.record_enrichment_attempt('song 3|artist', 'provider', {'bpm': 122}, now=now)
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            metadata_runtime.upsert_conflict(store.conn, 108, 'bpm', [{'provider': 'provider', 'value': 121}], now=now)
        job.finish_analytics(store)
        store.conn.close()
        store = Store(path)
        store.init_schema()
        assert provider_frontiers.read(store.conn, 'provider') == refresh
        store.enrichment_state.reconcile_provider_configuration(configuration)
        assert provider_frontiers.read(store.conn, 'provider') is None
        assert store.enrichment_state.enrichment_provider_jobs(8) == ['provider']
        assert not store.enrichment_state.enrichment_provider_jobs(3)
        assert store.enrichment_state.enrichment_provider_state(3, include_previous=True)['provider']['fields'] == {'bpm': 122}
        queued = store.enrichment_queue.next_enrich_batch(10, providers=['provider'], now=now+1)
        assert 8 in {track['recording_id'] for track in queued}
        assert 3 not in {track['recording_id'] for track in queued}
        assert store.rec_proposals.get_proposals('home_choice') == card
        assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=108').fetchone()[0] == 130
        conflict = metadata_runtime.conflict(store.conn, 108, 'bpm')
        assert metadata_runtime.conflict_details(store.conn, conflict, now=now)['candidates'][0]['value'] == 121
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


@pytest.mark.parametrize('converted', [0, 1, 2])
def test_prediction_lineage_gains_roots_without_losing_original_inputs(development93, converted):
    with closing(sqlite3.connect(development93)) as source:
        with source:
            source.execute('INSERT INTO measurements(id,recording_id,vibe,score,support,source,model_version,input_digest) '
                "VALUES (501,3,'relax',.5,1,'provider_measurement','basis','original')")
            source.execute('INSERT INTO measurements(id,recording_id,vibe,score,support,source,model_version,input_digest) '
                "VALUES (502,3,'relax',.6,1,'provider_measurement','basis','other-input')")
            source.execute('INSERT INTO measurements(id,recording_id,vibe,score,support,source,model_version,input_digest) '
                "VALUES (503,3,'relax',.7,1,'provider_measurement','basis','unreferenced-input')")
            source.execute('INSERT INTO predictions(id,recording_id,vibe,score,support,kind,policy_version,predicted_at,subgenre_key) '
                "VALUES (701,8,'relax',.5,1,'genre','basis',1,'house')")
            source.execute('INSERT INTO prediction_inputs VALUES (701,501)')
            source.execute('INSERT INTO predictions(id,recording_id,vibe,score,support,kind,policy_version,predicted_at,subgenre_key) '
                "VALUES (702,8,'relax',.5,1,'genre','basis',1,'house')")
            source.execute('INSERT INTO prediction_inputs VALUES (702,501)')
    store = Store(development93)
    try:
        store.init_schema()
        from yt_playlist.core import vibe_provenance
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            store.conn.execute('UPDATE prediction_id_state SET high_water=900')
            if converted:
                assert vibe_provenance.prepare_batch(store.conn, converted) == converted
                store.conn.execute('INSERT INTO prediction_measurement_roots VALUES (502)')
        report = job.run(store, rebuild=True)
        assert report['analytics_pending']
        assert vibe_provenance.read(store.conn, 701) == {'inputs': [501], 'artists': []}
        assert vibe_provenance.read(store.conn, 702) == {'inputs': [501], 'artists': []}
        assert [row[0] for row in store.conn.execute('SELECT measurement_id FROM prediction_measurement_roots')] == (
            [501,502] if converted else [501])
        with closing(sqlite3.connect(report['original'])) as original:
            original.row_factory = sqlite3.Row
            verification.predictions(original, store.conn)
            store.conn.execute('BEGIN IMMEDIATE')
            store.conn.execute('DELETE FROM prediction_measurement_roots')
            with pytest.raises(ValueError, match='measurement roots'):
                verification.predictions(original, store.conn)
            store.conn.rollback()
            store.conn.execute('BEGIN IMMEDIATE')
            store.conn.execute('INSERT INTO prediction_measurement_roots VALUES (503)')
            with pytest.raises(ValueError, match='measurement roots'):
                verification.predictions(original, store.conn)
            store.conn.rollback()
            store.conn.execute('BEGIN IMMEDIATE')
            store.conn.execute('DELETE FROM prediction_provenance')
            with pytest.raises(ValueError, match='provenance count'):
                verification.predictions(original, store.conn)
            store.conn.rollback()
            store.conn.execute('BEGIN IMMEDIATE')
            store.conn.execute("UPDATE prediction_provenance SET state='expired' WHERE prediction_id=701")
            with pytest.raises(ValueError, match='calculation provenance: 701'):
                verification.predictions(original, store.conn)
            store.conn.rollback()
        assert vibe_provenance.next_id(store.conn) == 901
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        job.finish_analytics(store)
    finally:
        store.conn.close()


def test_saved_playback_and_sync_own_new_playlist_scopes(development93):
    from yt_playlist.core import albums
    with closing(sqlite3.connect(development93)) as source:
        source.row_factory = sqlite3.Row
        build._configure(source)
        with source:
            albums.observe(source, 'original-album')
            settings = {
                'listening_playback_bindings': [{'identity_id': 11, 'playlist': 'PL-binding', 'video': 'video-8',
                    'opportunity_id': 'original', 'option_id': 'video-8', 'at': 17}],
                'listening_album_pending': {'identity_id': 11, 'opportunity_id': 'album', 'browse': 'original-album',
                    'playlist': 'PL-album', 'at': 18, 'videos': ['video-3', 'video-8']},
                'library_sync_state': {'status': 'partial', 'accounts': {'11': {'status': 'partial', 'resources': {'playlists': {
                    'status': 'partial', 'items': {'PL-sync': {'status': 'complete'}}}}}}}}
            source.executemany('INSERT INTO settings VALUES (?,?)', [(key,json.dumps(value)) for key,value in settings.items()])
    store = Store(development93)
    try:
        store.init_schema()
        report = job.run(store, rebuild=True)
        for table,scope in [('listening_playback_bindings', 'PL-binding'),
                            ('album_playback_intents', 'PL-album'), ('sync_cursors', 'PL-sync')]:
            assert store.conn.execute('SELECT 1 FROM '+table+' WHERE playlist_scope_id=?', (scope,)).fetchone()
        with closing(sqlite3.connect(report['original'])) as original:
            original.row_factory = sqlite3.Row
            verification.converted(original, store.conn)
            store.conn.execute('BEGIN IMMEDIATE')
            store.conn.execute("INSERT INTO playlist_scopes(id,provider_playlist_id,kind) VALUES ('invented','invented','shared')")
            with pytest.raises(ValueError, match='unowned playlist scope'):
                verification.converted(original, store.conn)
            store.conn.rollback()
        job.finish_analytics(store)
    finally:
        store.conn.close()


@pytest.mark.parametrize(('mode', 'restart'), [
    ('existing_before', False), ('new_source', False), ('existing_during', False),
    ('existing_before', True), ('new_source', True)])
def test_foreground_provider_results_survive_analytics_and_restart(development93, mode, restart):
    from yt_playlist.core.storage_maintenance import StorageMaintenance
    with closing(sqlite3.connect(development93)) as source:
        source.row_factory = sqlite3.Row
        build._configure(source)
        with source:
            source.execute('BEGIN IMMEDIATE')
            metadata_native.record(source, 108, 'native', [MetadataObservation(
                field, value, 'recording', 'native-id', 'prediction', unit='classifier_probability')
                for field, value in [('mood_party', .2), ('energy', .4)]],
                run_id='original', now=17, recording_id=8)
        old_key, old_revision = source.execute('SELECT source_key,revision FROM rec_musical_sources').fetchone()
    store = Store(development93)
    try:
        store.init_schema()
        first = job.run(store, rebuild=True)
        original_document = metadata_runtime_files.read(store.conn, 'native', (old_key,), old_revision)
        entity = 'new-native-id' if mode == 'new_source' else 'native-id'
        key = json.dumps(['native', 'recording', entity], separators=(',', ':'))
        def lookup():
            assert not store.activity_paused
            store.enrichment_state.record_metadata_observations(108, 'native', [MetadataObservation(
                'mood_party', .91, 'recording', entity, 'prediction', unit='classifier_probability')],
                run_id='foreground-lookup', now=30)
            assert metadata_native.source_values(store.conn, key)['mood_party']['value'] == .91
        if mode != 'existing_during':
            lookup()
        def progress(phase, detail):
            if detail['stage'] == 'served-facts':
                if mode == 'existing_during':
                    lookup()
                if restart:
                    raise InterruptedError('after accepted foreground result')
        if restart:
            with pytest.raises(InterruptedError, match='after accepted foreground result'):
                job.finish_analytics(store, progress=progress)
            store.conn.close()
            store = Store(development93)
            store.init_schema()
            maintenance = StorageMaintenance(store)
            try:
                maintenance.start()
                maintenance.join(30)
                assert maintenance.phase == 'done', maintenance.error
                assert maintenance.ready.is_set()
                assert maintenance.report['id'] == first['id']
            finally:
                maintenance.stop()
                maintenance.join(30)
        else:
            job.finish_analytics(store, progress=progress)
        assert not db_handles.state(development93)['analytics_pending']
        assert metadata_native.source_values(store.conn, key)['mood_party']['value'] == .91
        assert store.conn.execute('SELECT run_id FROM metadata_native_delivery_heads '
                                  'WHERE source_key=?', (key,)).fetchone()[0] == 'foreground-lookup'
        assert metadata_runtime_files.read(store.conn, 'native', (old_key,), old_revision) == original_document
        if mode != 'new_source':
            assert metadata_native.source_values(store.conn, key)['energy']['value'] == .4
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


def test_released_recommendations_and_learning_inputs_are_usable_at_runtime_cutover(production64):
    content = json.dumps({'library': {'genre': 'Synth Pop', 'year': '2001', 'audio': {'bpm': 123.5}},
                          'discovered': None})
    card = {'schema': 1, 'protos': [{'label': 'Keep listening', 'tracks': [
        {'video_id': 'video-8', 'identity_key': 'song 8|artist'}]}]}
    with closing(sqlite3.connect(production64)) as source:
        with source:
            source.execute("INSERT INTO rec_weights VALUES ('genre:Synth Pop',1.25,10)")
            source.execute('INSERT INTO rec_prepared_content VALUES (?,?,?,?,?)',
                ('song 8|artist', content, hashlib.sha256(content.encode()).hexdigest(), 12, 7))
            source.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('home_choice', json.dumps(card), 77))
    store = Store(production64)
    try:
        store.init_schema()
        report = job.run(store, rebuild=True)
        assert report['analytics_pending'] and not store.activity_paused
        assert store.rec_proposals.get_proposals('home_choice') == card
        assert store.conn.execute('SELECT weight FROM rec_weights').fetchone()[0] == 1.25
        assert tuple(store.conn.execute('SELECT recording_id,library_bpm,input_revision '
                                        'FROM prepared_recording_inputs').fetchone()) == (8, 123.5, 7)
        store.settings.set_setting('normal-use', 'accepted')
        job.finish_analytics(store)
        assert store.rec_proposals.get_proposals('home_choice') == card
        assert store.settings.get_setting('normal-use') == 'accepted'
    finally:
        store.conn.close()


def test_missing_diagnostic_archive_does_not_block_catalog_or_recommendations(development93):
    store = Store(development93)
    try:
        store.init_schema()
        prepared = home_outputs.prepare('home_choice', json.dumps({
            'schema': 1, 'protos': [{'label': 'Keep listening', 'tracks': [
                {'video_id': 'video-8', 'identity_key': 'song 8|artist'}]}]}), 77)
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            home_outputs.publish(store.conn, prepared, store=store, input_revision=23)
            # This obsolete receipt points to a body that is already gone.
            # Current catalog, ratings and recommendation output remain useful.
            store.conn.execute('INSERT INTO album_identity_evidence('
                'kind,source_id,reason,filename,content_digest,byte_size,file_offset,format_version,'
                'archived_at,resolution_status) VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('album_legacy:test', 'old', 'obsolete diagnostic', '/missing/old-album.json',
                 'a' * 64, 1, 0, 'legacy-evidence-1', 1, 'unresolved'))
            catalog.choose_field(store.conn, 108, 'bpm', 131, now=19)
        store.settings.set_setting('personal-preference', 'keep me')
        ratings = [tuple(row) for row in store.conn.execute('SELECT * FROM item_ratings')]
        history = [tuple(row) for row in store.conn.execute(
            'SELECT id,account_id,recording_id FROM play_events')]
        members = store.playlists.get_playlist_track_ids(41)
        report = job.run(store, rebuild=True)
        assert report['analytics_pending']
        for phase in ('runtime', 'analytics'):
            assert not store.activity_paused
            assert home_outputs.encoded(store.conn, 'home_choice') == prepared.raw
            assert store.playlists.get_playlist_track_ids(41) == members
            assert [tuple(row) for row in store.conn.execute('SELECT * FROM item_ratings')] == ratings
            assert [tuple(row) for row in store.conn.execute(
                'SELECT id,account_id,recording_id FROM play_events')] == history
            assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=108').fetchone()[0] == 131
            assert store.settings.get_setting('personal-preference') == 'keep me'
            assert not store.conn.execute('SELECT 1 FROM album_identity_evidence').fetchone()
            store.settings.set_setting('normal-use', phase)
            if phase == 'runtime':
                job.finish_analytics(store)
        assert not db_handles.state(development93)['analytics_pending']
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


@pytest.mark.parametrize('source_fixture', ['production64', 'development93'])
def test_runtime_reopens_before_analytics_and_preserves_new_edits(request, source_fixture):
    path = request.getfixturevalue(source_fixture)
    if source_fixture == 'development93':
        with closing(sqlite3.connect(path)) as source:
            source.row_factory = sqlite3.Row
            build._configure(source)
            with source:
                source.execute('BEGIN IMMEDIATE')
                metadata_native.record(source, 108, 'native', [MetadataObservation(
                    'mood_party', .8, 'recording', 'native-id', 'prediction',
                    unit='classifier_probability')], run_id='old-native', now=17, recording_id=8)
                source.execute('INSERT INTO recording_embeddings VALUES (?,?,?,?,?,?)',
                    (8, 'collaborative', 'old-model', 2, struct.pack('=ff', .2, .7), 17))
            key = source.execute('SELECT source_key FROM rec_musical_sources LIMIT 1').fetchone()[0]
            expected = metadata_native.source_values(source, key)
    store = Store(path)
    try:
        store.init_schema()
        report = job.run(store, rebuild=True)
        assert report['phase'] == 'complete' and report['analytics_pending']
        assert not store.activity_paused
        assert metadata_runtime.active(store.conn)
        assert store.conn.execute('SELECT 1 FROM '+build.CHECKPOINT).fetchone()
        assert not store.conn.execute('SELECT 1 FROM native_metadata_observations LIMIT 1').fetchone()
        assert not store.conn.execute('SELECT 1 FROM recording_embeddings LIMIT 1').fetchone()
        assert not (path.parent / 'verified-original.db').exists()
        if source_fixture == 'development93':
            assert metadata_native.source_values(store.conn, key) == expected
        store.settings.set_setting('between-phases', 'accepted')
        with store.conn:
            catalog.choose_field(store.conn, 108, 'bpm', 131, now=19)
        phases = []
        def progress(phase, detail):
            assert not store.activity_paused
            assert store.settings.get_setting('between-phases') == 'accepted'
            assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=108').fetchone()[0] == 131
            phases.append(phase)
        result = job.finish_analytics(store, progress=progress)
        assert phases and set(phases) == {'analytics'}
        assert not result['analytics_pending']
        assert not db_handles.state(path)['analytics_pending']
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (build.CHECKPOINT,)).fetchone()
        if source_fixture == 'development93':
            assert metadata_native.source_values(store.conn, key) == expected
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


def test_interrupted_analytics_resumes_after_restart_without_rebuilding_runtime(production64, monkeypatch):
    from yt_playlist.core.db_rebuild import destination
    from yt_playlist.core.storage_maintenance import StorageMaintenance
    store = Store(production64)
    try:
        store.init_schema()
        first = job.run(store, rebuild=True)
        store.settings.set_setting('accepted-after-cutover', 'keep me')
        def interrupted(phase, detail):
            if detail['stage'] == 'served-facts':
                raise InterruptedError('committed analytics checkpoint')
        with pytest.raises(InterruptedError, match='committed analytics checkpoint'):
            job.finish_analytics(store, progress=interrupted)
        assert not store.activity_paused
        assert db_handles.state(production64)['analytics_pending']
    finally:
        store.conn.close()
    store = Store(production64)
    maintenance = StorageMaintenance(store)
    try:
        store.init_schema()
        # Unrelated application edits change the broad construction digest,
        # while the installed schema and saved analytics stage plan still fit.
        monkeypatch.setattr(destination, 'signature', lambda: 'unrelated-application-edit')
        maintenance.start()
        maintenance.join(15)
        assert maintenance.phase == 'done', maintenance.error
        assert maintenance.ready.is_set()
        assert maintenance.report['id'] == first['id']
        assert store.settings.get_setting('accepted-after-cutover') == 'keep me'
        assert not maintenance.report['analytics_pending']
    finally:
        maintenance.stop()
        maintenance.join(15)
        store.conn.close()


def test_analytics_rejects_changed_stage_plan_without_touching_its_checkpoint(production64, monkeypatch):
    from yt_playlist.core.db_rebuild import destination
    store = Store(production64)
    try:
        store.init_schema()
        job.run(store, rebuild=True)
        before = store.conn.execute('SELECT document FROM '+build.CHECKPOINT).fetchone()[0]
        for_source = destination.for_source

        def changed(*args, **kwargs):
            spec = for_source(*args, **kwargs)
            return replace(spec, stages=spec.stages[:-2] + tuple(reversed(spec.stages[-2:])))

        monkeypatch.setattr(destination, 'for_source', changed)
        with pytest.raises(ValueError, match='checkpoint belongs to another rebuild'):
            job.finish_analytics(store)
        assert store.conn.execute('SELECT document FROM '+build.CHECKPOINT).fetchone()[0] == before
        assert db_handles.state(production64)['analytics_pending']
        assert not store.activity_paused
        store.settings.set_setting('after-analytics-error', 'accepted')
    finally:
        store.conn.close()


def test_analytics_serves_foreground_writes_between_batches(production64, monkeypatch):
    from yt_playlist.core.db_rebuild import destination
    store = Store(production64)
    entered, queued, completed = threading.Event(), threading.Event(), threading.Event()
    failures = []
    writer = None
    try:
        store.init_schema()
        job.run(store, rebuild=True)
        # Cooperative priority waits are deliberately bounded. Even if a
        # waiting request outlives that timeslice, SQL writer admission must
        # not let the analytics connection overtake its pending write.
        monkeypatch.setattr(store.priority, 'checkpoint', lambda cancelled: None)
        members = store.playlists.get_playlist_track_ids(41)
        original = destination.for_source
        batches = 0
        def planned(*args, **kwargs):
            plan = original(*args, **kwargs)
            def copy(callback):
                def checked(*args):
                    nonlocal batches
                    batches += 1
                    if batches == 1:
                        entered.set()
                        assert queued.wait(5), 'Foreground read was blocked by the analytics writer'
                        assert not completed.wait(.05), 'Analytics did not own the first write transaction'
                    else:
                        assert completed.is_set(), 'Analytics took another batch before serving the queued write'
                    return callback(*args)
                return checked
            return replace(plan, stages=tuple(replace(stage, copy=copy(stage.copy))
                if index >= plan.runtime_stages else stage for index,stage in enumerate(plan.stages)))
        monkeypatch.setattr(destination, 'for_source', planned)
        def foreground():
            try:
                assert entered.wait(5)
                # Normal read admission uses a WAL reader while the private
                # writer owns serving write admission. Then queue a real edit.
                assert store.playlists.get_playlist_track_ids(41) == members
                queued.set()
                store.settings.set_setting('while-analytics-writes', 'accepted')
                completed.set()
            except BaseException as error:
                failures.append(error)
        writer = threading.Thread(target=foreground)
        writer.start()
        result = job.finish_analytics(store)
        writer.join(5)
        assert not writer.is_alive()
        assert not failures
        assert batches > 1 and completed.is_set()
        assert store.settings.get_setting('while-analytics-writes') == 'accepted'
        assert not result['analytics_pending']
    finally:
        if writer is not None:
            writer.join(6)
        store.conn.close()


@pytest.mark.parametrize('commit_phase', ['batch', 'completion'])
def test_slow_analytics_commit_queues_edits_without_blocking_browsing(production64, monkeypatch, commit_phase):
    from yt_playlist.core.db_diagnostics import DiagnosticConnection
    store = Store(production64)
    committing, release, written = threading.Event(), threading.Event(), threading.Event()
    failures, connections = [], set()
    analytics = writer = None
    checkpoint, finish = build._checkpoint, DiagnosticConnection._finish
    held = False

    def marked(conn, value=None):
        if value is not None:
            connections.add(conn)
        return checkpoint(conn, value)

    def delayed(conn, sql, target, *args):
        nonlocal held
        complete = conn in connections and not conn.execute(
            'SELECT 1 FROM sqlite_schema WHERE name=?', (build.CHECKPOINT,)).fetchone()
        if (not held and conn in connections and sql == 'COMMIT' and conn.in_transaction
                and (commit_phase == 'batch' or complete)):
            held = True
            committing.set()
            assert release.wait(10)
        return finish(conn, sql, target, *args)

    def complete():
        try:
            job.finish_analytics(store)
        except BaseException as error:
            failures.append(error)

    def edit():
        try:
            store.settings.set_setting('queued-through-durability', 'accepted')
            written.set()
        except BaseException as error:
            failures.append(error)

    try:
        store.init_schema()
        job.run(store, rebuild=True)
        members = store.playlists.get_playlist_track_ids(41)
        # A real foreground INSERT must queue outside SQLite even if durability
        # exceeds that connection's deliberately short test-only busy timeout.
        store.conn.execute('PRAGMA busy_timeout=25')
        monkeypatch.setattr(build, '_checkpoint', marked)
        monkeypatch.setattr(DiagnosticConnection, '_finish', delayed)
        analytics = threading.Thread(target=complete)
        analytics.start()
        assert committing.wait(10)
        with closing(sqlite3.connect(store._db_path, timeout=0)) as probe:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                probe.execute('BEGIN IMMEDIATE')
        assert store.playlists.get_playlist_track_ids(41) == members
        writer = threading.Thread(target=edit)
        writer.start()
        assert not written.wait(.15)
        assert not failures
        release.set()
        writer.join(10)
        analytics.join(20)
        assert not writer.is_alive() and not analytics.is_alive()
        assert not failures and written.is_set()
        assert store.settings.get_setting('queued-through-durability') == 'accepted'
        assert not db_handles.state(store._db_path)['analytics_pending']
        store.conn.close()
        store = Store(production64)
        store.init_schema()
        assert store.settings.get_setting('queued-through-durability') == 'accepted'
    finally:
        release.set()
        for thread in (writer, analytics):
            if thread is not None:
                thread.join(20)
        store.conn.close()


def test_workers_wait_for_analytics_while_runtime_accepts_writes(production64, monkeypatch):
    from yt_playlist.core.storage_maintenance import StorageMaintenance
    entered, release = threading.Event(), threading.Event()
    finish = job.finish_analytics
    def delayed(store, **kwargs):
        entered.set()
        assert release.wait(15)
        return finish(store, **kwargs)
    monkeypatch.setattr(job, 'finish_analytics', delayed)
    store = Store(production64)
    maintenance = StorageMaintenance(store)
    try:
        store.init_schema()
        maintenance.start()
        assert entered.wait(15), maintenance.error
        assert not maintenance.ready.is_set()
        assert not maintenance.status()['active']
        store.settings.set_setting('before-worker-start', 'accepted')
        release.set()
        maintenance.join(15)
        assert maintenance.phase == 'done', maintenance.error
        assert maintenance.ready.is_set()
        assert store.settings.get_setting('before-worker-start') == 'accepted'
    finally:
        release.set()
        maintenance.stop()
        maintenance.join(15)
        store.conn.close()
