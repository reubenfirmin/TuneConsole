"""Conflict policy continues with evidence IDs rather than copied candidates."""
import json

import pytest

from tests.core.test_metadata_evidence_migration import additive_catalog, catalog, capture, mutate
from yt_playlist.core import metadata_conflicts as conflicts, metadata_evidence as evidence, metadata_legacy as legacy


def _status(store, conflict):
    return store.conn.execute('SELECT status FROM metadata_conflicts WHERE id=?', (conflict,)).fetchone()[0]


def test_resolution_convergence_and_reopening_keep_the_actual_choice_and_candidate_evidence(catalog):
    store = catalog
    entry, _, _, _ = capture(store, fields={})
    offered = [{'provider': 'a', 'value': 120}, {'provider': 'b', 'value': 130}]
    conflict = mutate(store, lambda: conflicts.upsert(store.conn, entry, 'bpm', offered, now=21))
    offered = conflicts.candidates(store.conn, conflict)
    count = store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0]
    choice = mutate(store, lambda: evidence.choose(store.conn, entry, 'bpm', 125, now=22))
    mutate(store, lambda: conflicts.resolve(store.conn, entry, 'bpm', choice, now=22))
    mutate(store, lambda: conflicts.upsert(store.conn, entry, 'bpm', offered, now=23))
    assert _status(store, conflict) == 'user_resolved'
    assert store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] == count + 1
    mutate(store, lambda: conflicts.upsert(store.conn, entry, 'bpm',
        [offered[0], {'provider': 'b', 'value': 140}], now=24))
    assert _status(store, conflict) == 'unresolved'
    mutate(store, lambda: conflicts.converge(store.conn, entry, {'bpm': {'a': 120}}, now=25))
    assert _status(store, conflict) == 'unresolved'
    mutate(store, lambda: conflicts.converge(store.conn, entry, {'bpm': {'a': 120, 'b': 120}}, now=26))
    assert _status(store, conflict) == 'provider_converged'
    mutate(store, lambda: conflicts.upsert(store.conn, entry, 'bpm', offered, now=27))
    assert _status(store, conflict) == 'unresolved'
    assert store.conn.execute('SELECT selected_observation_id FROM metadata_conflicts WHERE id=?',
                              (conflict,)).fetchone()[0] == choice
    assert evidence.effective(store.conn, entry, 'bpm') == 125


def test_failed_provider_keeps_its_candidate_until_it_successfully_refreshes(catalog):
    store = catalog
    entry, _, _, _ = capture(store, fields={})
    conflict = mutate(store, lambda: conflicts.upsert(store.conn, entry, 'genre',
        [{'provider': 'a', 'value': 'Jazz'}, {'provider': 'b', 'value': 'Rock'}], now=20))
    mutate(store, lambda: conflicts.upsert(store.conn, entry, 'genre',
        [{'provider': 'a', 'value': 'Blues'}], refreshed_providers={'a'}, now=21))
    assert [(r['provider'], r['value']) for r in conflicts.candidates(store.conn, conflict)] == [('a', 'Blues'), ('b', 'Rock')]
    mutate(store, lambda: conflicts.upsert(store.conn, entry, 'genre',
        [{'provider': 'a', 'value': 'Blues'}], refreshed_providers={'a', 'b'}, now=22))
    assert [(r['provider'], r['value']) for r in conflicts.candidates(store.conn, conflict)] == [('a', 'Blues')]


def test_recording_conflicts_are_shared_while_year_conflicts_stay_on_the_captured_entry(catalog):
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    with store.conn:
        related = store.conn.execute("INSERT INTO catalog_entries(recording_id,source_ref) VALUES (?,'related')",
                                     (recording,)).lastrowid
        store.conn.execute("INSERT INTO unresolved_items(entry_id,title,artist) VALUES (?,'Song','Artist')", (related,))
    for field in ('bpm', 'year'):
        values = (120, 130) if field == 'bpm' else ('1999', '2000')
        mutate(store, lambda: conflicts.upsert(store.conn, entry, field,
            [{'provider': 'a', 'value': values[0]}, {'provider': 'b', 'value': values[1]}], now=20))
    rows = conflicts.for_entry(store.conn, related)
    assert [(r['field'], r['recording_id'], r['entry_id']) for r in rows] == [('bpm', recording, None)]
    mutate(store, lambda: conflicts.converge(store.conn, related,
        {'bpm': {'a': 120, 'b': 120}, 'year': {'a': '1999', 'b': '1999'}}, now=21))
    assert [(r['field'], r['status']) for r in conflicts.for_entry(store.conn, entry)] == [
        ('bpm', 'provider_converged'), ('mb_year', 'unresolved')]


def test_supplied_evidence_cannot_be_borrowed_from_another_input_or_provider(catalog):
    store = catalog
    entry, _, _, observations = capture(store)
    other, _, _, _ = capture(store, video='other')
    before = list(store.conn.iterdump())
    for target, provider in ((other, 'provider'), (entry, 'different-provider')):
        with pytest.raises(ValueError, match='different evidence'):
            mutate(store, lambda: conflicts.upsert(store.conn, target, 'bpm',
                [{'provider': provider, 'value': 120, 'observation_id': observations['bpm']}]))
    assert list(store.conn.iterdump()) == before


def test_legacy_entry_conflict_continues_with_new_scoped_evidence_without_reassigning_history(additive_catalog):
    store = additive_catalog
    entry, recording, _, _ = capture(store, fields={})
    original = json.dumps([{'provider': 'a', 'value': 120}, {'provider': 'b', 'value': 130}])
    with store.conn:
        old = store.conn.execute("INSERT INTO enrichment_conflict(track_id,field,candidates) VALUES (?,'bpm',?)",
                                 (entry, original)).lastrowid
    mutate(store, lambda: legacy.backfill_conflicts(store.conn))
    historical = list(map(tuple, store.conn.execute("SELECT * FROM metadata_observations WHERE origin='legacy_conflict'")))
    conflict = mutate(store, lambda: conflicts.upsert(store.conn, entry, 'bpm',
        [{'provider': 'a', 'value': 120}, {'provider': 'b', 'value': 140}], now=40))
    row = store.conn.execute('SELECT * FROM metadata_conflicts WHERE id=?', (conflict,)).fetchone()
    assert row['legacy_record_id'] == old and row['field_scope'] == 'legacy_item'
    assert row['recording_id'] is None and row['entry_id'] == entry
    assert list(map(tuple, store.conn.execute("SELECT * FROM metadata_observations WHERE origin='legacy_conflict'"))) == historical
    assert [(r['field_scope'], r['value']) for r in conflicts.candidates(store.conn, conflict)] == [
        ('recording', 120), ('recording', 140)]
    assert {r[0] for r in store.conn.execute("SELECT recording_id FROM metadata_observations WHERE origin='provider'")} == {recording}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
