"""Selections explain their retained evidence, including unknown legacy lineage."""
import json

import pytest

from tests.core.test_metadata_owner_cutover import predecessor, seed, convert
from tests.test_audio_vibes import sample
from yt_playlist.core import catalog, metadata_deliveries, metadata_evidence
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.providers.base import MetadataObservation


def test_legacy_explanation_keeps_unknown_attribution_and_original_candidate_deliveries(predecessor):
    store = predecessor
    entry, _, _ = seed(store)
    convert(store)
    report = store.enrichment_reports.metadata_provenance(entry)
    assert report['fields']['bpm']['value'] is None and report['fields']['bpm']['override']
    assert report['fields']['genre']['value'] == '' and report['fields']['genre']['override']
    selected = report['lineage']['genre']
    assert selected['selection']['attribution_status'] == 'legacy_unresolved'
    assert selected['evidence'] is None
    choice = selected['choice']
    assert choice['observation']['field_scope'] == 'legacy_item'
    assert choice['observation']['origin'] == 'legacy_choice'
    assert metadata_deliveries.response(store.conn, choice['delivery']['id'])['owner'] == 'entry_field_choices'
    assert {row['status'] for row in report['conflicts']} == {
        'unresolved', 'user_resolved', 'provider_converged'}
    for conflict in report['conflicts']:
        for candidate in conflict['candidates']:
            assert candidate['observation']['legacy_owner'] == 'enrichment_conflict'
            assert metadata_deliveries.response(store.conn, candidate['delivery']['id'])['owner'] == 'enrichment_conflict'


def test_new_selection_and_withdrawal_explain_the_exact_delivery_and_retained_decision(predecessor):
    store = predecessor
    entry, _, _ = seed(store)
    convert(store)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        first = metadata_deliveries.capture_state(store.conn, entry, 'provider', 'first', {'genre': 'Rock'}, now=30)
        second = metadata_deliveries.capture_state(store.conn, entry, 'provider', 'second', {'genre': 'Rock'}, now=31)
        observations = [metadata_evidence.record_provider(store.conn, delivery, {'genre': 'Rock'})['genre']
                        for delivery in (first, second)]
        metadata_evidence.select(store.conn, entry, 'genre', observations[1], now=32)
        withdrawal = catalog.withdraw_choice(store.conn, entry, 'genre', now=33)
    report = store.enrichment_reports.metadata_provenance(entry)
    assert report['fields']['genre'] == {'value': 'Rock', 'override': False, 'source': 'provider'}
    lineage = report['lineage']['genre']
    assert lineage['selection']['attribution_status'] == 'evidenced'
    assert lineage['evidence']['observation']['id'] == observations[1]
    assert lineage['evidence']['delivery']['id'] == second
    assert lineage['choice']['observation']['id'] == withdrawal
    assert lineage['choice']['observation']['value_kind'] == 'withdrawn'
    assert lineage['choice']['delivery'] is None
    decision = next(row for row in report['decisions'] if row['id'] == withdrawal)
    assert decision['withdrawn'] and decision['chosen_at'] == 33
    assert decision['evidence'] == lineage['choice']
    assert len([row for row in report['decisions'] if row['field'] == 'genre']) >= 3
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('consumer', ['musical', 'content', 'vibes'])
def test_preparation_guards_typed_choices_for_only_its_captured_entries(predecessor, consumer):
    store = predecessor
    # The staged predecessor has not run Store.init_schema's WAL setup. Match
    # normal runtime before exercising a writer alongside a pinned reader.
    store.conn.execute('PRAGMA journal_mode=WAL')
    entry, _, key = seed(store)
    other = store.tracks.upsert_track('other-video', 'Other Song', 'Artist', 'Jazz', 200)
    convert(store)
    if consumer == 'vibes':
        for point in ('thirty', 'middle', 'two_thirds'):
            value = sample(run_id='typed-metadata', point=point)
            value['video_id'] = 'video'
            store.audio_vibes.save_sample(value, 30)
    store._snapshot_revisions = Revisions(store.conn)

    def capture(reader):
        if consumer == 'musical':
            return reader.musical_profiles.recording_inputs([key])[key][1]
        if consumer == 'vibes':
            inputs = reader.vibe_predictions.inputs([key])[key]
            assert len(inputs['samples']) == 3
            return inputs['metadata']
        return json.loads(reader.prepared_content.capture([(key, 0)])[0][2])

    with store.read_snapshot() as snapshot:
        before = capture(snapshot.store)
        if consumer in ('musical', 'vibes'):
            assert before['bpm'] is None and before['genre'] == ''
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            catalog.choose_field(store.conn, other, 'genre', 'Blues', now=40)
        assert snapshot.publish(lambda: None)
    with store.read_snapshot() as snapshot:
        assert capture(snapshot.store) == before
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            catalog.withdraw_choice(store.conn, entry, 'genre', now=41)
        assert not snapshot.publish(lambda: None)
    with store.read_view() as reader:
        after = capture(reader)
    if consumer == 'musical':
        assert 'genre' not in after and after['bpm'] is None
    elif consumer == 'vibes':
        assert after['genre'] == 'Jazz' and after['bpm'] is None
    else:
        assert after['library']['genre'] == 'Jazz'


def test_vibe_inputs_follow_native_source_heads_after_payload_retirement(predecessor):
    store = predecessor
    store.conn.execute('PRAGMA journal_mode=WAL')
    entry, recording, key = seed(store)
    other = store.tracks.upsert_track('other-video', 'Other Song', 'Artist', 'Jazz', 200)
    convert(store)
    for point in ('thirty', 'middle', 'two_thirds'):
        value = sample(run_id='native-source', point=point)
        value['video_id'] = 'video'
        store.audio_vibes.save_sample(value, 30)

    def record(subject, entity, value, run):
        observation = MetadataObservation('mood_aggressive', value, 'recording', str(entity),
                                          'reported', unit='classifier_probability')
        store.enrichment_state.record_metadata_observations(subject, 'native', [observation],
                                                            run_id=run, now=40)

    record(entry, recording, .75, 'first')
    store._snapshot_revisions = Revisions(store.conn)
    with store.read_snapshot() as snapshot:
        inputs = snapshot.store.vibe_predictions.inputs([key])[key]
        assert len(inputs['samples']) == 3 and inputs['metadata']['mood_aggressive'] == .75
        record(other, 'other-recording', .2, 'unrelated')
        assert snapshot.publish(lambda: None)
    with store.read_snapshot() as snapshot:
        assert snapshot.store.vibe_predictions.inputs([key])[key]['metadata']['mood_aggressive'] == .75
        record(entry, recording, .5, 'replacement')
        assert not snapshot.publish(lambda: None)
    with store.read_view() as reader:
        assert reader.vibe_predictions.inputs([key])[key]['metadata']['mood_aggressive'] == .5
