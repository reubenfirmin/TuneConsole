"""Compatible metadata selection participates in bounded profile snapshots."""
import json
from pathlib import Path

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_metadata_destinations import stage
from tests.rec.test_musical_preparation import observe
from yt_playlist.core import metadata_destination_files
from yt_playlist.core.preparation import metadata_evidence, metadata_destinations
from yt_playlist.rec import musical_preparation, musical_profiles
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo


def prepare(store, state):
    if state == 'unstarted':
        return None
    metadata_evidence.JOB.install(store.conn)
    if state == 'partial':
        metadata_destinations.install(store.conn)
        metadata_evidence.JOB.step(store.conn, batch_size=1)
        assert metadata_destinations.selected(store.conn) is None
        return None
    identity = stage(store.conn)
    if state == 'selected':
        metadata_destinations.publish(store.conn, identity=identity)
    return identity


def saved(store, key):
    return tuple(store.conn.execute('SELECT * FROM rec_musical_source_profiles WHERE source_key=?', (key,)).fetchone())


def intensity(store, key):
    row = store.conn.execute('SELECT payload FROM rec_musical_source_profiles WHERE source_key=?', (key,)).fetchone()
    return json.loads(row[0])['dimensions']['intensity']['value']


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('state', ['unstarted', 'partial', 'staged', 'selected'])
def test_source_profile_works_through_metadata_preparation(installed, state):
    store, _ = installed
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    observe(store, tid, [{'name': 'Metal'}])
    key = store.musical_profiles.pending()
    prepare(store, state)
    assert musical_preparation.source_step(store, 1000)
    assert intensity(store, key) > .5
    assert store.musical_profiles.pending() is None
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('state', ['partial', 'staged', 'selected'])
def test_source_change_during_calculation_keeps_last_good_and_retries(installed, state, monkeypatch):
    store, _ = installed
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    observe(store, tid, [{'name': 'Metal'}])
    key = store.musical_profiles.pending()
    prepare(store, state)
    assert musical_preparation.source_step(store, 1000)
    before = saved(store, key)
    store.musical_profiles.queue_stale('force-rebuild', 1)
    original = musical_profiles.source_profile

    def changed(source):
        result = original(source)
        observe(store, tid, [{'name': 'Ambient'}], run='changed')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(musical_profiles, 'source_profile', changed)
        assert not musical_preparation.source_step(store, 1001)
    assert saved(store, key) == before
    assert store.musical_profiles.pending() == key
    assert musical_preparation.source_step(store, 1002)
    assert intensity(store, key) < .5


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('owner', ['published_metadata_head', 'published_metadata_destination_head'])
def test_each_selection_head_change_rejects_stale_publication(installed, owner, monkeypatch):
    store, _ = installed
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    observe(store, tid, [{'name': 'Metal'}])
    key = store.musical_profiles.pending()
    prepare(store, 'selected')
    assert musical_preparation.source_step(store, 1000)
    before = saved(store, key)
    store.musical_profiles.queue_stale('force-rebuild', 1)
    original = musical_profiles.source_profile

    def withdraw(source):
        result = original(source)
        with store.conn:
            store.conn.execute('DELETE FROM ' + owner + ' WHERE id=1')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(musical_profiles, 'source_profile', withdraw)
        assert not musical_preparation.source_step(store, 1001)
    assert saved(store, key) == before
    assert musical_preparation.source_step(store, 1002)
    assert intensity(store, key) > .5


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('change', ['first-selection', 'successor'])
def test_selection_change_before_pinning_defers_without_reading_new_references(installed, change, monkeypatch):
    store, _ = installed
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    observe(store, tid, [{'name': 'Metal'}])
    key = store.musical_profiles.pending()
    identity = prepare(store, 'staged' if change == 'first-selection' else 'selected')
    original = MusicalProfilesRepo.source_dependencies

    def change_selection(repo, source_key):
        dependencies = original(repo, source_key)
        target = identity
        if change == 'successor':
            observe(store, tid, [{'name': 'Ambient'}], run='changed')
            target = stage(store.conn)
        metadata_destinations.publish(store.conn, identity=target)
        return dependencies

    with monkeypatch.context() as patch:
        patch.setattr(MusicalProfilesRepo, 'source_dependencies', change_selection)
        assert not musical_preparation.source_step(store, 1000)
    assert store.musical_profiles.pending() == key
    assert not store.conn.execute('SELECT 1 FROM rec_musical_source_profiles WHERE source_key=?', (key,)).fetchone()
    assert musical_preparation.source_step(store, 1001)
    assert (intensity(store, key) > .5) == (change == 'first-selection')


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('state', ['partial', 'staged', 'selected'])
def test_unrelated_source_arrival_does_not_discard_unaffected_inputs(installed, state, monkeypatch):
    store, _ = installed
    one = store.tracks.upsert_track('one', 'One', 'Artist', None, 200)
    two = store.tracks.upsert_track('two', 'Two', 'Other', None, 200)
    observe(store, one, [{'name': 'Metal'}])
    observe(store, two, [{'name': 'Ambient'}], entity='other')
    key = store.musical_profiles.pending()
    prepare(store, state)
    # Withdraw selected metadata before the slice. A later unrelated source
    # arrival cannot change the absent selection or this source's raw payload.
    # An active selected head being withdrawn is separately required to defer.
    observe(store, two, [{'name': 'Jazz'}], entity='other', run='withdraw')
    original = musical_profiles.source_profile

    def unrelated(source):
        result = original(source)
        observe(store, two, [{'name': 'Ambient'}], entity='other', run='unrelated')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(musical_profiles, 'source_profile', unrelated)
        assert musical_preparation.source_step(store, 1000)
    assert intensity(store, key) > .5


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('recovery', ['repair', 'restore'])
def test_delivery_recovery_during_calculation_preserves_output_and_continues(installed, recovery, tmp_path, monkeypatch):
    store, _ = installed
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    observe(store, tid, [{'name': 'Metal'}])
    key = store.musical_profiles.pending()
    prepare(store, 'selected')
    assert musical_preparation.source_step(store, 1000)
    before = saved(store, key)
    store.musical_profiles.queue_stale('force-rebuild', 1)
    with store.read_snapshot() as snapshot:
        manifest = metadata_destination_files.export(snapshot.store.conn, tmp_path / 'export')
    delivery = store.conn.execute('SELECT d.id,d.filename FROM '+metadata_destinations.table('native')+' n JOIN '+
        metadata_destinations.table('deliveries')+' d ON d.id=n.delivery_id WHERE n.source_key=?', (key,)).fetchone()
    original = musical_profiles.source_profile

    def recovered(source):
        result = original(source)
        Path(delivery['filename']).unlink()
        if recovery == 'repair':
            metadata_destinations.repair(store.conn, delivery['id'])
        else:
            metadata_destination_files.restore(store.conn, manifest)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(musical_profiles, 'source_profile', recovered)
        assert musical_preparation.source_step(store, 1001) == (recovery == 'repair')
    if recovery == 'restore':
        assert saved(store, key) == before
        assert musical_preparation.source_step(store, 1002)
    assert intensity(store, key) > .5
    assert metadata_destinations.source_values(store.conn, key)['tags']['value'] == [{'name': 'Metal'}]


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_nonprofile_observations_can_stage_with_nullable_source_references(installed):
    store, _ = installed
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    observe(store, tid, [{'name': 'Metal'}])
    observe(store, tid, [{'name': 'Unresolved item'}], scope='item', entity='item', run='item')
    metadata_evidence.JOB.install(store.conn)
    metadata_destinations.install(store.conn)
    # The worker can bind revision counters before later preparation batches.
    store._snapshot_revisions.refresh()
    prepare(store, 'selected')
    assert store.conn.execute('SELECT 1 FROM '+metadata_destinations.table('native')+' WHERE source_key IS NULL').fetchone()
    assert musical_preparation.source_step(store, 1000)


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_selected_profile_reads_only_its_source_and_delivery_keys(installed):
    store, _ = installed
    one = store.tracks.upsert_track('one', 'One', 'Artist', None, 200)
    two = store.tracks.upsert_track('two', 'Two', 'Other', None, 200)
    observe(store, one, [{'name': 'Metal'}])
    observe(store, two, [{'name': 'Ambient'}], entity='other')
    key = store.musical_profiles.pending()
    prepare(store, 'selected')
    expected_files = {('published_metadata_destination_deliveries', 'key:' + str(row[0]))
        for row in store.conn.execute('SELECT delivery_id FROM published_metadata_destination_native WHERE source_key=?', (key,))}
    declared, references = store.musical_profiles.source_dependencies(key)
    with store.read_snapshot(dependencies=declared) as snapshot:
        assert snapshot.store.musical_profiles.source_input(key, observation_ids=references)
        consumed = {dependency for dependency in snapshot.store._snapshot_reads.dependencies
                    if dependency[0].startswith('published_metadata_')}
        assert consumed == expected_files | {
            ('published_metadata_head', 'key:1'), ('published_metadata_destination_head', 'key:1'),
            ('published_metadata_destination_native', 'key:' + key)}
        assert consumed <= declared
        assert snapshot.publish(lambda: None)
