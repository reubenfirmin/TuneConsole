"""Source association retractions rebuild profiles without altering source history."""
import json

import pytest

from yt_playlist.core import metadata_native
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.store import Store
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import musical_preparation, vibe_preparation
from yt_playlist.rec.progressive import Preparation
from tests.musical_support import prepare_profiles
from tests.historical_support import upgrade_fixture
from yt_playlist.rec import musical_profiles


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
    return store.musical_profiles.by_keys(['song|artist'])['song|artist']['source_versions']


def test_profiles_drain_after_a_bounded_capture_defers(store, monkeypatch):
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    deliver(store, entry, ('a',), 'initial')
    original = Preparation.capture
    calls = []

    def defer_once(self, now):
        calls.append(now)
        return False if len(calls) == 1 else original(self, now)

    monkeypatch.setattr(Preparation, 'capture', defer_once)
    settled(store)
    assert len(calls) > 1
    assert set(versions(store)) == {source_rows(store)['a']['digest']}


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
        # Removing a source link invalidates the joint model's masked metadata
        # inputs immediately, even though no source profile needs rebuilding.
        from yt_playlist.rec import vibe_joint_preparation
        assert store.vibe_predictions.pending() is None
        assert vibe_preparation.due(store) == vibe_joint_preparation.due(store)
    settled(store)
    current = source_rows(store)
    assert set(versions(store)) == {current[entity]['digest'] for entity in corrected}
    assert all(current[entity] == row for entity, row in before.items())
    assert store.prepared_content.counters() == counters
    observations = store.enrichment_state.metadata_observations('song|artist')
    assert len([row for row in observations if row['provider'] == 'fixture-retraction']) == len(initial) + len(corrected)


@pytest.mark.parametrize('contracted', [False, True])
def test_schema_63_upgrade_recovers_only_stranded_retracted_profiles(tmp_path, contracted):
    path = tmp_path / 'library.db'
    store = Store(path)
    migrate(store.conn, registry=registry.MIGRATIONS[:63])
    # Populate the frozen predecessor without invoking today's repository API.
    with store.conn:
        recording = store.conn.execute("INSERT INTO recordings(identity_key,title,artist) VALUES ('song|artist','Song','Artist')").lastrowid
        store.conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,artist,duration_s) VALUES ('video',?,'Song','Artist',200)", (recording,))
        entry = store.conn.execute("INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,'video')", (recording,)).lastrowid
        store.conn.execute("INSERT INTO library_memberships VALUES (?,'fixture',NULL)", (entry,))
        store.conn.execute("INSERT INTO recording_aliases VALUES ('song|artist',?,'fixture')", (recording,))

    def historical_delivery(entities, delivery):
        with store.conn:
            for entity in entities:
                provenance = {'schema': 1, 'scope': 'artist', 'entity_id': entity,
                              'kind': 'reported', 'delivery_id': delivery,
                              'match_evidence': {'method': 'title_artist_match'}}
                store.conn.execute('INSERT INTO metadata_findings(entry_id,recording_id,provider,field,value_json,provenance_json,observed_at) VALUES (?,?,?,?,?,?,10)',
                    (entry, recording, 'fixture-retraction', 'tags', json.dumps([{'name': 'Ambient' if entity == 'a' else 'Metal'}]), json.dumps(provenance)))

    historical_delivery(('a', 'b'), 'initial')
    sources = []
    with store.conn:
        for row in store.conn.execute('SELECT * FROM rec_musical_sources').fetchall():
            profile = musical_profiles.source_profile({**dict(row), 'observations': json.loads(row['payload'])})
            sources.append(profile)
            store.conn.execute('INSERT INTO rec_musical_source_profiles VALUES (?,?,?,?,?)',
                (row['source_key'], musical_profiles.canonical(profile), musical_profiles.VERSION, musical_profiles.digest(profile), 1000))
        profile = musical_profiles.resolve(sources)
        store.conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?)',
            ('song|artist', musical_profiles.canonical(profile), musical_profiles.VERSION, musical_profiles.digest(profile), 1000))
        store.conn.execute('DELETE FROM rec_musical_pending')
        store.conn.execute('DELETE FROM rec_content_pending')
    before = source_rows(store)
    old_versions = json.loads(store.conn.execute('SELECT payload FROM rec_musical_profiles').fetchone()[0])['source_versions']
    if contracted:
        historical_delivery(('a',), 'old-contraction')
        assert not store.conn.execute('SELECT 1 FROM rec_content_pending').fetchone()
        assert source_rows(store) == before
        assert json.loads(store.conn.execute('SELECT payload FROM rec_musical_profiles').fetchone()[0])['source_versions'] == old_versions
    store.conn.close()

    reopened = Store(path)
    try:
        migrate(reopened.conn, registry=registry.MIGRATIONS[:64])
        assert reopened.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 64
        assert source_rows(reopened) == before
        assert json.loads(reopened.conn.execute('SELECT payload FROM rec_musical_profiles').fetchone()[0])['source_versions'] == old_versions
        assert [row[0] for row in reopened.conn.execute('SELECT identity_key FROM rec_content_pending')] == (['song|artist'] if contracted else [])
        # Establish the selective v64 repair before later format changes enqueue
        # their own work, then exercise the current bounded preparation worker.
        upgrade_fixture(reopened)
        settled(reopened)
        assert set(versions(reopened)) == {before[entity]['digest'] for entity in (('a',) if contracted else ('a', 'b'))}
        current = source_rows(reopened)
        for entity, original in before.items():
            # v80 replaces the source JSON column with retained observation
            # owners. Compare the same evidence through that current boundary.
            assert current[entity] == {key: value for key, value in original.items() if key != 'payload'}
            assert metadata_native.source_values(reopened.conn, original['source_key']) == json.loads(original['payload'])
        reopened.init_schema()
        assert not reopened.prepared_content.has_pending()
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()
