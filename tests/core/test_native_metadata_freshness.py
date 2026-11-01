"""Accepted deliveries advance independently of native model-input revisions."""
from dataclasses import replace
import json

import pytest

from tests.core.test_metadata_runtime import entry, runtime_store
from yt_playlist.core import metadata_native, metadata_runtime_files as files
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.core.store import Store


def observation(value=.8, **changes):
    return replace(MetadataObservation('mood_party', value, 'recording', 'native-id', 'prediction',
        unit='classifier_probability', model_version='classifier-1'), **changes)


def record(store, track, values, run, at, provider='native'):
    store.enrichment_state.record_metadata_observations(track, provider, values, run_id=run, now=at)


def source(provider='native', scope='recording', entity='native-id'):
    return json.dumps([provider, scope, entity], separators=(',', ':'))


def inputs(store, key=None):
    return metadata_native.source_values(store.conn, key or source())


def test_newer_identical_delivery_blocks_older_changes_and_changed_replay(runtime_store):
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    record(store, track, [observation()], 'newest', 30)
    record(store, track, [observation(.1)], 'late', 20)
    assert inputs(store)['mood_party']['value'] == .8
    with pytest.raises(ValueError, match='cannot change its result'):
        record(store, track, [observation(.2)], 'newest', 30)
    assert inputs(store)['mood_party']['value'] == .8


def test_identical_deliveries_keep_input_revision_files_and_work_unchanged(runtime_store):
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    before = inputs(store)
    tables = ('rec_musical_sources', 'rec_musical_pending', 'rec_content_pending')
    rows = {table: list(map(tuple, store.conn.execute('SELECT * FROM '+table))) for table in tables}
    paths = {path: path.read_bytes() for path in files.directory(store.conn).rglob('*.json')}
    for number in range(20):
        record(store, track, [observation()], 'unchanged-'+str(number), 30+number)
    assert inputs(store) == before
    assert rows == {table: list(map(tuple, store.conn.execute('SELECT * FROM '+table))) for table in tables}
    assert paths == {path: path.read_bytes() for path in files.directory(store.conn).rglob('*.json')}
    for table in ('metadata_deliveries', 'native_metadata_observations', 'musical_source_observations'):
        assert store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0


def test_equal_time_first_delivery_wins_and_replay_does_not_renew_its_time(runtime_store):
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    record(store, track, [observation(.1)], 'tie', 10)
    assert inputs(store)['mood_party']['value'] == .8
    record(store, track, [observation()], 'first', 100)
    record(store, track, [observation(.3)], 'next', 20)
    assert inputs(store)['mood_party']['value'] == .3


def test_partial_deliveries_keep_other_fields_and_exact_run_result(runtime_store):
    store = runtime_store
    track, _ = entry(store)
    mood, energy = observation(), observation(.4, field='energy')
    record(store, track, [mood, energy], 'initial', 10)
    record(store, track, [mood], 'partial', 30)
    record(store, track, [observation(.1, field='energy')], 'late', 20)
    assert {key: value['value'] for key, value in inputs(store).items()} == {'mood_party': .8, 'energy': .4}
    with pytest.raises(ValueError, match='cannot change its result'):
        record(store, track, [mood, energy], 'partial', 30)
    record(store, track, [observation(.6, field='energy')], 'new-energy', 40)
    assert {key: value['value'] for key, value in inputs(store).items()} == {'mood_party': .8, 'energy': .6}
    assert inputs(store)['mood_party']['provenance']['model_version'] == 'classifier-1'


def test_same_run_shared_source_can_link_another_recording(runtime_store):
    store = runtime_store
    first, _ = entry(store, 'first')
    second, _ = entry(store, 'second')
    record(store, first, [observation(scope='artist', match_evidence={'input_title': 'First'})], 'shared', 10)
    record(store, second, [observation(scope='artist', match_evidence={'input_title': 'Second'})], 'shared', 10)
    assert store.conn.execute('SELECT count(*) FROM rec_musical_links').fetchone()[0] == 2
    with pytest.raises(ValueError, match='cannot change its result'):
        record(store, second, [observation(scope='artist', model_version='different')], 'shared', 10)


def test_identical_acceptance_during_profile_calculation_keeps_publication(runtime_store, monkeypatch):
    from yt_playlist.rec import musical_preparation, musical_profiles
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    calculate = musical_profiles.source_profile

    def accept_unchanged(values):
        result = calculate(values)
        record(store, track, [observation()], 'newest', 30)
        return result

    monkeypatch.setattr(musical_profiles, 'source_profile', accept_unchanged)
    assert musical_preparation.source_step(store, 40)
    assert not store.musical_profiles.pending()
    assert store.conn.execute('SELECT count(*) FROM rec_musical_source_profiles').fetchone()[0] == 1


def test_changed_link_during_profile_calculation_still_prevents_publication(runtime_store, monkeypatch):
    from yt_playlist.rec import musical_preparation, musical_profiles
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    calculate = musical_profiles.source_profile

    def unlink(values):
        result = calculate(values)
        with store.conn:
            store.conn.execute('DELETE FROM rec_musical_links')
        return result

    monkeypatch.setattr(musical_profiles, 'source_profile', unlink)
    assert not musical_preparation.source_step(store, 40)
    assert store.conn.execute('SELECT count(*) FROM rec_musical_source_profiles').fetchone()[0] == 0


def test_a_reader_consuming_the_delivery_token_still_guards_it(runtime_store):
    from yt_playlist.core.db_revisions import keyed_reads
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    with store.read_snapshot() as snapshot:
        with keyed_reads(snapshot.store, 'rec_musical_links', [source()]):
            row = snapshot.store.conn.execute('SELECT delivery_id FROM rec_musical_links WHERE source_key=?',
                                              (source(),)).fetchone()
        assert row[0] == 'first'
        record(store, track, [observation()], 'newest', 30)
        assert not snapshot.publish(lambda: pytest.fail('Published stale delivery bookkeeping'))


@pytest.mark.parametrize('change', [dict(provider='other'), dict(scope='artist'), dict(entity_id='other-id')])
def test_acceptance_is_scoped_to_provider_and_source_entity(runtime_store, change):
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'same-run', 30)
    provider = change.get('provider', 'native')
    value = observation(.1, **{key: value for key, value in change.items() if key != 'provider'})
    record(store, track, [value], 'same-run', 10, provider)
    key = source(provider, value.scope, value.entity_id)
    assert inputs(store, key)['mood_party']['value'] == .1
    assert inputs(store)['mood_party']['value'] == .8


def test_freshness_survives_relocation_and_reopen(runtime_store, tmp_path):
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    record(store, track, [observation()], 'newest', 30)
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = files.export(store.conn, tmp_path/'export')
    moved = Store(tmp_path/'moved.db')
    try:
        store.conn.backup(moved.conn)
        files.restore(moved.conn, manifest)
        moved.init_schema()
        record(moved, track, [observation(.1)], 'late', 20)
        assert inputs(moved)['mood_party']['value'] == .8
        with pytest.raises(ValueError, match='cannot change its result'):
            record(moved, track, [observation(.1)], 'newest', 30)
        assert not moved.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        moved.conn.close()


def test_failed_publication_rolls_back_freshness_and_allows_retry(runtime_store, monkeypatch):
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    record(store, track, [observation()], 'newest', 30)
    before = list(store.conn.iterdump())
    publish = files.publish

    def interrupt(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError('interrupted publication')

    monkeypatch.setattr(files, 'publish', interrupt)
    with pytest.raises(RuntimeError, match='interrupted publication'):
        record(store, track, [observation(.2)], 'failed', 40)
    assert list(store.conn.iterdump()) == before
    monkeypatch.setattr(files, 'publish', publish)
    record(store, track, [observation(.3)], 'retry', 40)
    record(store, track, [observation(.1)], 'late', 35)
    assert inputs(store)['mood_party']['value'] == .3


def test_unchanged_delivery_freshness_is_rolled_back_with_its_caller(runtime_store):
    from yt_playlist.core import metadata_observations
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    before = list(store.conn.iterdump())
    with pytest.raises(RuntimeError, match='caller rollback'):
        with store.conn:
            metadata_observations.record(store.conn, track, 'native', [observation()], run_id='failed', now=50)
            raise RuntimeError('caller rollback')
    assert list(store.conn.iterdump()) == before
    record(store, track, [observation(.2)], 'next', 40)
    assert inputs(store)['mood_party']['value'] == .2


def test_existing_active_source_lazily_adopts_its_known_capture(runtime_store):
    store = runtime_store
    track, _ = entry(store)
    record(store, track, [observation()], 'first', 10)
    # An already-active #681 source has capture provenance in its immutable
    # file, but predates the additive current-delivery owner.
    with store.conn:
        store.conn.execute('DELETE FROM metadata_native_delivery_heads')
    record(store, track, [observation(.1)], 'late', 5)
    assert inputs(store)['mood_party']['value'] == .8
    record(store, track, [observation()], 'newest', 30)
    record(store, track, [observation(.1)], 'late', 20)
    assert inputs(store)['mood_party']['value'] == .8
