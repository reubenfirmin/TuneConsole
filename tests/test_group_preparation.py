import json
import hashlib
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest

from tests.group_support import recordings
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, native_sql_calls, without_query_timing,
)
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core import prepared_inputs, schema_inspection
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import group_discovery, group_preparation, musical_profiles


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Group preparation owns SQL/files, without the web lifecycle."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('death-metal', 'Death Metal'), ('pop', 'Pop'),
                                  ('house', 'House'), ('jazz', 'Jazz'))) as schema:
        yield schema


@pytest.fixture
def store(tmp_path, _pristine_current_schema, monkeypatch):
    # Keep real private readers, guarded snapshots, source-change rejection,
    # cancellation and file publication. A file reader avoids copying the
    # entire in-memory catalog for every preparation turn.
    connect = sqlite3.connect

    def cached_connection(*args, **kwargs):
        # These consumers repeatedly query one fixed installed schema. SQLite
        # still executes every statement, transaction and publication fence.
        kwargs['cached_statements'] = 512
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', cached_connection)
    with _file_consumer_store(tmp_path, _pristine_current_schema) as database:
        reader = database._make_reader()

        @contextmanager
        def read_view(**kwargs):
            # Preparation's actual snapshots stay independent. Reporting keeps
            # a real committed reader, with artifact leases scoped to each call.
            with database._retained_readers():
                yield reader

        monkeypatch.setattr(database, 'read_view', read_view)
        with closing(reader.conn), schema_inspection.reuse_tables(reader.conn):
            yield database


def seed(store, tracks):
    with store.conn:
        for i, track in enumerate(tracks):
            library = {'genre': track.genre, 'year': str(track.year or ''), 'audio': {}}
            payload = json.dumps({'library': library, 'discovered': None})
            store.conn.execute('INSERT INTO recordings(identity_key) VALUES (?) '
                               'ON CONFLICT(identity_key) DO NOTHING', (track.key,))
            prepared_inputs.upsert(store.conn, track.key, json.loads(payload),
                                   hashlib.sha256(payload.encode()).hexdigest(), 1, i + 1)
            profile = {'schema': 1, 'version': musical_profiles.VERSION, 'dimensions': {
                name: {'value': value, 'support': support, 'evidence': [{'kind': 'explicit'}]}
                for name, value, support in zip(musical_profiles.DIMENSIONS, track.values, track.support)}}
            store.conn.execute('INSERT OR REPLACE INTO rec_musical_profiles VALUES (?,?,?,?,?)',
                               (track.key, json.dumps(profile), musical_profiles.VERSION, str(i), 1))


def observe_artist(store, track_id, artist, genre):
    store.enrichment_state.record_metadata_observations(
        track_id, 'fixture', [MetadataObservation(
            'tags', [{'name': genre, 'count': 10}], 'artist', artist, 'reported')],
        run_id='group-preparation', now=10)


def test_background_shadow_is_coalesced_and_does_not_replace_live_modes(store):
    tracks = recordings([('Death Metal', 80, .9), ('Pop', 12, .2)])
    seed(store, tracks)
    worker = group_preparation.Preparation(store)
    assert worker.due(1000) and worker.step(1000)
    head = store.taste_groups.head(members=True)
    assert head['payload']['shadow'] and head['payload']['serving_policy'] == 'legacy'
    assert not worker.due(1060)
    assert not store.modes.list_modes()
    # Even one change is checked after coalescing; unchanged inputs are cheap acknowledgements.
    with store.conn:
        store.conn.execute('UPDATE rec_musical_profiles SET prepared_at=2 WHERE identity_key=?', (tracks[0].key,))
    assert worker.due(1100)
    # Unchanged musical inputs advance the acknowledgement, not the immutable definition.
    assert not worker.step(1100)
    assert not worker.due(1160)
    assert store.taste_groups.head()['generation'] == head['generation']
    worker.policy = replace(worker.policy, broad_edge=.61)
    assert not worker.due(1159)
    assert worker.due(1160) and worker.step(1160)
    current = store.taste_groups.head()
    assert current['generation'] != head['generation']
    assert current['policy'] == musical_profiles.canonical(worker.policy.manifest())


def test_group_preparation_never_loads_or_reimports_retired_mode_definitions(store, monkeypatch):
    seed(store, recordings([('House', 12, .2)]))
    def forbidden(*args, **kwargs):
        pytest.fail('Grouping tried to import legacy mode history')
    monkeypatch.setattr(type(store.modes), 'list_modes', forbidden)
    monkeypatch.setattr(type(store.taste_groups), 'legacy_versions', forbidden)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    assert all(not group.get('legacy') for group in store.taste_groups.head()['groups'])
    seed(store, recordings([('House', 13, .2)]))
    assert worker.due(1100) and worker.step(1100)


def test_stale_work_preserves_last_good_and_short_allowance_finishes_admitted_plan(store, monkeypatch):
    tracks = recordings([('Pop', 12, .2)])
    seed(store, tracks)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    first = store.taste_groups.head()
    seed(store, [replace(tracks[0], values=(.95,) * len(tracks[0].values))])
    original = group_discovery.discover

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        with store.conn:
            store.conn.execute("UPDATE rec_musical_profiles SET payload='{}'")
        return result

    monkeypatch.setattr(group_discovery, 'discover', race)
    assert worker.due(1100) and not worker.step(1100)
    assert store.taste_groups.head()['generation'] == first['generation']
    monkeypatch.setattr(group_discovery, 'discover', original)
    deferred = group_preparation.Preparation(store, budget=group_preparation.Budget(seconds=0))
    assert deferred.due(1200) and deferred.step(1200)
    assert store.taste_groups.head()['generation'] != first['generation']


def test_cancellation_propagates_without_publishing_a_partial_generation(store, monkeypatch):
    seed(store, recordings([('Pop', 12, .2)]))
    worker = group_preparation.Preparation(store)
    monkeypatch.setattr(group_discovery, 'discover', lambda *a, **kw: (_ for _ in ()).throw(WorkClosing('stop')))
    with pytest.raises(WorkClosing):
        worker.step(1000)
    assert store.taste_groups.head() is None


def test_checkpoint_failure_retries_after_the_complete_generation_commits(store, monkeypatch):
    seed(store, recordings([('Pop', 12, .2)]))
    worker = group_preparation.Preparation(store)
    repository = store.taste_groups.repository
    original = type(repository).checkpoint_preparation

    def fail_checkpoint(*_args):
        assert not store.conn.in_transaction
        raise RuntimeError('controlled checkpoint failure')

    monkeypatch.setattr(type(repository), 'checkpoint_preparation', fail_checkpoint)
    with pytest.raises(RuntimeError, match='controlled checkpoint failure'):
        worker.step(1000)
    generation = store.taste_groups.head_generation()
    assert generation and worker._pending is not None
    monkeypatch.setattr(type(repository), 'checkpoint_preparation', original)

    def forbid_restage(*_args):
        pytest.fail('A committed generation must not be staged again')

    monkeypatch.setattr(store.taste_groups, 'stage', forbid_restage)
    assert worker.step(1060)
    assert store.taste_groups.head_generation() == generation
    assert worker._pending is None
    assert json.loads(store.settings.get_setting(group_preparation.CHECKPOINT))['generation'] == generation


def test_new_supported_album_is_represented_on_the_next_update(store):
    metal = recordings([('Death Metal', 60, .9)])
    seed(store, metal)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    first = store.taste_groups.head(members=True)
    metal_id = first['groups'][0]['group_id']
    pop = recordings([('Pop', 12, .2)])
    seed(store, pop)
    assert worker.step(1100)
    second = store.taste_groups.head(members=True)
    assert any(group['group_id'] == metal_id and set(group['members']) == {r.key for r in metal}
               for group in second['groups'])
    assert any(set(group['members']) == {r.key for r in pop} for group in second['groups'])


@pytest.mark.parametrize('target_genre', ['Death Metal', 'House'])
def test_corrected_genre_or_profile_moves_on_the_next_update(store, target_genre):
    tracks = recordings([('House', 30, .1), (target_genre, 30, .95)])
    seed(store, tracks)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    original = store.taste_groups.head(members=True)
    leaves = [g for g in original['groups'] if g['leaf']]
    assert len(leaves) == 2
    target = next(g for g in leaves if tracks[-1].key in g['members'])
    changed = replace(tracks[-1], key=tracks[0].key)
    seed(store, [changed])
    assert worker.due(1900) and worker.step(1900)
    current = store.taste_groups.head(members=True)
    actual = next(g for g in current['groups'] if g['leaf'] and changed.key in g['members'])
    assert actual['group_id'] == target['group_id']
    assert set(actual['members']) == {r.key for r in tracks[30:]} | {changed.key}
    assert {g['group_id'] for g in current['groups']} == {g['group_id'] for g in original['groups']}
    assert current['payload']['last_structure_at'] == original['payload']['last_structure_at']
    historical = store.taste_groups.definitions(original['generation'], members=True)
    assert historical == original['groups']


def test_member_that_loses_all_support_becomes_unknown_without_stale_assignment(store):
    tracks = recordings([('House', 30, .1), ('House', 30, .95)])
    seed(store, tracks)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    original = store.taste_groups.head(members=True)
    changed = replace(tracks[0], genre='', values=(0.,) * len(tracks[0].values),
                      support=(0.,) * len(tracks[0].support))
    seed(store, [changed])
    assert worker.step(1900)
    current = store.taste_groups.head(members=True)
    assert all(changed.key not in g['members'] for g in current['groups'])
    unknown = store.conn.execute(
        'SELECT u.reason FROM group_unknown u '
        'JOIN group_generations g ON g.id=u.generation_id '
        'JOIN recordings r ON r.id=u.recording_id '
        'WHERE g.external_key=? AND r.identity_key=?',
        (current['generation'], changed.key)).fetchone()
    assert unknown and unknown[0] == 'insufficient-shared-evidence'
    assert store.taste_groups.definitions(original['generation'], members=True) == original['groups']


def test_initial_three_track_graph_splits_and_rejoins_while_backlog_continues(store):
    tracks = recordings([('House', 3, .1)])
    seed(store, tracks)
    pending = store.tracks.upsert_track('pending-source', 'Pending', 'Other Artist', '', 200)
    observe_artist(store, pending, 'other-artist', 'Jazz')
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    first = store.taste_groups.head(members=True)
    assert [g['size'] for g in first['groups'] if g['leaf']] == [3]
    changed = replace(tracks[-1], values=(.95,) * len(tracks[-1].values))
    seed(store, [changed])
    assert not worker.due(1030)  # Bounded coalescing, never a structural freeze.
    assert worker.due(1060) and worker.step(1060)
    second = store.taste_groups.head(members=True)
    assert sorted(g['size'] for g in second['groups'] if g['leaf']) == [1, 2]
    seed(store, [tracks[-1]])
    assert worker.step(1120)
    third = store.taste_groups.head(members=True)
    assert [g['size'] for g in third['groups'] if g['leaf']] == [3]
    assert third['groups'][0]['group_id'] == first['groups'][0]['group_id']
    assert store.prepared_content.has_pending() and store.musical_profiles.pending()
    assert store.taste_groups.definitions(first['generation'], members=True) == first['groups']
    assert store.taste_groups.definitions(second['generation'], members=True) == second['groups']


def test_fresh_library_publishes_from_real_capture_before_either_queue_drains(store):
    from tests.conftest import seed_catalog_track
    from yt_playlist.rec import group_serving, progressive

    for i in range(12):
        genre = ('House', 'House', 'Jazz')[i % 3]
        track_id = seed_catalog_track(
            store, f'fresh {i}|artist {i}', f'Fresh {i}', f'Artist {i}',
            video_id=f'fresh{i}', genre=genre)
        observe_artist(store, track_id, f'artist{i}', genre)
    preparation = progressive.Preparation(store, budget=replace(progressive.Budget(), batch=3))
    assert preparation.capture(1000)  # First cached source batch.
    assert preparation.capture(1001)  # First three recordings, without a full-corpus fit.
    assert preparation.groups.step(1002)
    assert group_serving.prepare(store, 1003)
    first = group_serving.view(store)
    assert first['counts']['known'] == 3
    assert first['groups'] and not store.modes.list_modes()
    assert not any(g['mix_ready'] for g in first['groups'])
    progress = group_serving.progress(store)
    assert progress['prepared'] == 3 and progress['pending_recordings'] == 9
    assert progress['sources'] == {'artist': 11}
    # More inputs improve the initial graph while both queues remain nonempty.
    # Qualified models share the bounded worker with source/recording turns.
    # Require useful progress before either queue drains, without fixing which
    # kind of work the next two turns must perform.
    for turn in range(32):
        preparation.capture(1060 + turn)
        if store.prepared_content.progress()['prepared'] == 6:
            break
    assert store.prepared_content.progress()['prepared'] == 6
    assert preparation.groups.step(1100), group_preparation.status(store)
    assert group_serving.prepare(store, 1101)
    assert group_serving.view(store)['counts']['known'] == 6
    assert store.prepared_content.has_pending() and store.musical_profiles.pending()
