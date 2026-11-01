"""Opt-in cutover on the retained, isolated library after metadata conversion."""
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from yt_playlist.core.migrations import current, v080_metadata_evidence
from yt_playlist.core.store import Store


def test_actual_completed_library_preserves_offer_history(request, tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply the retained isolated schema82 library')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve() / '.pytest_cache')
    custody = json.loads((target.parent / 'metadata-conversion.json').read_text())
    assert custody['interruption_rollback_verified'] and len(custody['copied_domains']) == 7
    store = Store(target)
    try:
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 82
        recordings = store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0]
        assert recordings >= 10000
        row = store.conn.execute("SELECT payload,built_at FROM rec_proposals WHERE surface='home_shown'").fetchone()
        assert row is not None
        original, built_at = json.loads(row[0]), row[1]
        assert original['signatures']
        original_catalog = v080_metadata_evidence._catalog_digest(store.conn)
        protected_tables = ('metadata_deliveries', 'metadata_observations', 'metadata_conflicts',
                            'metadata_conflict_candidates', 'provider_attempts', 'album_descriptions',
                            'provider_usage', 'recommendation_generations', 'recommendation_pools')

        def counts():
            return {table: store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                    for table in protected_tables}

        before = counts()
        schedule = tuple(store.conn.execute('SELECT * FROM model_selection_schedule').fetchone())
        preparation = tuple(store.conn.execute('SELECT * FROM tv_preparation_state').fetchone())
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic() - started
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == current.VERSION
        assert store.rec_proposals.get_proposals('home_shown') == original
        assert store.conn.execute('SELECT built_at FROM home_offer_dedup').fetchone()[0] == built_at
        assert store.rec_ledger.claim_home_offers(original['epoch'], original['signatures'], built_at + 1) == set()
        assert tuple(store.conn.execute('SELECT * FROM model_selection_schedule').fetchone()) == schedule
        assert tuple(store.conn.execute('SELECT * FROM tv_preparation_state').fetchone()) == preparation
        assert v080_metadata_evidence._catalog_digest(store.conn) == original_catalog
        assert counts() == before
        reopened = Store(target)
        try:
            reopened.init_schema()
            assert reopened.rec_proposals.get_proposals('home_shown') == original
            assert reopened.rec_ledger.claim_home_offers(original['epoch'], original['signatures'], built_at + 2) == set()
        finally:
            reopened.conn.close()
        (tmp_path / 'home-offers.json').write_text(json.dumps({
            'status': 'passed', 'isolated_target': str(target), 'recordings': recordings,
            'python': sys.version, 'sqlite': sqlite3.sqlite_version,
            'schema_before': 82, 'schema_after': current.VERSION,
            'offer_history': original, 'built_at': built_at,
            'schedule': schedule, 'preparation': preparation,
            'protected_counts': before, 'catalog_digest': original_catalog.hex(),
            'upgrade_seconds': elapsed, 'restarted': True,
            'same_window_offers_recounted': 0}, indent=2) + '\n')
    finally:
        store.conn.close()
