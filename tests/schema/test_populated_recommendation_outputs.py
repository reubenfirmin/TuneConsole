"""Opt-in ordinary display migration on the retained isolated actual library."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from yt_playlist.core import home_outputs, listening_outputs, recommendation_outputs
from yt_playlist.core.migrations import current, migrate, registry, v090_listening_outputs, v091_home_outputs
from yt_playlist.core.store import Store


def test_actual_outputs_keep_exact_bytes_roots_paid_results_and_restart(request, tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply the retained isolated actual-library copy')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve() / '.pytest_cache')
    custody = json.loads((target.parent / 'metadata-conversion.json').read_text())
    assert custody['interruption_rollback_verified'] and len(custody['copied_domains']) == 7
    store = Store(target)
    try:
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] in (84, 85, 86, 87, 88, 89)
        # Establish the predecessor before taking preservation measurements.
        # Genre normalization belongs to #495; this test must not attribute its
        # intentional physical catalog changes to a display-file relocation.
        migrate(store.conn, registry=registry.MIGRATIONS[:89])
        store._bind_repositories()
        assert store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] >= 10000
        original = {surface: (raw, at) for surface, raw, at in store.conn.execute('SELECT surface,payload,built_at FROM rec_proposals')
                    if surface in home_outputs.SURFACES or listening_outputs.owns(surface)}
        assert len(original) >= 15
        assert any(surface.startswith('listening_chunk:') for surface in original)
        protected = ('metadata_deliveries', 'metadata_observations', 'metadata_conflicts',
                     'metadata_conflict_candidates', 'provider_attempts', 'album_descriptions',
                     'provider_usage', 'recommendation_generations', 'recommendation_pools')
        def counts():
            return {table: store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in protected}
        before = counts()
        paid = {table: list(map(tuple, store.conn.execute(f'SELECT * FROM {table} ORDER BY id')))
                for table in ('album_descriptions', 'provider_usage')}
        def catalog_digest():
            digest = hashlib.sha256()
            for table in ('recordings', 'youtube_items', 'catalog_entries', 'artists', 'albums', 'album_provider_ids'):
                digest.update(table.encode())
                for row in store.conn.execute(f'SELECT * FROM {table} ORDER BY 1'):
                    digest.update(json.dumps(tuple(row), ensure_ascii=True, allow_nan=False).encode())
            return digest.hexdigest()
        catalog = catalog_digest()
        roots = list(map(tuple, store.conn.execute('SELECT * FROM group_evidence_owners ORDER BY kind,external_key')))
        with pytest.raises(InterruptedError), store.conn.atomic():
            v090_listening_outputs.apply(store.conn, None)
            v091_home_outputs.apply(store.conn, None)
            raise InterruptedError('Interrupted after file relocation, before commit')
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name IN ('home_display_publications','listening_output_publications')").fetchone()
        assert {surface: (raw, at) for surface, raw, at in store.conn.execute('SELECT surface,payload,built_at FROM rec_proposals')
                if surface in original} == original
        assert list(map(tuple, store.conn.execute('SELECT * FROM group_evidence_owners ORDER BY kind,external_key'))) == roots
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic() - started
        for surface, (raw, at) in original.items():
            if surface in home_outputs.SURFACES:
                assert home_outputs.encoded(store.conn, surface) == raw
                assert store.conn.execute('SELECT p.built_at FROM home_display_heads h '
                    'JOIN home_display_publications p ON p.id=h.publication_id WHERE h.surface=?', (surface,)).fetchone()[0] == at
            else:
                assert store.listening_analytics.encoded(surface) == raw
                kind = 'listening_chunk' if surface.startswith('listening_chunk:') else surface
                key = surface.removeprefix('listening_chunk:') if kind == 'listening_chunk' else ''
                assert store.conn.execute('SELECT built_at FROM listening_output_files WHERE surface=? AND chunk_key=?',
                                          (kind, key)).fetchone()[0] == at
            assert not store.conn.execute('SELECT 1 FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        assert counts() == before and catalog_digest() == catalog
        assert {table: list(map(tuple, store.conn.execute(f'SELECT * FROM {table} ORDER BY id')))
                for table in paid} == paid
        with store.conn.atomic():
            backup = recommendation_outputs.export(store.conn, tmp_path / 'display-backup')
        recommendation_outputs.restore(store.conn, backup)
        reopened = Store(target)
        try:
            reopened.init_schema()
            for surface, (raw, _) in original.items():
                actual = (home_outputs.encoded(reopened.conn, surface) if surface in home_outputs.SURFACES
                          else reopened.listening_analytics.encoded(surface))
                assert actual == raw
            assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
        finally:
            reopened.conn.close()
        (tmp_path / 'display-outputs.json').write_text(json.dumps({
            'status': 'passed', 'isolated_target': str(target), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
            'schema_before': 89, 'schema_after': current.VERSION, 'source_rows': len(original),
            'preserved_payload_bytes': sum(len(raw.encode()) for raw, _ in original.values()),
            'protected_counts': before, 'catalog_digest': catalog, 'upgrade_seconds': elapsed,
            'interrupted_cutover_rollback': True, 'backup_verified': True, 'restarted': True,
            'home_parts': store.conn.execute('SELECT count(*) FROM home_display_files').fetchone()[0],
            'listening_parts': store.conn.execute('SELECT count(*) FROM listening_output_files').fetchone()[0]}, indent=2) + '\n')
    finally:
        store.conn.close()
