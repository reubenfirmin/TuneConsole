"""Selective v64 repair retains evidence through current bounded preparation."""
from contextlib import closing
import json
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core.test_catalog import complete_regular_input
from tests.genre_catalog_support import bound_catalog
from tests.musical_support import prepare_profiles
from yt_playlist.core import metadata_native, schema_definition
from yt_playlist.core.migrations import registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store
from yt_playlist.rec import musical_preparation, musical_profiles, vibe_preparation


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """No web lifespan or remote providers participate in these owners."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('musical-retraction-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def retraction_predecessor():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        runner.migrate(conn, registry=registry.MIGRATIONS[:63], clock=lambda: 1.)
        yield conn


def test_retraction_predecessor_is_the_actual_frozen_v63_contract(retraction_predecessor):
    runner.validate_version(retraction_predecessor, 63)
    assert retraction_predecessor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 63


def source_rows(conn):
    return {row['source_key']: dict(row) for row in conn.execute(
        'SELECT s.*,p.digest FROM rec_musical_sources s '
        'JOIN rec_musical_source_profiles p USING(source_key) ORDER BY s.source_key')}


@pytest.fixture(scope='module')
def retraction_seeded(retraction_predecessor, tmp_path_factory):
    path = tmp_path_factory.mktemp('musical-retraction') / 'library.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        retraction_predecessor.backup(conn)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        records = {}

        def delivery(record, entities, delivery_id):
            recording, entry, provider = record
            for entity in entities:
                provenance = {'schema': 1, 'scope': 'artist', 'entity_id': entity,
                              'kind': 'reported', 'delivery_id': delivery_id,
                              'match_evidence': {'method': 'title_artist_match'}}
                conn.execute('INSERT INTO metadata_findings(entry_id,recording_id,provider,field,value_json,provenance_json,observed_at) VALUES (?,?,?,?,?,?,10)',
                    (entry, recording, provider, 'tags', json.dumps([{'name': 'Ambient' if entity == 'a' else 'Metal'}]), json.dumps(provenance)))

        with conn:
            for name in ('kept', 'contracted'):
                key, video = name + ' song|artist', name + '-video'
                recording = conn.execute('INSERT INTO recordings(identity_key,title,artist) VALUES (?,\'Song\',\'Artist\')', (key,)).lastrowid
                conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist,duration_s) VALUES (?, ?,\'Song\',\'Artist\',200)', (video, recording))
                entry = conn.execute('INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,?)', (recording, video)).lastrowid
                conn.execute("INSERT INTO library_memberships VALUES (?,'fixture',NULL)", (entry,))
                conn.execute("INSERT INTO recording_aliases VALUES (?,?,'fixture')", (key, recording))
                records[key] = (recording, entry, 'fixture-retraction-' + name)
                delivery(records[key], ('a', 'b'), 'initial')
            profiles = {}
            for row in conn.execute('SELECT * FROM rec_musical_sources').fetchall():
                profile = musical_profiles.source_profile({**dict(row), 'observations': json.loads(row['payload'])})
                profiles.setdefault(row['provider'], []).append(profile)
                conn.execute('INSERT INTO rec_musical_source_profiles VALUES (?,?,?,?,?)',
                    (row['source_key'], musical_profiles.canonical(profile), musical_profiles.VERSION, musical_profiles.digest(profile), 1000))
            for key, (_, _, provider) in records.items():
                profile = musical_profiles.resolve(profiles[provider])
                conn.execute('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?)',
                    (key, musical_profiles.canonical(profile), musical_profiles.VERSION, musical_profiles.digest(profile), 1000))
            conn.execute('DELETE FROM rec_musical_pending')
            conn.execute('DELETE FROM rec_content_pending')
        before = source_rows(conn)
        versions = {key: json.loads(payload)['source_versions'] for key, payload in conn.execute(
            'SELECT identity_key,payload FROM rec_musical_profiles')}
        with conn:
            delivery(records['contracted song|artist'], ('a',), 'old-contraction')
        yield conn, path, records, before, versions


def test_old_contraction_strands_only_one_profile_without_rewriting_source_history(retraction_seeded):
    conn, _, records, before, versions = retraction_seeded
    assert len(before) == 4
    assert source_rows(conn) == before
    assert not conn.execute('SELECT 1 FROM rec_content_pending').fetchone()
    for key, (recording, _, _) in records.items():
        assert conn.execute('SELECT count(*) FROM rec_musical_links WHERE recording_id=?', (recording,)).fetchone()[0] == (1 if key.startswith('contracted') else 2)
        assert json.loads(conn.execute('SELECT payload FROM rec_musical_profiles WHERE identity_key=?', (key,)).fetchone()[0])['source_versions'] == versions[key]


@pytest.fixture(scope='module')
def retraction_upgrade(retraction_seeded):
    original, path, records, before, versions = retraction_seeded
    original.close()
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        yield conn, path, records, before, versions


def test_restarted_v64_repair_queues_only_the_stranded_profile_and_preserves_all_evidence(retraction_upgrade):
    conn, _, _, before, versions = retraction_upgrade
    runner.migrate(conn, registry=registry.MIGRATIONS[:64], clock=lambda: 1.)
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 64
    assert source_rows(conn) == before
    assert [row[0] for row in conn.execute('SELECT identity_key FROM rec_content_pending')] == ['contracted song|artist']
    for key, payload in conn.execute('SELECT identity_key,payload FROM rec_musical_profiles'):
        assert json.loads(payload)['source_versions'] == versions[key]


def assert_sources(conn, before, native):
    expected = {key: {name: value for name, value in row.items() if not native or name != 'payload'}
                for key, row in before.items()}
    assert source_rows(conn) == expected
    if native:
        for key, row in before.items():
            assert metadata_native.source_values(conn, key) == json.loads(row['payload'])
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('target', [66, 67, 68, 69, 79, 80, 88, 94, 102])
def test_later_registered_owners_keep_the_original_retracted_source_history(retraction_upgrade, target):
    conn, _, _, before, _ = retraction_upgrade
    runner.migrate(conn, registry=registry.MIGRATIONS[:target], clock=lambda: 1.)
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == target
    assert_sources(conn, before, target >= 80)


def test_account_reference_retains_musical_source_owners(retraction_upgrade):
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions
    assert 'CREATE TABLE rec_musical_sources' in definitions


@pytest.fixture(scope='module')
def retraction_renamed(retraction_upgrade):
    conn, _, _, _, _ = retraction_upgrade
    runner.migrate(conn, registry=registry.MIGRATIONS[:102])
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(conn, registry=registry.MIGRATIONS[:103])
    restore.assert_called_once()
    guards.assert_called_once_with(conn)
    return retraction_upgrade, restore.call_args


def test_real_account_cutover_keeps_every_retracted_source_and_foreign_key(retraction_renamed):
    (conn, _, _, before, _), captured = retraction_renamed
    assert captured.args[0] is conn
    assert captured.args[1]
    assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
    assert_sources(conn, before, True)


@pytest.fixture(scope='module')
def retraction_restored(retraction_renamed):
    value, captured = retraction_renamed
    conn, _, _, _, _ = value
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return value


def test_real_program_restoration_keeps_musical_source_owners(retraction_restored):
    conn, _, _, before, _ = retraction_restored
    runner.validate_version(conn, 103, foreign_keys=False, data=False)
    assert_sources(conn, before, True)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_current_registered_owners_keep_the_retracted_source_history(retraction_restored, target):
    conn, _, _, before, _ = retraction_restored
    runner.migrate(conn, registry=registry.MIGRATIONS[:target], clock=lambda: 1.)
    assert_sources(conn, before, True)


@pytest.fixture(scope='module')
def retraction_regular(retraction_restored):
    conn, _, _, _, _ = retraction_restored
    runner.migrate(conn, registry=registry.MIGRATIONS)
    complete_regular_input(conn)
    return retraction_restored


def test_current_regular_admission_keeps_exact_retracted_source_inputs(retraction_regular):
    conn, _, _, before, _ = retraction_regular
    assert runner.migrate(conn, defer_validation=True) is False
    regular.validate(conn)
    assert_sources(conn, before, True)


@pytest.fixture(scope='module')
def retraction_store(retraction_regular):
    _, path, records, before, _ = retraction_regular
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        yield store, records, before
    finally:
        store.conn.close()


def test_fresh_store_admits_the_real_repair_queue_and_unchanged_evidence(retraction_store):
    store, _, before = retraction_store
    assert_sources(store.conn, before, True)
    assert store.prepared_content.has_pending()
    assert {row[0] for row in store.prepared_content.pending(32)} == {'contracted song|artist'}


def test_bounded_worker_rebuilds_only_linked_profile_contributions(retraction_store, monkeypatch):
    store, records, before = retraction_store
    # The ordinary capture worker and all profile SQL/files remain real.
    # Fitting unrelated vibe models has independent generation owners.
    monkeypatch.setattr(vibe_preparation, 'step', Mock(return_value=False))
    prepare_profiles(store)
    profiles = store.musical_profiles.by_keys(list(records))
    for key, (_, _, provider) in records.items():
        expected = {row['digest'] for row in before.values() if row['provider'] == provider
                    and (not key.startswith('contracted') or row['entity_id'] == 'a')}
        assert set(profiles[key]['source_versions']) == expected
    assert_sources(store.conn, before, True)
    assert not store.prepared_content.has_pending()
    assert not musical_preparation.due(store)


def test_final_reopen_keeps_repaired_profiles_and_does_not_requeue_work(retraction_store):
    source, records, before = retraction_store
    expected = source.musical_profiles.by_keys(list(records))
    store = Store(source._db_path)
    try:
        store.init_schema()
        assert store.musical_profiles.by_keys(list(records)) == expected
        assert not store.prepared_content.has_pending()
        assert_sources(store.conn, before, True)
    finally:
        store.conn.close()
