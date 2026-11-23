"""Committed foreground reads remain available during an atomic bundle write."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from yt_playlist.core.store import Store
from yt_playlist.core.model_state import ArtistModelUpdate, RecordingModelUpdate
from yt_playlist.core import json_cache
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.rec import content_model, embedding_vectors, radio_candidate_inputs
from yt_playlist.core.proposal_codec import encode_proposals
from tests.recommendation_support import publish_bundles


@pytest.mark.parametrize('file_backed', [True, False])
def test_settings_lookup_reuses_available_connection_and_owned_transaction(
        tmp_path, monkeypatch, file_backed, consumer_store):
    store = consumer_store(tmp_path / 'lookup.db' if file_backed else ':memory:')
    store.settings.set_setting('config_input', 'old')

    def no_reader():
        pytest.fail('An available indexed lookup opened another connection')

    monkeypatch.setattr(store, '_make_reader', no_reader)
    try:
        assert store.settings.get_setting('config_input') == 'old'
        assert store.settings.get_setting('missing', 'default') == 'default'
        with store._lock:
            store.conn.execute("UPDATE settings SET value='uncommitted' WHERE key='config_input'")
            try:
                assert store.settings.get_setting('config_input') == 'uncommitted'
                assert store.settings.get_settings_with_prefix('config_') == {
                    'config_input': 'uncommitted'}
                # Publication guards and callers inside transactions use the same connection.
                assert not store.rec_proposals.has_proposals('mode_bundles')
                assert store.rec_proposals.get_proposals('mode_bundles') is None
            finally:
                store.conn.rollback()
        assert store.settings.get_setting('config_input') == 'old'
    finally:
        store.conn.close()


def test_model_updates_and_related_settings_share_a_commit(tmp_path, consumer_store):
    store = consumer_store(tmp_path / 'models.db')
    try:
        model = store.rec_model
        model.publish_model_state(recording=RecordingModelUpdate(generation='old-generation'),
                                  artist=ArtistModelUpdate(fit_fingerprint='old-fit'),
                                  related_settings={'config_input': 'old-version'})
        with pytest.raises(sqlite3.IntegrityError, match='Collection state requires its domain owner'):
            model.publish_model_state(recording=RecordingModelUpdate(generation='new-generation'),
                                      artist=ArtistModelUpdate(fit_fingerprint='new-fit'),
                                      related_settings={'config_input': 'new-version',
                                                        'rec_content_generation': 'wrong owner'})
        assert model.recording_state().generation == 'old-generation'
        assert model.artist_state().fit_fingerprint == 'old-fit'
        assert store.settings.get_setting('config_input') == 'old-version'
    finally:
        store.conn.close()


def test_radio_inputs_are_coherent_and_reader_closes_before_decode(tmp_path, monkeypatch, consumer_store):
    from yt_playlist.core import recommendation_content
    store = consumer_store(tmp_path / 'radio.db')
    store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    old_model = json.dumps({'cat': {'old': 0}})
    new_model = json.dumps({'cat': {'new': 0}})
    store.rec_model.publish_model_state(recording=RecordingModelUpdate(generation='old', encoder=old_model))
    publish_bundles(store, {'_meta': {'musical_generation': 'old'},
        'all': {'wheelhouse': [{'key': 'song|artist', 'video_id': 'video',
                               'title': 'Song', 'artist': 'Artist', 'reason': 'Retained explanation'}]}}, 1)
    from yt_playlist.rec.stored_radio import PreparedRadio
    prepared_radio = PreparedRadio(store, 1)
    original_reader, connections = store._make_reader, []

    def reader():
        view = original_reader()
        connections.append(view.conn)
        return view

    space_id = embedding_vectors.content_space_id
    # Stage outside the input reader: only the head/settings commit belongs
    # inside the deliberately pinned read.
    from yt_playlist.rec import bundle_storage
    policy = store.recommendations.head('home')['policy_version']
    plan = bundle_storage.prepare(store.conn, {'_meta': {'musical_generation': 'new'}}, [],
        external_key='coherent-new', policy_version=policy, input_revision=1, created_at=2)
    build = store.recommendations.claim(plan)
    store.recommendations.stage(plan, build)

    def publish_during_read(view, **kwargs):
        assert view.conn is not store.conn
        result = space_id(view, **kwargs)
        # A real commit while the input reader is pinned must not mix generations.
        with store._lock:
            store.rec_model.publish_model_state(recording=RecordingModelUpdate(generation='new', encoder=new_model))
            store.recommendations.publish(build, 2)
        return result

    def no_global_decode(*args, **kwargs):
        pytest.fail('Typed Radio inputs inflated a retired global JSON bundle')

    original_decode, observations = recommendation_content.decode, []

    def decode(encoded, *args, **kwargs):
        assert not store._lock.held_by_current_thread
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                connection.execute('SELECT 1')
        observations.append(True)
        return original_decode(encoded, *args, **kwargs)

    monkeypatch.setattr(store, '_make_reader', reader)
    monkeypatch.setattr(embedding_vectors, 'content_space_id', publish_during_read)
    monkeypatch.setattr(json_cache, 'decode', no_global_decode)
    monkeypatch.setattr(recommendation_content, 'decode', decode)
    try:
        generation, encoding_model, space, modes, bundles = radio_candidate_inputs.prepared_inputs(
            store, prepared=prepared_radio)
        assert generation == 'old' and encoding_model == old_model
        assert space == content_model.content_model_fingerprint(json.loads(old_model))
        assert modes == {} and bundles['_meta']['musical_generation'] == 'old'
        assert bundles['all']['wheelhouse'][0]['key'] == 'song|artist'
        assert bundles['all']['wheelhouse'][0]['reason'] == 'Retained explanation'
        assert observations, 'The fixture must reach real nonempty output decoding'
        assert store.recommendations.framing('home')['musical_generation'] == 'new'
        assert not store._lock.held_by_current_thread
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connections[0].execute('SELECT 1')
        assert store.rec_model.recording_state().generation == 'new'
    finally:
        prepared_radio.close()
        store.conn.close()
