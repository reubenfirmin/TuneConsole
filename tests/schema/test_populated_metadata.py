"""Opt-in metadata conversion on a copied SQL/metadata cohort, never the source."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

import pytest

from tests.schema.test_populated_groups import storage
from yt_playlist.core import catalog, metadata_deliveries, metadata_delivery_files, metadata_legacy
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import migrate, registry, v080_metadata_evidence as migration
from yt_playlist.core.migrations.runner import Context, validate_version
from yt_playlist.core.store import Store
from yt_playlist.providers.waterfall import run_waterfall


OWNERS = ('metadata_findings', 'enrichment_log', 'metadata_decisions', 'entry_field_choices',
          'enrichment_conflict', 'provider_attempts', 'enrichment_provider_state',
          'recording_identifiers', 'catalog_field_hints')


def source_fingerprints(request):
    paths = ('core/metadata_legacy.py', 'core/metadata_attempts.py', 'core/metadata_evidence.py',
             'core/metadata_conflicts.py', 'core/migrations/v080_metadata_evidence.py',
             'core/migrations/v081_playback_feedback.py', 'core/migrations/schema_playback_feedback.sql',
             'providers/waterfall.py', 'core/store.py', 'core/storage_maintenance.py',
             'core/db_revisions.py', 'core/playback_feedback.py', 'core/migrations/runner.py',
             '__main__.py', 'web/app.py', 'web/routes/home.py', 'web/routes/bridge.py', 'web/routes/likes.py',
             'web/radio_session.py', 'web/radio_playback.py', 'library/ratings.py', 'library/ops.py', 'rec/onboarding.py',
             'rec/artist_start.py', 'rec/account_growth.py', 'rec/candidate_refresh.py',
             'repos/base.py', 'repos/tracks.py',
             'repos/musical_profiles.py', 'repos/collection.py', 'repos/rec_ledger.py',
             'repos/rec_proposals.py', 'repos/rec_model.py', 'repos/modes.py',
             'repos/rec_candidates.py', 'repos/discovery.py', 'repos/playlists.py')
    root = Path(request.config.rootpath)
    return {name: hashlib.sha256((root / 'src/yt_playlist' / name).read_bytes()).hexdigest()
            for name in paths}


def file_bytes(root):
    return sum(path.stat().st_size for path in root.rglob('*') if path.is_file())


def counts(conn):
    return {name: conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
            for name in ('metadata_deliveries', 'metadata_observations', 'provider_attempts',
                         'metadata_conflicts', 'metadata_conflict_candidates')}


def verify_continuation(store, source, new_root, save, *, run_prefix='actual'):
    """Validate originals and continue writes without repeating a completed cutover."""
    checked = {}
    for owner in OWNERS:
        checked[owner] = 0
        for ordering in ('ASC', 'DESC'):
            row = source.execute(f'SELECT rowid AS _legacy_id,* FROM {owner} ORDER BY rowid {ordering} LIMIT 1').fetchone()
            if row is None:
                continue
            captured = store.conn.execute('SELECT delivery_id FROM metadata_legacy_records '
                'WHERE legacy_owner=? AND legacy_id=?', (owner, row['_legacy_id'])).fetchone()
            expected = {key: row[key] for key in row.keys() if key != '_legacy_id'
                        and key not in metadata_legacy.ADDED_COLUMNS.get(owner, ())}
            assert metadata_deliveries.response(store.conn, captured[0]) == {
                'format': metadata_legacy.FORMAT, 'owner': owner, 'row': expected}
            checked[owner] += 1
    # Current catalog inputs are real. Provider replies are controlled and make
    # no network requests; independent replies retain distinct delivery IDs.
    from tests.test_waterfall import FakeProv, _cfg
    inputs = store.conn.execute('SELECT id FROM catalog_entries WHERE video_id IS NOT NULL ORDER BY id LIMIT 16').fetchall()
    tracks = [dict(store.tracks.track_for_waterfall(row[0])) for row in inputs]
    tracks = [{**track, 'thumbnail': 'https://example.test/cover.jpg', 'provider_jobs': ['a', 'b']}
              for track in tracks]
    assert len(tracks) == 16
    a = FakeProv('a', {track['title']: {'genre': 'Jazz', 'bpm': 120} for track in tracks})
    b = FakeProv('b', {track['title']: {'genre': 'Rock', 'bpm': 130} for track in tracks})
    configuration, arguments = _cfg(('a', True), ('b', True)), {'registry': {'a': a, 'b': b}}
    growth = []
    for suffix, reuse in (('original', False), ('replay', True), ('independent', False)):
        run_id = run_prefix + '-' + suffix
        completed = run_waterfall(store, tracks, configuration, lambda _: None,
            run_id=run_id, now_fn=lambda: 1793289600, reuse_completed=reuse, **arguments)
        assert len(completed) == len(tracks)
        growth.append({'run': run_id, **counts(store.conn), 'file_bytes': file_bytes(new_root)})
    assert growth[0]['metadata_deliveries'] == growth[1]['metadata_deliveries']
    assert growth[0]['metadata_observations'] == growth[1]['metadata_observations']
    assert growth[2]['metadata_deliveries'] > growth[1]['metadata_deliveries']
    entry = tracks[0]['id']
    for at, value in ((1793289601, None), (1793289602, '')):
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            catalog.choose_field(store.conn, entry, 'genre', value, now=at)
        assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == value
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        catalog.withdraw_choice(store.conn, entry, 'genre', now=1793289603)
    assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] != ''
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    save(original_samples_checked=checked, repeated_enrichment_growth=growth,
         final_bytes=storage(store.conn), final_file_bytes=file_bytes(new_root), status='passed')


def test_actual_metadata_startup_from_retained_predecessor(request, tmp_path, monkeypatch):
    """Resume the real startup check after an interrupted isolated attempt."""
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply a retained predecessor test library to resume startup checks')
    target_path = Path(option).resolve(strict=True)
    assert target_path.is_relative_to(Path(request.config.rootpath).resolve() / '.pytest_cache')
    previous_path = target_path.parent / 'metadata-conversion.json'
    previous = json.loads(previous_path.read_text())
    assert previous['interruption_rollback_verified']
    assert set(previous['copied_domains']) == {
        'annotations', 'policies', 'descriptions', 'groups', 'recommendations', 'metadata', 'archives'}
    source_path = Path(previous['source']).resolve(strict=True)
    report_path = tmp_path / 'metadata-startup.json'
    report = {'status': 'starting', 'source': str(source_path), 'target': str(target_path),
              'prior_report': str(previous_path), 'resumed_isolated_library': True,
              'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'source_sha256': source_fingerprints(request), 'captured_rows': {}}

    def save(**changes):
        report.update(changes)
        report_path.write_text(json.dumps(report, indent=2) + '\n')

    store = Store(target_path)
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    try:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
        report['recordings'] = store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0]
        assert report['recordings'] >= 10000
        report['original_counts'] = {owner: store.conn.execute(f'SELECT count(*) FROM {owner}').fetchone()[0]
                                     for owner in OWNERS}
        assert report['original_counts'] == previous['original_counts']
        assert report['original_counts'] == {owner: source.execute(f'SELECT count(*) FROM {owner}').fetchone()[0]
                                             for owner in OWNERS}
        before_catalog = migration._catalog_digest(store.conn)
        capture = metadata_legacy.capture_batch

        def progress(conn, owner, rows):
            result = capture(conn, owner, rows)
            report['captured_rows'][owner] = report['captured_rows'].get(owner, 0) + len(result)
            if report['captured_rows'][owner] % 4096 < len(result):
                save()
            return result

        monkeypatch.setattr(metadata_legacy, 'capture_batch', progress)
        save()
        from tests.schema.metadata_startup import convert_while_serving
        started = time.monotonic()
        converted = convert_while_serving(store, request, tmp_path, monkeypatch, migration, report, save)
        migration.validate(store.conn)
        assert converted == report['original_counts']
        assert migration._catalog_digest(store.conn) == before_catalog
        save(conversion_seconds=time.monotonic() - started, converted_counts=converted,
             status='continued_enrichment')
        new_root = metadata_delivery_files.root_for_database(target_path, None)
        verify_continuation(store, source, new_root, save, run_prefix='retained-startup')
    finally:
        store.conn.close()
        source.close()
        if report['status'] != 'passed':
            save(status='failed')


def test_actual_metadata_continuation_from_retained_conversion(request, tmp_path):
    option = request.config.getoption('--schema-metadata-converted-db')
    if not option:
        pytest.skip('Supply a retained converted test fixture to resume continuation checks')
    converted_path = Path(option).resolve(strict=True)
    assert converted_path.is_relative_to(Path(request.config.rootpath).resolve() / '.pytest_cache')
    previous_path = converted_path.parent / 'metadata-conversion.json'
    previous = json.loads(previous_path.read_text())
    assert previous['interruption_rollback_verified']
    assert previous['converted_counts'] == previous['original_counts']
    source_path = Path(previous['source']).resolve(strict=True)
    target_path, report_path = tmp_path / 'continuation.db', tmp_path / 'metadata-continuation.json'
    report = {'status': 'copying', 'source': str(source_path), 'converted_source': str(converted_path),
              'target': str(target_path), 'prior_report': str(previous_path),
              'prior_report_status': previous['status'], 'conversion_reused': True,
              'prior_conversion_seconds': previous['conversion_seconds'],
              'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'source_sha256': source_fingerprints(request)}

    def save(**changes):
        report.update(changes)
        report_path.write_text(json.dumps(report, indent=2) + '\n')

    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    converted = sqlite3.connect(converted_path.as_uri() + '?mode=ro', uri=True)
    source.row_factory = converted.row_factory = sqlite3.Row
    target = sqlite3.connect(target_path)
    store = None
    try:
        source.execute('PRAGMA query_only=ON')
        converted.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        converted.execute('BEGIN')
        assert source.execute('SELECT count(*) FROM recordings').fetchone()[0] >= 10000
        assert {owner: converted.execute('SELECT count(*) FROM metadata_legacy_records WHERE legacy_owner=?',
                                        (owner,)).fetchone()[0] for owner in OWNERS} == previous['original_counts']
        converted.backup(target)
        old_root = metadata_delivery_files.root_for_database(converted_path, None)
        new_root = metadata_delivery_files.root_for_database(target_path, None)
        copied = 0
        for filename, in converted.execute('SELECT DISTINCT raw_filename FROM metadata_deliveries WHERE raw_filename IS NOT NULL'):
            path = Path(filename)
            destination = new_root / path.relative_to(old_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            with destination.open('rb') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == path.stem
            copied += 1
        with target:
            target.execute('UPDATE metadata_deliveries SET raw_filename=? || substr(raw_filename,?) '
                           'WHERE raw_filename IS NOT NULL', (str(new_root), len(str(old_root)) + 1))
        target.close()
        # The retained fixture deliberately ran a staged v80 conversion while
        # its ledger remained v79. Do not run normal startup against it again.
        store = Store(target_path)
        migration.validate(store.conn)
        store._bind_repositories()
        store._snapshot_revisions = Revisions(store.conn)
        trigger = store.conn.execute("SELECT sql FROM sqlite_temp_schema "
            "WHERE name='snapshot_revision_rec_musical_sources_update'").fetchone()
        assert trigger is not None and 'payload' not in trigger[0]
        save(status='continued_enrichment', copied_metadata_files=copied,
             original_counts=previous['original_counts'])
        verify_continuation(store, source, new_root, save, run_prefix='retained-continuation')
    finally:
        if store is not None:
            store.conn.close()
        target.close()
        converted.close()
        source.close()
        if report['status'] != 'passed':
            save(status='failed')


def test_actual_metadata_conversion_interruption_and_continuing_enrichment(request, tmp_path, monkeypatch):
    option = request.config.getoption('--schema-upgraded-db') or request.config.getoption('--schema-source-db')
    if not option:
        pytest.skip('Supply an offline schema79 source; it is opened with SQLite mode=ro')
    source_path, target_path = Path(option).resolve(strict=True), tmp_path / 'metadata.db'
    report_path = tmp_path / 'metadata-conversion.json'
    report = {'source': str(source_path), 'target': str(target_path), 'status': 'copying',
              'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'copied_domains': ['metadata'], 'source_sha256': source_fingerprints(request)}
    startup = request.config.getoption('--schema-metadata-startup')

    def save(**changes):
        report.update(changes)
        report_path.write_text(json.dumps(report, indent=2) + '\n')

    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(target_path)
    source.execute('PRAGMA query_only=ON')
    source.execute('BEGIN')
    store = None
    try:
        assert source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
        report['recordings'] = source.execute('SELECT count(*) FROM recordings').fetchone()[0]
        assert report['recordings'] >= 10000
        report['original_counts'] = {name: source.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                                     for name in OWNERS}
        started = time.monotonic()
        source.backup(target)
        old_root = metadata_delivery_files.root_for_database(source_path, None)
        new_root = metadata_delivery_files.root_for_database(target_path, None)
        copied = 0
        for filename, in source.execute('SELECT DISTINCT raw_filename FROM metadata_deliveries WHERE raw_filename IS NOT NULL'):
            path = Path(filename)
            destination = new_root / path.relative_to(old_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            with destination.open('rb') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == path.stem
            copied += 1
        with target:
            if not startup:
                target.execute('UPDATE metadata_deliveries SET raw_filename=? || substr(raw_filename,?) '
                               'WHERE raw_filename IS NOT NULL', (str(new_root), len(str(old_root)) + 1))
            if startup:
                from tests.schema.test_evidence_recovery import _copy_owned_files
                report['copied_domains'] = []
                _copy_owned_files(source, target, source_path, target_path, report)
        if startup:
            evidence_source = source_path.with_name(source_path.name + '.evidence')
            evidence_target = target_path.with_name(target_path.name + '.evidence')
            for path in evidence_source.glob('*/*/*.json'):
                destination = evidence_target / path.relative_to(evidence_source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        target.close()
        store = Store(target_path)
        started_schema = time.monotonic()
        # Startup migrates before repositories and their TEMP revision triggers
        # are bound. Keep that lifecycle while examining this staged cutover.
        migrate(store.conn, registry=registry.MIGRATIONS[:79])
        validate_version(store.conn, 79)
        assert not store.conn.execute("SELECT 1 FROM sqlite_temp_schema WHERE type='trigger' "
                                      "AND name LIKE 'snapshot_revision_%'").fetchone()
        save(copy_and_open_seconds=time.monotonic() - started,
             schema_validation_seconds=time.monotonic() - started_schema, copied_metadata_files=copied,
             before_bytes=storage(store.conn), before_file_bytes=file_bytes(new_root), status='interruption')
        before_catalog = migration._catalog_digest(store.conn)
        before_schema = list(map(tuple, store.conn.execute('SELECT type,name,sql FROM sqlite_schema ORDER BY type,name')))
        capture = metadata_legacy.capture_batch

        def interrupted(conn, owner, rows):
            result = capture(conn, owner, rows)
            save(interrupted_owner=owner, interrupted_rows=len(result))
            raise WorkClosing('Controlled interruption after publishing a populated capture batch')

        monkeypatch.setattr(metadata_legacy, 'capture_batch', interrupted)
        with pytest.raises(WorkClosing, match='Controlled interruption'):
            with store.conn:
                store.conn.execute('BEGIN IMMEDIATE')
                migration.apply(store.conn, Context(4))
                migration.convert_legacy(store.conn)
        assert list(map(tuple, store.conn.execute('SELECT type,name,sql FROM sqlite_schema ORDER BY type,name'))) == before_schema
        assert migration._catalog_digest(store.conn) == before_catalog
        assert {name: store.conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                for name in OWNERS} == report['original_counts']
        save(interruption_rollback_verified=True, files_after_interruption_bytes=file_bytes(new_root),
             status='converting', captured_rows={})

        def progress(conn, owner, rows):
            result = capture(conn, owner, rows)
            report['captured_rows'][owner] = report['captured_rows'].get(owner, 0) + len(result)
            if report['captured_rows'][owner] % 4096 < len(result):
                save()
            return result

        monkeypatch.setattr(metadata_legacy, 'capture_batch', progress)
        started = time.monotonic()
        if startup:
            from tests.schema.metadata_startup import convert_while_serving
            converted = convert_while_serving(store, request, tmp_path, monkeypatch, migration, report, save)
        else:
            with store.conn:
                store.conn.execute('BEGIN IMMEDIATE')
                migration.apply(store.conn, Context(4))
                converted = migration.convert_legacy(store.conn)
        migration.validate(store.conn)
        assert converted == report['original_counts']
        assert migration._catalog_digest(store.conn) == before_catalog
        if not startup:
            store._bind_repositories()
            store._snapshot_revisions = Revisions(store.conn)
        trigger = store.conn.execute("SELECT sql FROM sqlite_temp_schema "
            "WHERE name='snapshot_revision_rec_musical_sources_update'").fetchone()
        assert trigger is not None and 'payload' not in trigger[0]
        save(conversion_seconds=time.monotonic() - started, converted_counts=converted,
             after_conversion_counts=counts(store.conn), after_bytes=storage(store.conn),
             after_file_bytes=file_bytes(new_root), status='continued_enrichment')
        verify_continuation(store, source, new_root, save)
    finally:
        if store is not None:
            store.conn.close()
        target.close()
        source.close()
        if report['status'] != 'passed':
            save(status='failed')
