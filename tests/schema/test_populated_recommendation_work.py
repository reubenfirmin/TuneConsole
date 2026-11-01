"""Opt-in work-state cutover on a retained, already isolated schema81 library."""
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from yt_playlist.core.migrations import current
from yt_playlist.core.migrations import v080_metadata_evidence
from yt_playlist.core.store import Store


def test_actual_completed_library_preserves_work_state_and_catalog(request, tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply a retained isolated schema81 library')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve() / '.pytest_cache')
    prior = json.loads((target.parent / 'metadata-conversion.json').read_text())
    assert prior['interruption_rollback_verified']
    assert len(prior['copied_domains']) == 7
    store = Store(target)
    try:
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 81
        recordings = store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0]
        assert recordings >= 10000
        captured = {row[0]: {'state': json.loads(row[1]), 'built_at': row[2]} for row in store.conn.execute(
            "SELECT surface,payload,built_at FROM rec_proposals WHERE surface IN ('daily_model_schedule','tv_preparation')")}
        original_catalog = v080_metadata_evidence._catalog_digest(store.conn)
        protected_tables = ('metadata_deliveries', 'metadata_observations', 'metadata_conflicts',
                            'metadata_conflict_candidates', 'provider_attempts', 'album_descriptions',
                            'provider_usage', 'recommendation_generations', 'recommendation_pools')

        def counts():
            return {table: store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                    for table in protected_tables}

        before = counts()
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic() - started
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == current.VERSION
        for surface, original in captured.items():
            assert store.rec_proposals.get_proposals(surface) == (original['state'] or None)
        assert v080_metadata_evidence._catalog_digest(store.conn) == original_catalog
        assert counts() == before
        current.validate(store.conn)
        reopened = Store(target)
        try:
            reopened.init_schema()
            for surface, original in captured.items():
                assert reopened.rec_proposals.get_proposals(surface) == (original['state'] or None)
        finally:
            reopened.conn.close()
        (tmp_path / 'recommendation-work.json').write_text(json.dumps({
            'status': 'passed', 'isolated_target': str(target), 'recordings': recordings,
            'python': sys.version, 'sqlite': sqlite3.sqlite_version,
            'schema_before': 81, 'schema_after': current.VERSION,
            'captured_work': captured, 'protected_counts': before,
            'catalog_digest': original_catalog.hex(), 'upgrade_seconds': elapsed,
            'restarted': True}, indent=2) + '\n')
    finally:
        store.conn.close()
