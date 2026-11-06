"""Committed foreground reads remain available during an atomic bundle write."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from yt_playlist.core.store import Store
from yt_playlist.core import json_cache
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.rec import content_model, radio_candidates
from yt_playlist.repos.rec_proposals import encode_proposals
from tests.recommendation_support import publish_bundles


@pytest.fixture
def historical_publication_store(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'publication.db')
    try:
        # Keep the genuine hidden-row owner, including its historical guards.
        _pristine_installed_schema(69).backup(store.conn)
        store.conn.execute('PRAGMA journal_mode=WAL')
        store._bind_repositories()
        store._snapshot_revisions = Revisions(store.conn)
        yield store
    finally:
        store.conn.close()


@pytest.mark.parametrize('commit', [True, False])
@pytest.mark.parametrize('staged', [False, True])
def test_foreground_reads_see_last_good_while_publication_holds_writer(
        historical_publication_store, monkeypatch, commit, staged):
    store = historical_publication_store
    assert store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    # Exercise the retained predecessor cache protocol. Current typed head
    # publication and its foreground readers are checked independently.
    old = {'all': {'fresh': [{'key': 'old'}]}, '_meta': {'full_pools': True}}
    new = {'all': {'fresh': [{'key': 'new'}]}, '_meta': {'full_pools': True}}
    store.rec_proposals.put_proposals('mode_bundles', old, 1)
    store.settings.set_setting('config_input', 'old')
    encoded = encode_proposals('mode_bundles', new)
    prepared = store.rec_proposals.stage_prepared_bundles(encoded, '[]') if staged else (encoded, '[]')
    entered, release = threading.Event(), threading.Event()
    original, connections = store._make_reader, []

    def reader():
        view = original()
        connections.append(view.conn)
        return view

    def no_revisions(*args, **kwargs):
        pytest.fail('Serving lookup copied publication revisions')

    monkeypatch.setattr(store, '_make_reader', reader)
    monkeypatch.setattr(store._snapshot_revisions, 'capture', no_revisions)

    def publish():
        with store._lock:
            store.conn.execute('BEGIN IMMEDIATE')
            try:
                store.rec_proposals.publish_prepared_bundles(*prepared, 2)
                store.settings.publish_values({'config_input': 'new'})
                entered.set()
                assert release.wait(5), 'Foreground read waited for the publishing writer'
                if commit:
                    store.conn.commit()
            finally:
                store.conn.rollback()

    def read(operation):
        if operation == 'setting':
            return store.settings.get_setting('config_input')
        if operation == 'namespace':
            return store.settings.get_settings_with_prefix('config_')
        if operation == 'present':
            return store.rec_proposals.has_proposals('mode_bundles')
        if operation == 'groups':
            return json.loads(store.rec_proposals.group_pools_json())
        return store.rec_proposals.get_proposals('mode_bundles')

    expected = {'setting': 'old', 'namespace': {'config_input': 'old'},
                'present': True, 'bundle': old, 'groups': old}
    try:
        with ThreadPoolExecutor(2) as workers:
            writing = workers.submit(publish)
            try:
                assert entered.wait(5)
                for operation, value in expected.items():
                    assert workers.submit(read, operation).result(2) == value, operation
                    assert not writing.done(), 'Writer did not remain active during the read'
            finally:
                release.set()
            writing.result(5)
        assert len(connections) == len(expected)
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                connection.execute('SELECT 1')
        assert store.settings.get_setting('config_input') == ('new' if commit else 'old')
        assert store.rec_proposals.get_proposals('mode_bundles') == (new if commit else old)
    finally:
        store.conn.close()


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


def test_radio_inputs_are_coherent_and_reader_closes_before_decode(tmp_path, monkeypatch, consumer_store):
    from yt_playlist.core import recommendation_outputs
    store = consumer_store(tmp_path / 'radio.db')
    store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    old_model = json.dumps({'cat': {'old': 0}})
    new_model = json.dumps({'cat': {'new': 0}})
    store.settings.set_values({'rec_content_generation': 'old', 'rec_content_model': old_model})
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

    space_id = radio_candidates.vectors.content_space_id
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
        with store._lock, store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            store.settings.publish_values({'rec_content_generation': 'new',
                                           'rec_content_model': new_model})
            store.recommendations.publish(build, 2)
        return result

    def no_global_decode(*args, **kwargs):
        pytest.fail('Typed Radio inputs inflated a retired global JSON bundle')

    original_decode, observations = recommendation_outputs._decode, []

    def decode(encoded, *args, **kwargs):
        assert not store._lock.held_by_current_thread
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                connection.execute('SELECT 1')
        observations.append(True)
        return original_decode(encoded, *args, **kwargs)

    monkeypatch.setattr(store, '_make_reader', reader)
    monkeypatch.setattr(radio_candidates.vectors, 'content_space_id', publish_during_read)
    monkeypatch.setattr(json_cache, 'decode', no_global_decode)
    monkeypatch.setattr(recommendation_outputs, '_decode', decode)
    try:
        generation, encoding_model, space, modes, bundles = radio_candidates._prepared_inputs(store, prepared=prepared_radio)
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
        assert store.settings.get_setting('rec_content_generation') == 'new'
    finally:
        prepared_radio.close()
        store.conn.close()
