"""Opt-in release-note cutover on a retained, isolated actual library."""
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from yt_playlist.core.migrations import migrate, registry, v080_metadata_evidence, v084_album_note_requests
from yt_playlist.core.migrations.runner import Context
from yt_playlist.core.store import Store


def test_actual_release_notes_preserve_paid_results_and_resume_demand(request, tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply the retained isolated schema83 library')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve() / '.pytest_cache')
    custody = json.loads((target.parent / 'metadata-conversion.json').read_text())
    assert custody['interruption_rollback_verified'] and len(custody['copied_domains']) == 7
    store = Store(target)
    try:
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 83
        original = {row[0]: (row[1], row[2]) for row in store.conn.execute(
            "SELECT surface,payload,built_at FROM rec_proposals WHERE surface IN ('album_notes','album_note_requests')")}
        notes = json.loads(original['album_notes'][0])
        assert notes and store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] >= 10000
        protected = ('metadata_deliveries', 'metadata_observations', 'metadata_conflicts',
                     'metadata_conflict_candidates', 'provider_attempts', 'album_descriptions',
                     'provider_usage', 'recommendation_generations', 'recommendation_pools')
        def counts():
            return {table: store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in protected}
        before = counts()
        paid_before = {table: list(map(tuple, store.conn.execute(f'SELECT * FROM {table} ORDER BY id')))
                       for table in ('album_descriptions', 'provider_usage')}
        catalog = v080_metadata_evidence._catalog_digest(store.conn)
        # Simulate process interruption after durable files and new rows exist,
        # before the migration's encompassing transaction can commit.
        with pytest.raises(InterruptedError), store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            v084_album_note_requests.apply(store.conn, Context(1793289600))
            raise InterruptedError('Interrupted before migration commit')
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='album_note_outputs'").fetchone()
        assert {row[0]: (row[1], row[2]) for row in store.conn.execute(
            "SELECT surface,payload,built_at FROM rec_proposals WHERE surface IN ('album_notes','album_note_requests')")} == original
        started = time.monotonic()
        migrate(store.conn, registry=registry.MIGRATIONS[:84])
        store._bind_repositories()
        elapsed = time.monotonic() - started
        assert store.rec_proposals.get_proposals('album_notes') == notes
        assert all(row[0] == original['album_notes'][1] for row in store.conn.execute('SELECT built_at FROM album_note_outputs'))
        assert not store.conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN ('album_notes','album_note_requests')").fetchone()
        album = next(dict(row) for row in store.conn.execute("SELECT p.provider_id browse_id,a.artist,a.title,a.year,a.thumbnail "
            'FROM albums a JOIN album_provider_ids p ON p.album_id=a.id '
            "WHERE p.provider='youtube' AND a.artist IS NOT NULL AND a.title IS NOT NULL ORDER BY a.id")
                     if row['browse_id'] not in notes)
        # Controlled demand uses an actually admitted release. It is restored
        # after checking stale completion so this library remains usable later.
        queue_before = store.rec_proposals.get_proposals('album_note_requests')
        assert not queue_before
        store.rec_proposals.request_album_notes([album], 1793289600, visible=True)
        _, old = store.rec_proposals.next_album_note_request()
        store.rec_proposals.request_album_notes([{**album, 'year': 'changed metadata'}], 1793289601, visible=True)
        assert not store.rec_proposals.publish_album_note(album['browse_id'], {'label': 'stale'}, 1793289602,
                                                          expected_request=old)
        pending = store.rec_proposals.get_proposals('album_note_requests')
        restarted = Store(target)
        try:
            migrate(restarted.conn, registry=registry.MIGRATIONS[:84])
            restarted._bind_repositories()
            assert restarted.rec_proposals.get_proposals('album_notes') == notes
            assert restarted.rec_proposals.get_proposals('album_note_requests') == pending
            restarted.rec_proposals.put_proposals('album_note_requests', queue_before, 1793289603)
        finally:
            restarted.conn.close()
        assert counts() == before
        assert {table: list(map(tuple, store.conn.execute(f'SELECT * FROM {table} ORDER BY id')))
                for table in paid_before} == paid_before
        assert v080_metadata_evidence._catalog_digest(store.conn) == catalog
        (tmp_path / 'album-notes.json').write_text(json.dumps({
            'status': 'passed', 'isolated_target': str(target), 'python': sys.version,
            'sqlite': sqlite3.sqlite_version, 'schema_before': 83, 'schema_after': 84,
            'notes': len(notes), 'protected_counts': before, 'catalog_digest': catalog.hex(),
            'upgrade_seconds': elapsed, 'interruption_rollback': True, 'restarted': True,
            'stale_completion_rejected': True, 'controlled_release': album['browse_id']}, indent=2) + '\n')
    finally:
        store.conn.close()
