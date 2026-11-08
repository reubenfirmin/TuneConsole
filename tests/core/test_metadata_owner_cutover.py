"""Retiring overlapping owners preserves the installed catalog and recovery."""
import json
from tests.enrichment_support import choose_field
from tests.metadata_provenance_support import metadata_provenance
import sqlite3
import threading
import time

import pytest

from yt_playlist.core import catalog, metadata_deliveries, metadata_native
from yt_playlist.core.migrations import current, migrate, registry, regular, v080_metadata_evidence as migration
from yt_playlist.core.migrations.runner import Context, MigrationError
from yt_playlist.core.store import Store
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.providers.base import MetadataObservation


@pytest.fixture
def predecessor(tmp_path, metadata_predecessor_schema, monkeypatch):
    from tests.genre_catalog_support import bound_catalog
    from tests.store_fixture_support import reuse_reporting_reader
    from yt_playlist.core import schema_inspection

    bound_catalog(monkeypatch, entries=(('rock', 'Rock'), ('jazz', 'Jazz'), ('blues', 'Blues')))
    store = Store(tmp_path / 'before-evidence.db')
    try:
        # These private SQL fixtures exercise conversion and publication,
        # without requiring power-loss durability of their disposable input.
        store.conn.execute('PRAGMA synchronous=OFF')
        metadata_predecessor_schema.backup(store.conn)
        assert store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        store._bind_repositories()
        with reuse_reporting_reader(store, monkeypatch), schema_inspection.reuse_tables(store.conn):
            yield store
    finally:
        store.conn.close()


def seed(store, *, title='Song'):
    entry = store.tracks.upsert_track('video', title, 'Artist', None, 200)
    recording, key = store.conn.execute('SELECT r.id,r.identity_key FROM catalog_entries e '
        'JOIN recordings r ON r.id=e.recording_id WHERE e.id=?', (entry,)).fetchone()
    fields = {'bpm': 120, 'genre': 'Jazz', 'year': '2000'}
    store.enrichment.apply_provider_fields(entry, 'a', fields, providers=('a',))
    for field, value in {**fields, 'isrc': ['ONE']}.items():
        store.enrichment_state.log_enrichment(entry, 'original', 'a', field, value, now=10)
    store.enrichment_state.record_enrichment_attempt(key, 'a', fields, video_id='video', now=10)
    choose_field(store, entry, 'bpm', None)
    choose_field(store, entry, 'genre', '')
    observation = MetadataObservation('tags', [{'name': 'Jazz', 'count': 4}], 'artist', 'artist',
        'reported', unit='provider_tag_count_or_order', match_evidence={'query_kind': 'artist'})
    store.enrichment_state.record_metadata_observations(entry, 'native', [observation], run_id='native-original', now=11)
    with store.conn:
        store.conn.execute("INSERT INTO catalog_field_hints VALUES (?,'genre','import','\"Blues\"',1)", (entry,))
        for field, status, scope, selected in [('genre', 0, 'recording', None),
                                              ('bpm', 1, 'recording', '130'), ('year', 2, 'release', None)]:
            candidates = [{'provider': 'a', 'value': fields[field]},
                          {'provider': 'b', 'value': 130 if field == 'bpm' else 'Other'}]
            store.conn.execute('INSERT INTO enrichment_conflict(track_id,field,candidates,resolved,resolved_value,'
                'updated_at,recording_id,input_video_id,field_scope) VALUES (?,?,?,?,?,10,?,?,?)',
                (entry, field, json.dumps(candidates), status, selected, recording, 'video', scope))
    return entry, recording, key


def convert(store):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        migration.apply(store.conn, Context(4))
        return migration.convert_legacy(store.conn)


def test_startup_retains_metadata_owners_and_binds_installed_revision_triggers(predecessor, monkeypatch):
    store = predecessor
    entry, _, _ = seed(store)
    before = dict(store.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone())
    def checked_conversion(conn):
        pytest.fail('Routine startup invoked the historical bulk converter')

    monkeypatch.setattr(migration, 'convert_legacy', checked_conversion)
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        assert reopened._schema_compatibility and not reopened.activity_paused
        assert reopened.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
        assert reopened.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_findings'").fetchone()
        assert not reopened.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_observations'").fetchone()
        assert dict(reopened.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()) == before
        trigger = reopened.conn.execute("SELECT sql FROM sqlite_temp_schema "
            "WHERE name='snapshot_revision_rec_musical_sources_update'").fetchone()
        assert trigger is not None and 'payload' in trigger[0]
        revisions = reopened._snapshot_revisions
        with reopened.conn:
            reopened.conn.execute('UPDATE rec_musical_sources SET revision=revision+1')
        assert revisions.capture()[('rec_musical_sources', '')] > 0
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        reopened.conn.close()


def test_null_blank_and_withdrawal_remain_distinct_from_display_formatting(predecessor):
    store = predecessor
    entry, _, _ = seed(store)
    # Formatting/withdrawal owns this actual metadata conversion edge; the
    # registered upgrade test owns continuing through later schema versions.
    convert(store)
    for at, value in ((20, None), (21, '')):
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            catalog.choose_field(store.conn, entry, 'genre', value, now=at)
        assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == value
        assert store.tracks.get_track_enrichment(entry)[0] == ''
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        catalog.withdraw_choice(store.conn, entry, 'genre', now=22)
    assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 'Jazz'


def test_deferred_startup_installs_receipts_without_advancing_committed_metadata_history(predecessor):
    store = predecessor
    seed(store)
    migrate(store.conn, registry=registry.MIGRATIONS[:80])
    reopened = Store(store._db_path)
    maintenance = StorageMaintenance(reopened)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened._schema_compatibility and not reopened.activity_paused
        assert reopened.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 80
        maintenance.start()
        maintenance.join(20)
        assert not reopened._schema_recovery_pending
        assert reopened.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 80
        assert list(map(tuple, reopened.conn.execute('SELECT version,name FROM schema_changes ORDER BY version'))) == [
            (change.version,change.name) for change in regular.CHANGES]
    finally:
        maintenance.stop()
        maintenance.join(20)
        reopened.conn.close()


def test_suspended_regular_ddl_keeps_predecessor_writable(predecessor, monkeypatch):
    from yt_playlist.core.migrations import regular
    store = predecessor
    entry, _, _ = seed(store)
    before = dict(store.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone())
    entered, release = threading.Event(), threading.Event()
    apply = regular.CHANGES[0].apply

    def held_ddl(conn, context):
        entered.set()
        assert release.wait(10)
        return apply(conn, context)

    change = regular.CHANGES[0]
    monkeypatch.setattr(regular, 'CHANGES', (regular.Change(change.version, change.name, held_ddl, change.validate),))
    reopened = Store(store._db_path)
    reopened.init_schema(defer_metadata=True)
    assert reopened._schema_compatibility and not reopened.activity_paused
    assert reopened.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
    maintenance = StorageMaintenance(reopened)
    try:
        maintenance.start()
        assert entered.wait(5)
        started = time.monotonic()
        assert not maintenance.status()['active']
        with reopened.read_view() as reader:
            assert dict(reader.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()) == before
        assert time.monotonic() - started < 2
        assert not maintenance.ready.is_set()
        release.set()
        maintenance.join(20)
        assert maintenance.ready.is_set() and not reopened.activity_paused
        assert reopened.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
        choose_field(reopened, entry, 'genre', 'Blues')
        assert reopened.tracks.get_track_enrichment(entry)[0] == 'Blues'
        trigger = reopened.conn.execute("SELECT sql FROM sqlite_temp_schema "
            "WHERE name='snapshot_revision_rec_musical_sources_update'").fetchone()
        assert trigger is not None and 'payload' in trigger[0]
        with reopened.conn:
            reopened.conn.execute('UPDATE rec_musical_sources SET revision=revision+1')
        assert reopened._snapshot_revisions.capture()[('rec_musical_sources', '')] > 0
    finally:
        release.set()
        maintenance.stop()
        maintenance.join(20)
        reopened.conn.close()


def test_failed_regular_ddl_does_not_pause_or_automatically_retry(predecessor, monkeypatch):
    from yt_playlist.core.migrations import regular
    store = predecessor
    entry, _, _ = seed(store)
    before = dict(store.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone())
    reopened = Store(store._db_path)
    reopened.init_schema(defer_metadata=True)
    maintenance = StorageMaintenance(reopened)
    original = regular.CHANGES
    attempts = []
    def interrupted(conn, context):
        attempts.append(1)
        raise RuntimeError('controlled independent DDL failure')
    change = original[0]
    monkeypatch.setattr(regular, 'CHANGES', (regular.Change(change.version, change.name, interrupted, change.validate),))
    try:
        maintenance.start()
        maintenance.join(20)
        assert attempts == [1]
        assert maintenance.ready.is_set() and maintenance.phase == 'schema_retry'
        assert not reopened.activity_paused
        reopened.settings.set_setting('after-ddl-failure', 'still writable')
        assert reopened.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
        assert dict(reopened.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()) == before
    finally:
        maintenance.stop()
        maintenance.join(20)
        reopened.conn.close()
    monkeypatch.setattr(regular, 'CHANGES', original)
    resumed = Store(store._db_path)
    resumed.init_schema(defer_metadata=True)
    maintenance = StorageMaintenance(resumed)
    try:
        assert not resumed.activity_paused
        maintenance.start()
        maintenance.join(20)
        assert not resumed.activity_paused
        assert resumed.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
        assert list(map(tuple, resumed.conn.execute('SELECT version,name FROM schema_changes ORDER BY version'))) == [
            (change.version,change.name) for change in regular.CHANGES]
        assert dict(resumed.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()) == before
        assert not resumed.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        maintenance.stop()
        maintenance.join(20)
        resumed.conn.close()


def test_pre_cutover_snapshot_cannot_publish_after_background_commit(predecessor):
    seed(predecessor)
    reopened = Store(predecessor._db_path)
    reopened.init_schema(defer_metadata=True)
    maintenance = StorageMaintenance(reopened)
    published = []
    try:
        with reopened.read_snapshot() as stale:
            assert stale.store.tracks.identity_key_for_video('video')
            maintenance.start()
            maintenance.join(20)
            assert not reopened._schema_recovery_pending
            assert not stale.publish(lambda: published.append('stale'))
        assert not published
        with reopened.read_snapshot() as fresh:
            assert fresh.store.tracks.identity_key_for_video('video')
            assert fresh.publish(lambda: published.append('fresh'))
        assert published == ['fresh']
    finally:
        maintenance.stop()
        maintenance.join(20)
        reopened.conn.close()


@pytest.mark.parametrize('index', ['ix_group_library_inputs', 'ix_entry_artist_subject'])
def test_predecessor_validation_rejects_missing_required_indexes_before_cutover(predecessor, index):
    store = predecessor
    seed(store)
    with store.conn:
        store.conn.execute(f'DROP INDEX {index}')
    with pytest.raises(MigrationError, match=index):
        migrate(store.conn)
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
    assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='metadata_observations'").fetchone()


def test_cutover_preserves_values_native_projection_and_every_original_owner(predecessor):
    store = predecessor
    entry, recording, key = seed(store)
    before = dict(store.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone())
    native_before, = store.enrichment_state.metadata_observations(key)
    counts = convert(store)
    assert dict(store.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()) == before
    assert counts == {'metadata_findings': 4, 'enrichment_log': 4, 'metadata_decisions': 2,
                      'entry_field_choices': 2, 'enrichment_conflict': 3, 'provider_attempts': 1,
                      'enrichment_provider_state': 1, 'recording_identifiers': 1, 'catalog_field_hints': 1}
    for owner, count in counts.items():
        captures = store.conn.execute('SELECT legacy_id,delivery_id FROM metadata_legacy_records WHERE legacy_owner=?',
                                       (owner,)).fetchall()
        assert len(captures) == count
        for original_id, delivery in captures:
            body = metadata_deliveries.response(store.conn, delivery)
            assert body['owner'] == owner and body['format'] == 'legacy-metadata-record-1'
    native_after, = store.enrichment_state.metadata_observations(key)
    assert native_after['value'] == native_before['value']
    assert native_after['provenance'] == native_before['provenance']
    source, = store.conn.execute('SELECT source_key FROM rec_musical_sources').fetchone()
    assert metadata_native.source_values(store.conn, source)['tags']['value'] == native_before['value']
    assert list(map(tuple, store.conn.execute('SELECT field,status FROM metadata_conflicts ORDER BY field'))) == [
        ('bpm', 'user_resolved'), ('genre', 'unresolved'), ('mb_year', 'provider_converged')]
    for table in ('entry_field_choices', 'metadata_decisions', 'catalog_field_hints', 'recording_field_sources',
                  'item_field_sources', 'enrichment_log', 'enrichment_conflict', 'metadata_findings'):
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()
    for table in ('provider_attempts', 'enrichment_provider_state'):
        assert 'fields_json' not in {row[1] for row in store.conn.execute(f'PRAGMA table_info({table})')}
    with pytest.raises(sqlite3.OperationalError, match='view'):
        store.conn.execute('DELETE FROM entry_field_choices')
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        catalog.withdraw_choice(store.conn, entry, 'genre', now=30)
    assert store.tracks.get_track_enrichment(entry)[0] == 'Jazz'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_current_source_without_matching_retained_evidence_aborts_the_entire_cutover(predecessor):
    store = predecessor
    seed(store)
    with store.conn:
        store.conn.execute("UPDATE rec_musical_sources SET payload='{}'")
    before = list(store.conn.iterdump())
    with pytest.raises(MigrationError, match='source projection differs'):
        convert(store)
    assert list(store.conn.iterdump()) == before


def test_interruption_after_retirement_restores_all_old_owners_and_values(predecessor, monkeypatch):
    store = predecessor
    seed(store)
    before = list(store.conn.iterdump())
    retire = migration.retire_legacy_owners

    def interrupted(conn):
        retire(conn)
        raise RuntimeError('interrupted after metadata retirement')

    monkeypatch.setattr(migration, 'retire_legacy_owners', interrupted)
    with pytest.raises(RuntimeError, match='interrupted after metadata retirement'):
        convert(store)
    assert list(store.conn.iterdump()) == before
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 79
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
