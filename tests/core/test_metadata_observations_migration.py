"""Add scoped observations without rewriting existing selections or source history."""
import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.providers.base import MetadataObservation


def test_populated_upgrade_preserves_legacy_evidence_and_rolls_back_interruption(tmp_path):
    store = Store(tmp_path / 'installed.db')
    try:
        migrate(store.conn, registry=MIGRATIONS[:48])
        store._bind_repositories()
        tid = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
        store.enrichment.apply_provider_fields(tid, 'source', {'bpm': 125}, providers=['source'])
        store.enrichment_state.record_enrichment_attempt('song|artist', 'source', {'bpm': 125}, now=10)
        legacy = tuple(store.conn.execute('SELECT * FROM metadata_findings').fetchone())
        attempts = [tuple(r) for r in store.conn.execute('SELECT * FROM provider_attempts')]
        selected = store.enrichment_reports.metadata_provenance(tid)

        def interrupted(conn, context):
            MIGRATIONS[48].apply(conn, context)
            raise RuntimeError('interrupted observations migration')

        with pytest.raises(MigrationError, match='interrupted observations'):
            migrate(store.conn, registry=(*MIGRATIONS[:48], Migration(49, MIGRATIONS[48].name, interrupted)))
        assert tuple(store.conn.execute('SELECT * FROM metadata_findings').fetchone()) == legacy
        assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 48
        store.init_schema()
        upgraded = tuple(store.conn.execute('SELECT * FROM metadata_findings').fetchone())
        assert upgraded == (*legacy, None, None)
        assert [tuple(r) for r in store.conn.execute('SELECT * FROM provider_attempts')] == attempts
        assert store.enrichment_reports.metadata_provenance(tid) == selected
        assert store.enrichment_state.metadata_observations('song|artist') == []
        store.enrichment_state.record_metadata_observations(tid, 'new', [
            MetadataObservation('mood_party', 0.8, 'recording', 'mbid', 'prediction')], run_id='upgrade', now=20)
        before = list(store.conn.iterdump())
        store.init_schema()
        assert list(store.conn.iterdump()) == before
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()
