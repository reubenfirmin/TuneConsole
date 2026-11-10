"""Opt-in cutover on the retained, isolated library after metadata conversion."""
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

from yt_playlist.core.migrations import current, v080_metadata_evidence
from yt_playlist.core.migrations import migrate, registry, regular
from yt_playlist.core.store import Store
from tests.schema.actual_library import copy_readonly_library
from tests.library_image.fixtures import report_file


def test_actual_completed_library_preserves_offer_history(request, tmp_path, monkeypatch):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply the retained isolated schema82 library')
    source = Path(option).resolve(strict=True)
    custody = json.loads((source.parent / 'metadata-conversion.json').read_text())
    assert custody['interruption_rollback_verified'] and len(custody['copied_domains']) == 7
    target, capture = tmp_path / 'home-offers.db', {}
    target = copy_readonly_library(source, target, monkeypatch, capture)
    assert capture['source_version'] == 82
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
        # Exercise the explicit historical Home-owner conversion. Ordinary
        # startup preserves installed history instead of running bulk upgrades.
        migrate(store.conn, registry=registry.MIGRATIONS[:83])
        store.init_schema()
        elapsed = time.monotonic() - started
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 83
        assert store.rec_proposals.get_proposals('home_shown') == original
        assert store.conn.execute('SELECT built_at FROM home_offer_dedup').fetchone()[0] == built_at
        assert tuple(store.conn.execute('SELECT * FROM model_selection_schedule').fetchone()) == schedule
        assert tuple(store.conn.execute('SELECT * FROM tv_preparation_state').fetchone()) == preparation
        assert v080_metadata_evidence._catalog_digest(store.conn) == original_catalog
        assert counts() == before
        store.rec_proposals.put_proposals('home_shown', original, built_at + 1)
        reopened = Store(target)
        try:
            reopened.init_schema()
            assert reopened.rec_proposals.get_proposals('home_shown') == original
            assert reopened.conn.execute('SELECT built_at FROM home_offer_dedup').fetchone()[0] == built_at + 1
            regular.validate(reopened.conn)
        finally:
            reopened.conn.close()
        report = report_file(Path(request.config.rootpath) / 'build/test-reports/implementation1137-home-offers-actual.json')
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({
            'status': 'passed', 'isolated_target': str(target), 'recordings': recordings,
            'python': sys.version, 'sqlite': sqlite3.sqlite_version,
            'schema_before': 82, 'schema_after': 83, 'current_supported_version': current.VERSION,
            'offer_history': original, 'built_at': built_at,
            'schedule': schedule, 'preparation': preparation,
            'protected_counts': before, 'catalog_digest': original_catalog.hex(),
            'upgrade_seconds': elapsed, 'restarted': True,
            'continuing_write_built_at': built_at + 1, 'source_capture': capture}, indent=2) + '\n')
    finally:
        store.conn.close()
