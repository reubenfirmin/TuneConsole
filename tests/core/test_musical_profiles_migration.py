"""Upgrade reconstructs disposable source projections from retained observations."""
import json

import pytest

from yt_playlist.core.migrations import migrate, Migration, MigrationError
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.providers.base import MetadataObservation


def test_populated_upgrade_replays_latest_fields_without_changing_evidence_and_is_atomic(tmp_path):
    store = Store(tmp_path / 'upgrade.db')
    try:
        migrate(store.conn, registry=MIGRATIONS[:49])
        store._bind_repositories()
        tid = store.tracks.upsert_track('v', 'Song', 'Artist', '', 200)
        for i, value in enumerate(([{'name': 'Metal'}], [])):
            store.enrichment_state.record_metadata_observations(tid, 'fixture', [MetadataObservation(
                'tags', value, 'artist', 'artist-id', 'reported',
                match_evidence={'method': 'title_artist_match', 'input_title': 'Song'})], run_id=str(i), now=i)
        history = [tuple(row) for row in store.conn.execute('SELECT * FROM metadata_findings')]
        def interrupted(conn, context):
            MIGRATIONS[49].apply(conn, context)
            raise RuntimeError('interrupted profiles upgrade')
        with pytest.raises(MigrationError, match='interrupted profiles'):
            migrate(store.conn, registry=(*MIGRATIONS[:49], Migration(50, MIGRATIONS[49].name, interrupted)))
        assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 49
        store.init_schema()
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM metadata_findings')] == history
        source = store.conn.execute('SELECT payload FROM rec_musical_sources').fetchone()[0]
        assert json.loads(source)['tags']['value'] == []
        assert 'delivery_id' not in source and 'input_title' not in source
        assert store.conn.execute('SELECT COUNT(*) FROM rec_musical_links').fetchone()[0] == 1
        assert store.musical_profiles.pending()
        assert store.prepared_content.has_pending()
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        before = list(store.conn.iterdump())
        store.init_schema()
        assert list(store.conn.iterdump()) == before
    finally:
        store.conn.close()
