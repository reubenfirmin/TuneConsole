"""Source association retractions rebuild profiles without altering source history."""
import json

import pytest

from yt_playlist.core.migrations import registry
from yt_playlist.core.store import Store
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import musical_preparation, vibe_preparation
from yt_playlist.rec.progressive import Preparation
from tests.musical_support import prepare_profiles


def deliver(store, entry, entities, run):
    store.enrichment_state.record_metadata_observations(entry, 'fixture-retraction', [
        MetadataObservation('tags', [{'name': 'Ambient' if entity == 'a' else 'Metal'}],
            'artist', entity, 'reported', match_evidence={'method': 'title_artist_match'})
        for entity in entities], run_id=run, now=10)


def settled(store):
    preparation = Preparation(store)
    for _ in range(32):
        prepare_profiles(store)
        if not vibe_preparation.due(store):
            break
        preparation.capture(1000)
    else:
        pytest.fail('Independent vibe preparation did not settle')
    assert not store.prepared_content.has_pending()
    assert not musical_preparation.due(store) and not vibe_preparation.due(store)


def source_rows(store):
    return {row['entity_id']: dict(row) for row in store.conn.execute(
        'SELECT s.*,p.digest FROM rec_musical_sources s '
        'JOIN rec_musical_source_profiles p USING(source_key) ORDER BY s.source_key')}


def versions(store):
    return json.loads(store.conn.execute(
        "SELECT payload FROM rec_musical_profiles WHERE identity_key='song|artist'").fetchone()[0])['source_versions']


@pytest.mark.parametrize(('initial', 'corrected'), [(('a', 'b'), ('a',)), (('a',), ('b',))])
def test_delivery_contraction_and_replacement_rebuild_from_current_links(store, initial, corrected):
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    deliver(store, entry, initial, 'initial')
    settled(store)
    before, counters = source_rows(store), store.prepared_content.counters()
    assert set(versions(store)) == {row['digest'] for row in before.values()}
    deliver(store, entry, corrected, 'corrected')
    assert [row[0] for row in store.prepared_content.pending(32)] == ['song|artist']
    if len(initial) == 2:
        assert source_rows(store) == before
        assert store.musical_profiles.pending() is None
        assert not vibe_preparation.due(store)
    settled(store)
    current = source_rows(store)
    assert set(versions(store)) == {current[entity]['digest'] for entity in corrected}
    assert all(current[entity] == row for entity, row in before.items())
    assert store.prepared_content.counters() == counters
    assert store.conn.execute("SELECT COUNT(*) FROM metadata_findings WHERE provider='fixture-retraction'").fetchone()[0] == len(initial) + len(corrected)


@pytest.mark.parametrize('contracted', [False, True])
def test_schema_63_upgrade_recovers_only_stranded_retracted_profiles(tmp_path, monkeypatch, contracted):
    path = tmp_path / 'library.db'
    store = Store(path)
    with monkeypatch.context() as patch:
        patch.setattr(registry, 'MIGRATIONS', tuple(m for m in registry.MIGRATIONS if m.version <= 63))
        store.init_schema()
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    deliver(store, entry, ('a', 'b'), 'initial')
    settled(store)
    before, old_versions = source_rows(store), versions(store)
    if contracted:
        deliver(store, entry, ('a',), 'old-contraction')
        assert not store.prepared_content.has_pending() and source_rows(store) == before
        assert versions(store) == old_versions
    store.conn.close()

    reopened = Store(path)
    try:
        reopened.init_schema()
        assert reopened.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] >= 64
        assert source_rows(reopened) == before and versions(reopened) == old_versions
        assert [row[0] for row in reopened.prepared_content.pending(32)] == (['song|artist'] if contracted else [])
        settled(reopened)
        assert set(versions(reopened)) == {before[entity]['digest'] for entity in (('a',) if contracted else ('a', 'b'))}
        assert source_rows(reopened) == before
        reopened.init_schema()
        assert not reopened.prepared_content.has_pending()
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()
