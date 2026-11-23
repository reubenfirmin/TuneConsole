from datetime import datetime, timezone
import json

import numpy as np
import pytest

from yt_playlist.library import listening_composition, mix_listening
from yt_playlist.rec import embedding_vectors, measurement, mode_inputs, rec_params, scoring, surfaces
from tests.conftest import seed_catalog_track


def test_prepared_reading_reuses_plays_and_updates_at_window_boundaries(store, monkeypatch):
    midnight = datetime(2026, 2, 20, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr('yt_playlist.library.listening_window.machine_timezone', lambda: 'UTC')
    account = store.accounts.upsert_account('main', None, True)
    keys = ['today-a', 'today-b', 'now-a', 'now-b']
    for key in keys:
        seed_catalog_track(store, key, key, 'Artist', video_id='v-' + key)
    store.rec_model.replace_rec_vectors([(key, np.array(direction, dtype=np.float32).tobytes())
        for key, direction in zip(keys, ([1, 0], [1, 0], [0, 1], [0, 1]), strict=True)])
    rec_params.set_param(store, 'now_min_events', 2)
    earlier = [(key, 'v-' + key, midnight - 7200 + i * 60) for i, key in enumerate(keys[:2])]
    store.history.import_listening_events(account, earlier, source_timezone='UTC')
    start = midnight - 60
    store.history.record_play_event(account, keys[2], 'v-now-a', start, source_timezone='UTC')
    assert mix_listening.prepare(store, start)
    _, matrix, index = embedding_vectors.load_vectors(store)
    assert matrix is not None
    half = rec_params.get_param(store, 'session_halflife_h')
    today_gain = rec_params.get_param(store, 'session_alpha')
    now_gain = rec_params.get_param(store, 'now_gain')
    current, today = listening_composition.track_tilts(store, start, matrix, index)
    assert current is None
    assert mix_listening.read(store, start).direction('collaborative', start, 2) == pytest.approx(today * today_gain)

    def forbidden(*args, **kwargs):
        pytest.fail('a compatible reading must be reused without reacquiring play history')

    with monkeypatch.context() as patch:
        patch.setattr(mix_listening, '_capture', forbidden)
        at = start + 10
        assert not mix_listening.prepare(store, at)
        reading = mix_listening.read(store, at)
        expected = today * today_gain * 2 ** (-10 / (3600 * half))
        assert reading.direction('collaborative', at, 2) == pytest.approx(expected)

    at = start + 30
    store.history.record_play_event(account, keys[3], 'v-now-b', at, source_timezone='UTC')
    assert mix_listening.prepare(store, at)
    assert mix_listening.read(store, at).direction('collaborative', at, 2)[1] == pytest.approx(now_gain)

    assert mix_listening.prepare(store, midnight)
    assert mix_listening.read(store, midnight).direction('collaborative', midnight, 2) == pytest.approx([0., now_gain])
    store.rec_model.replace_rec_vectors([(key, np.array([1, 0], dtype=np.float32).tobytes()) for key in keys])
    assert mix_listening.read(store, midnight).direction('collaborative', midnight, 2) is None
    assert mix_listening.prepare(store, midnight)
    assert mix_listening.read(store, midnight).direction('collaborative', midnight, 2) == pytest.approx([now_gain, 0.])
    expired = midnight + 1800
    assert mix_listening.prepare(store, expired)
    assert mix_listening.read(store, expired).direction('collaborative', expired, 2) is None


def test_shared_ranks_work_without_playlists_or_recent_scoring(store, monkeypatch):
    for key in ('a', 'b'):
        seed_catalog_track(store, key, 'Track ' + key, 'Artist ' + key, video_id='v-' + key)
    store.rec_model.replace_rec_vectors([(key, np.array(direction, dtype=np.float32).tobytes())
        for key, direction in (('a', [1, 0]), ('b', [0, 1]))])
    now = 100_000.
    mix_listening.prepare(store, now)
    prepared = mix_listening.read(store, now)
    assert prepared.spaces == {}

    def forbidden(*args, **kwargs):
        pytest.fail('prepared Mix Tape ranks must omit RECENT')

    monkeypatch.setattr(scoring.transient, 'centroid_tilt', forbidden)
    monkeypatch.setattr(scoring.transient, 'facet_leans', forbidden)
    assert {row.key for row in surfaces.explore_for_you(store, now, mix_listening=prepared)} == {'a', 'b'}


def test_publication_clears_wait_and_the_next_conflict_starts_a_new_one(file_backed_store, monkeypatch):
    store = file_backed_store
    now = 1795104000.
    account = store.accounts.upsert_account('main', None, True)
    tracks = [('first', [1, 0]), ('second', [1, 0]), ('third', [0, 1]), ('fourth', [0, 1])]
    for key, _ in tracks:
        seed_catalog_track(store, key, key, 'Artist', video_id='v-' + key)
    store.rec_model.replace_rec_vectors([(key, np.array(direction, dtype=np.float32).tobytes())
                                         for key, direction in tracks])
    rec_params.set_param(store, 'now_min_events', 2)
    for offset, key in enumerate(('first', 'second')):
        assert store.history.record_play_event(account, key, 'v-' + key, now - 30 + 10 * offset)
    assert mix_listening.prepare(store, now)
    previous = mix_listening.rank_input(store, now)
    assert previous

    capture = mix_listening._capture
    diagnostics = store.maintenance_diagnostics
    clock = [diagnostics.clock()]
    monkeypatch.setattr(diagnostics, 'clock', lambda: clock[0])
    phase = mix_listening.PHASE

    def defer(at):
        def concurrent_catalog_write(reader, timestamp, checkpoint):
            result = capture(reader, timestamp, checkpoint)
            store.tracks.upsert_track(f'unrelated-{at}', f'Unrelated {at}', 'Other Artist', '', 200)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(mix_listening, '_capture', concurrent_catalog_write)
            assert not mix_listening.prepare(store, at)

    assert store.history.record_play_event(account, 'third', 'v-third', now + 10)
    defer(now + 10)
    assert mix_listening.rank_input(store, now + 10) == previous
    assert diagnostics.waiting[phase] == clock[0]
    assert diagnostics.reasons[phase, 'source:recordings'] == 1

    clock[0] += 1
    assert mix_listening.prepare(store, now + 11)
    applied = mix_listening.rank_input(store, now + 11)
    assert applied != previous
    assert mix_listening.read(store, now + 11).direction('collaborative', now + 11, 2)[1] > 0
    assert phase not in diagnostics.waiting

    clock[0] += 1
    assert store.history.record_play_event(account, 'fourth', 'v-fourth', now + 20)
    defer(now + 20)
    assert mix_listening.rank_input(store, now + 20) == applied
    assert diagnostics.waiting[phase] == clock[0]
    assert diagnostics.reasons[phase, 'source:recordings'] == 2


def test_shared_pool_policy_records_the_applied_listening_result(store):
    account = store.accounts.upsert_account('main', None, True)
    now = 100_000.
    rec_params.set_param(store, 'now_min_events', 2)
    rows = []
    for key, direction in (('a', [1, 0]), ('b', [1, 0]), ('c', [0, 1])):
        seed_catalog_track(store, key, key, 'Artist', video_id='v-' + key)
        rows.append((key, np.array(direction, dtype=np.float32).tobytes()))
    store.rec_model.replace_rec_vectors(rows)
    for index, key in enumerate(('a', 'b')):
        store.history.record_play_event(account, key, 'v-' + key, now - 30 + index * 10, source_timezone='UTC')

    def capture():
        with store.read_snapshot() as snapshot:
            policies = measurement.PoolPolicies(snapshot.store, now)
            bundles, _ = mode_inputs._calculate_bundles(snapshot.store, now, policies)
            pool = bundles['all']['wheelhouse']
            version = pool[0]['pool_policy']
            base = json.loads(policies.rows[version][1])['_policy_base']
            return version, json.loads(policies.bases[base][1])

    neutral, neutral_policy = capture()
    assert mix_listening.prepare(store, now)
    active, active_policy = capture()
    assert active != neutral
    assert neutral_policy['mix_listening']['directions'] == {}
    assert active_policy['mix_listening']['horizons'] == ['NOW', 'TODAY']
    assert set(active_policy['mix_listening']['directions']) == {'collaborative'}
    assert 'recent_plays' not in active_policy and 'facet_leans' not in active_policy
    assert capture() == (active, active_policy)
