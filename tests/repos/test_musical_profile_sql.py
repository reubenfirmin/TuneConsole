"""Musical-profile SQL detaches file heads and owns only its own writes."""
import hashlib

import pytest

from yt_playlist.core import fao
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.rec import musical_preparation


def seed_source(store):
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    observation = MetadataObservation('mood_party', .7, 'recording', 'recording', 'prediction',
                                      unit='classifier_probability')
    store.enrichment_state.record_metadata_observations(entry, 'native', [observation], run_id='one', now=20)
    return store.musical_profiles.pending()


def test_profile_repository_reads_detach_references_without_files_or_begin(store, monkeypatch):
    source = seed_source(store)
    assert musical_preparation.source_step(store, 30)
    body = '{"version":"v","dimensions":{}}'
    digest = hashlib.sha256(body.encode()).hexdigest()
    store.musical_profiles.publish_recordings([('song|artist', body, 'v', digest)], 31)
    statements = []
    monkeypatch.setattr(fao, 'read', lambda *args, **kwargs: pytest.fail('Repository opened a file'))
    monkeypatch.setattr(fao, 'write', lambda *args, **kwargs: pytest.fail('Repository wrote a file'))
    repository = store.musical_profiles.repository
    store.conn.set_trace_callback(statements.append)
    try:
        facts = repository.recording_facts(['song|artist'])['song|artist']
        assert facts[0][0][0] == source
        refs = repository.profile_references(['song|artist'])
        assert refs[0][1] == '' and refs[0][5] is not None
        assert repository.readiness_rows(['song|artist'])[0][:3] == ('song|artist', 'v', digest)
        source_digest = facts[0][0][3]
        assert repository.provenance_references('song|artist', [source_digest])[0][0] == source
    finally:
        store.conn.set_trace_callback(None)
    assert not any(sql.lstrip().upper().startswith('BEGIN') for sql in statements)
    assert not store.conn.in_transaction


def test_profile_files_are_written_before_the_repository_transaction(store, monkeypatch):
    source = seed_source(store)
    write, published = fao.write, []

    def outside_transaction(path, *args, **kwargs):
        assert not store.conn.in_transaction
        published.append(path)
        return write(path, *args, **kwargs)

    monkeypatch.setattr(fao, 'write', outside_transaction)
    assert musical_preparation.source_step(store, 30)
    assert not store.conn.execute('SELECT 1 FROM rec_musical_pending WHERE source_key=?', (source,)).fetchone()
    body = '{"version":"v","dimensions":{}}'
    digest = hashlib.sha256(body.encode()).hexdigest()
    store.musical_profiles.publish_recordings([('song|artist', body, 'v', digest)], 31)
    assert len(published) >= 2
    assert store.musical_profiles.by_keys(['song|artist'])['song|artist']['version'] == 'v'
    assert not store.conn.in_transaction
