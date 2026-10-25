"""Durable profiles share the accepted worker and its private publication guards."""
from dataclasses import replace
import time

import pytest

from yt_playlist.core import catalog
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.store import Store
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import musical_preparation, musical_profiles as profiles
from yt_playlist.rec.progressive import Budget, Preparation
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo


def observe(store, tid, value, *, entity='shared', scope='artist', field='tags', run='one', title=None):
    store.enrichment_state.record_metadata_observations(tid, 'fixture', [MetadataObservation(
        field, value, scope, entity, 'reported' if field == 'tags' else 'prediction',
        match_evidence={'method': 'title_artist_match', 'input_title': title})], run_id=run, now=10)


def drain(store, batch=32):
    p = Preparation(store, budget=replace(Budget(), batch=batch))
    for _ in range(500):
        if not store.prepared_content.has_pending() and not musical_preparation.due(store):
            return p
        assert p.capture(1000)
    pytest.fail('Profile work did not drain')


def get(store, key='song|artist'):
    return store.musical_profiles.by_keys([key])[key]['dimensions']


def test_shared_artist_fanout_is_paged_and_does_not_admit_content_refits(store):
    tids = [store.tracks.upsert_track(f'v{i}', f'Song {i}', 'Artist', '', 200) for i in range(7)]
    for tid in tids:
        observe(store, tid, [{'name': 'Metal', 'count': 10}])
    drain(store, batch=2)
    before = store.prepared_content.counters()
    observe(store, tids[0], [{'name': 'Ambient', 'count': 10}], run='changed')
    assert musical_preparation.source_step(store, 1001, batch=2)
    assert store.conn.execute('SELECT cursor FROM rec_musical_pending').fetchone()[0] > 0
    assert len(store.prepared_content.pending(32)) == 2
    drain(store, batch=2)
    assert store.prepared_content.counters() == before
    values = store.musical_profiles.by_keys([f'song {i}|artist' for i in range(7)])
    assert len(values) == 7
    assert all(value['dimensions']['intensity']['value'] < .5 for value in values.values())


def test_source_slice_captures_only_its_four_revision_keys(store, monkeypatch):
    tid = store.tracks.upsert_track('v', 'Song', 'Artist', '', 200)
    observe(store, tid, [{'name': 'Metal'}])
    key = store.musical_profiles.pending()
    original, captures = store._snapshot_revisions.capture, []
    def capture(dependencies=None):
        captures.append(dependencies)
        return original(dependencies)
    monkeypatch.setattr(store._snapshot_revisions, 'capture', capture)
    assert musical_preparation.source_step(store, 1000)
    expected = {(table, 'key:' + key) for table in (
        'rec_musical_pending', 'rec_musical_sources', 'rec_musical_links', 'rec_musical_source_profiles')}
    assert captures == [expected, expected]


def test_identical_artist_tags_from_new_recording_retain_history_without_restarting_fanout(store):
    one = store.tracks.upsert_track('one', 'One', 'Artist', '', 200)
    two = store.tracks.upsert_track('two', 'Two', 'Artist', '', 200)
    observe(store, one, [{'name': 'Metal'}], title='One')
    drain(store)
    observe(store, two, [{'name': 'Metal'}], run='another', title='Two')
    assert store.conn.execute('SELECT COUNT(*) FROM metadata_findings WHERE provenance_json IS NOT NULL').fetchone()[0] == 2
    assert not store.musical_profiles.pending()
    drain(store)
    assert get(store, 'one|artist') == get(store, 'two|artist')


def test_changed_artist_identity_retracts_old_link_without_merging_homonyms(store):
    one = store.tracks.upsert_track('one', 'One', 'Same name', '', 200)
    two = store.tracks.upsert_track('two', 'Two', 'Same name', '', 200)
    observe(store, one, [{'name': 'Metal'}], entity='artist-one')
    observe(store, two, [{'name': 'Ambient'}], entity='artist-two')
    drain(store)
    assert get(store, 'one|same name')['intensity']['value'] > .5
    assert get(store, 'two|same name')['intensity']['value'] < .5
    observe(store, one, [{'name': 'Ambient'}], entity='artist-two', run='corrected')
    drain(store)
    assert get(store, 'one|same name') == get(store, 'two|same name')
    rid = store.tracks.catalog_subject(one)['recording_id']
    assert store.conn.execute('SELECT COUNT(*) FROM rec_musical_links WHERE recording_id=?', (rid,)).fetchone()[0] == 1


def test_large_source_yields_to_recordings_between_pages(store):
    for i in range(5):
        tid = store.tracks.upsert_track(f'v{i}', f'Song {i}', 'Artist', '', 200)
        observe(store, tid, [{'name': 'Metal'}])
    p = Preparation(store, budget=replace(Budget(), batch=1))
    assert p.capture(1000)  # source page
    assert store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0] == 0
    assert store.musical_profiles.pending()
    assert p.capture(1001)  # recording gets its turn before the source drains
    # Identity reconciliation can put a retired-key tombstone first in the
    # recording queue. It must receive its turn just like a live recording.
    assert store.prepared_content.progress()['prepared'] == 1
    assert store.musical_profiles.pending()


def test_source_snapshot_rejects_changed_evidence_and_preserves_last_good(store, monkeypatch):
    tid = store.tracks.upsert_track('v', 'Song', 'Artist', '', 200)
    observe(store, tid, [{'name': 'Metal'}])
    drain(store)
    before = get(store)
    observe(store, tid, [{'name': 'Ambient'}], run='new')
    original = profiles.source_profile
    def change(source):
        result = original(source)
        observe(store, tid, [{'name': 'Techno'}], run='raced')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(profiles, 'source_profile', change)
        assert not musical_preparation.source_step(store, 1001)
    assert get(store) == before
    assert store.musical_profiles.pending()
    drain(store)
    assert get(store)['intensity']['value'] > .5


def test_recording_snapshot_rejects_override_race_and_explicit_null_blocks_fallback(store, monkeypatch):
    tid = store.tracks.upsert_track('v', 'Song', 'Artist', '', 200)
    observe(store, tid, .9, field='mood_aggressive', scope='recording')
    drain(store)
    before = get(store)
    with store.conn:
        catalog.choose_field(store.conn, tid, 'energy', .2)
    original = MusicalProfilesRepo.recording_inputs
    def change(repo, keys):
        result = original(repo, keys)
        with store.conn:
            catalog.choose_field(store.conn, tid, 'energy', None)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(MusicalProfilesRepo, 'recording_inputs', change)
        assert not Preparation(store).capture(1001)
    assert get(store) == before
    drain(store)
    assert get(store)['intensity']['value'] is None
    assert get(store)['intensity']['override']


def test_restart_budget_exhaustion_empty_tag_retraction_and_version_rebuild(tmp_path, monkeypatch):
    path = tmp_path / 'profiles.db'
    store = Store(path)
    store.init_schema()
    tid = store.tracks.upsert_track('v', 'Song', 'Artist', '', 200)
    observe(store, tid, [{'name': 'Metal'}])
    drain(store)
    previous = get(store)
    observe(store, tid, [], run='empty')
    with monkeypatch.context() as patch:
        patch.setattr(profiles, 'source_profile', lambda _: (_ for _ in ()).throw(WorkBudgetExceeded()))
        assert not musical_preparation.source_step(store, 1001)
    assert get(store) == previous
    store.conn.close()
    store = Store(path)
    store.init_schema()
    try:
        drain(store)
        assert get(store)['intensity']['value'] is None
        with store.conn:
            store.conn.execute("UPDATE rec_musical_profiles SET version='old'")
            store.conn.execute("UPDATE rec_musical_source_profiles SET version='old'")
        assert musical_preparation.due(store)
        drain(store, batch=1)
        assert not any(store.musical_profiles.stale(profiles.VERSION))
        assert get(store)['intensity']['value'] is None
    finally:
        store.conn.close()


def test_profile_publication_rolls_back_with_content_capture(store, monkeypatch):
    tid = store.tracks.upsert_track('v', 'Song', 'Artist', '', 200)
    observe(store, tid, .9, field='mood_aggressive', scope='recording')
    assert musical_preparation.source_step(store, 1000)
    original = MusicalProfilesRepo.publish_recordings
    def fail(repo, rows, now):
        original(repo, rows, now)
        raise RuntimeError('interrupted publication')
    with monkeypatch.context() as patch:
        patch.setattr(MusicalProfilesRepo, 'publish_recordings', fail)
        with pytest.raises(RuntimeError, match='interrupted'):
            Preparation(store).capture(1000)
    assert store.prepared_content.has_pending()
    assert not store.musical_profiles.by_keys(['song|artist'])
    assert store.prepared_content.counters()[0] == 0
    drain(store)
    assert get(store)['intensity']['value'] > .5


def test_byte_budgets_reject_oversize_inputs_without_losing_pending_or_last_good(store):
    tid = store.tracks.upsert_track('v', 'Song', 'Artist', '', 200)
    observe(store, tid, [{'name': 'Metal'}])
    key = store.musical_profiles.pending()
    with store.read_snapshot() as snapshot:
        with pytest.raises(WorkBudgetExceeded, match='byte budget'):
            snapshot.store.musical_profiles.source_input(key, max_bytes=1)
    assert store.musical_profiles.pending() == key
    drain(store)
    previous = get(store)
    with store.read_snapshot() as snapshot:
        with pytest.raises(WorkBudgetExceeded, match='byte budget'):
            snapshot.store.musical_profiles.recording_inputs(['song|artist'], max_bytes=1)
    assert get(store) == previous


def test_profile_preparation_budget_on_synthetic_catalog(store, record_property):
    # Engineering throughput measurement; no claim about listening quality.
    for i in range(96):
        tid = store.tracks.upsert_track(f'v{i}', f'Song {i}', 'Artist', '', 200)
        observe(store, tid, [{'name': 'Metal'}, {'name': 'Ambient'}])
    start = time.monotonic()
    drain(store)
    elapsed = time.monotonic() - start
    record_property('musical_profile_96_recordings_seconds', elapsed)
    assert elapsed < 15
    assert store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0] == 96
    # Stale-version probes must be indexed seeks, not full corpus scans.
    plans = store.conn.execute('EXPLAIN QUERY PLAN SELECT identity_key FROM rec_musical_profiles '
                               'WHERE version<? LIMIT 32', (profiles.VERSION,)).fetchall()
    assert any('SEARCH' in row[3] and 'version' in row[3] for row in plans)
