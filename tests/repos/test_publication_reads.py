"""Committed foreground reads remain available during an atomic bundle write."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from yt_playlist.core.store import Store
from yt_playlist.core import json_cache
from yt_playlist.rec import content_model, radio_candidates
from yt_playlist.repos.rec_proposals import encode_proposals


@pytest.mark.parametrize('operation', ['setting', 'namespace', 'present', 'bundle', 'groups'])
@pytest.mark.parametrize('commit', [True, False])
@pytest.mark.parametrize('staged', [False, True])
def test_foreground_reads_see_last_good_while_publication_holds_writer(
        tmp_path, monkeypatch, operation, commit, staged):
    store = Store(tmp_path / 'publication.db')
    store.init_schema()
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

    def read():
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
                'present': True, 'bundle': old, 'groups': old}[operation]
    try:
        with ThreadPoolExecutor(2) as workers:
            writing = workers.submit(publish)
            try:
                assert entered.wait(5)
                assert workers.submit(read).result(2) == expected
                assert not writing.done(), 'Writer did not remain active during the read'
            finally:
                release.set()
            writing.result(5)
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connections[0].execute('SELECT 1')
        assert store.settings.get_setting('config_input') == ('new' if commit else 'old')
        assert store.rec_proposals.get_proposals('mode_bundles') == (new if commit else old)
    finally:
        store.conn.close()


@pytest.mark.parametrize('file_backed', [True, False])
def test_settings_lookup_reuses_available_connection_and_owned_transaction(
        tmp_path, monkeypatch, file_backed):
    store = Store(tmp_path / 'lookup.db' if file_backed else ':memory:')
    store.init_schema()
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


def test_radio_inputs_are_coherent_and_reader_closes_before_decode(tmp_path, monkeypatch):
    store = Store(tmp_path / 'radio.db')
    store.init_schema()
    old_model = json.dumps({'cat': {'old': 0}})
    new_model = json.dumps({'cat': {'new': 0}})
    store.settings.set_values({'rec_content_generation': 'old', 'rec_content_model': old_model})
    store.rec_proposals.put_proposals('mode_bundles', {'_meta': {'generation': 'old'}}, 1)
    original_reader, connections = store._make_reader, []

    def reader():
        view = original_reader()
        connections.append(view.conn)
        return view

    space_id = radio_candidates.vectors.content_space_id

    def publish_during_read(view, **kwargs):
        assert view.conn is not store.conn
        result = space_id(view, **kwargs)
        # A real commit while the input reader is pinned must not mix generations.
        with store._lock, store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            store.settings.publish_values({'rec_content_generation': 'new',
                                           'rec_content_model': new_model})
            store.rec_proposals.publish_prepared_bundles('{"_meta":{"generation":"new"}}', '[]', 2)
        return result

    decode = json_cache.decode

    def after_close(encoded, **kwargs):
        assert not store._lock.held_by_current_thread
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connections[0].execute('SELECT 1')
        return decode(encoded, **kwargs)

    monkeypatch.setattr(store, '_make_reader', reader)
    monkeypatch.setattr(radio_candidates.vectors, 'content_space_id', publish_during_read)
    monkeypatch.setattr(json_cache, 'decode', after_close)
    try:
        generation, encoding_model, space, modes, bundles = radio_candidates._prepared_inputs(store)
        assert generation == 'old' and encoding_model == old_model
        assert space == content_model.content_model_fingerprint(json.loads(old_model))
        assert modes == {} and bundles == {'_meta': {'generation': 'old'}}
        assert store.settings.get_setting('rec_content_generation') == 'new'
    finally:
        store.conn.close()
