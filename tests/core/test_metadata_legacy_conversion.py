"""Historical metadata keeps its recorded identity, scope and decisions."""
import json

import pytest

from tests.core.test_metadata_evidence_migration import additive_catalog, capture, mutate
from yt_playlist.core import metadata_deliveries as deliveries, metadata_evidence as evidence
from yt_playlist.core import metadata_legacy as legacy, metadata_native as native


@pytest.fixture
def catalog(additive_catalog):
    return additive_catalog


def test_equal_legacy_values_keep_independent_captures_and_unknown_attribution(catalog):
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    with store.conn:
        store.conn.executemany('INSERT INTO metadata_findings(entry_id,recording_id,field,provider,value_json,observed_at) '
            "VALUES (?,?,'bpm','old','120',?)", [(entry, recording, 10), (entry, recording, 11)])
        store.conn.execute('INSERT INTO enrichment_log(track_id,run_id,provider,field,value,created_at) '
                           "VALUES (?,'old-run','old','bpm','120',12)", (entry,))
        store.conn.execute('INSERT INTO recording_field_sources VALUES (?,\'bpm\',\'old\',11)', (recording,))
    mutate(store, lambda: legacy.backfill_observations(store.conn))
    mutate(store, lambda: legacy.backfill_selections(store.conn))
    rows = store.conn.execute("SELECT o.*,d.raw_filename FROM metadata_observations o "
        "JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.origin LIKE 'legacy_%' ORDER BY o.id").fetchall()
    assert len(rows) == 3 and len({row['delivery_id'] for row in rows}) == 3
    assert [(r['origin'], r['observed_at'], r['recording_id'], r['field_scope'], r['number_value']) for r in rows] == [
        ('legacy_finding', 10, recording, 'recording', 120),
        ('legacy_finding', 11, recording, 'recording', 120),
        ('legacy_log', 12, None, 'legacy_item', 120)]
    selected = store.conn.execute('SELECT * FROM recording_metadata_selections').fetchone()
    assert (selected['observation_id'], selected['attribution_status'], selected['legacy_source']) == (
        None, 'legacy_unresolved', 'old')
    assert deliveries.response(store.conn, rows[-1]['delivery_id'])['row']['run_id'] == 'old-run'
    before = list(store.conn.iterdump())
    mutate(store, lambda: legacy.backfill_observations(store.conn))
    mutate(store, lambda: legacy.backfill_selections(store.conn))
    assert list(store.conn.iterdump()) == before
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('chosen', [None, ''])
def test_active_legacy_choices_retain_null_and_blank_without_inventing_old_recording(catalog, chosen):
    store = catalog
    entry, _, _, _ = capture(store)
    with store.conn:
        store.conn.execute('INSERT INTO entry_field_choices VALUES (?,\'bpm\',?,\'user\')',
                           (entry, json.dumps(chosen)))
        store.conn.execute('INSERT INTO metadata_decisions(entry_id,field,value_json,source,chosen_at) '
                           "VALUES (?,'bpm','90','user',2)", (entry,))
    mutate(store, lambda: legacy.backfill_observations(store.conn))
    assert evidence.effective(store.conn, entry, 'bpm') == chosen
    selected = store.conn.execute('SELECT o.* FROM metadata_choice_heads h '
                                 'JOIN metadata_observations o ON o.id=h.observation_id').fetchone()
    assert selected['recording_id'] is None and selected['field_scope'] == 'legacy_item'
    assert selected['observed_at'] is None and selected['origin'] == 'legacy_choice'
    assert selected['value_kind'] == ('null' if chosen is None else 'blank')
    assert store.conn.execute("SELECT count(*) FROM metadata_observations WHERE origin='legacy_choice'").fetchone()[0] == 2


def test_native_legacy_source_points_to_its_original_delivery_without_rewriting_revision(catalog):
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    provenance = {'schema': 1, 'scope': 'artist', 'entity_id': 'native-artist', 'kind': 'reported',
                  'unit': 'provider_tag_count_or_order', 'delivery_id': 'original-run',
                  'match_evidence': {'input_title': 'original input', 'query_kind': 'artist'}}
    tags = [{'name': 'Jazz', 'count': 4}]
    with store.conn:
        finding = store.conn.execute('INSERT INTO metadata_findings(entry_id,recording_id,field,provider,value_json,'
            "observed_at,provenance_json) VALUES (?,?,'tags','native',?,10,?)",
            (entry, recording, json.dumps(tags), json.dumps(provenance))).lastrowid
    before = list(map(tuple, store.conn.execute('SELECT * FROM rec_musical_sources')))
    mutate(store, lambda: legacy.backfill_observations(store.conn))
    key = json.dumps(['native', 'artist', 'native-artist'], separators=(',', ':'))
    observation = store.conn.execute('SELECT id FROM native_metadata_observations WHERE legacy_finding_id=?',
                                     (finding,)).fetchone()[0]
    assert native.read(store.conn, observation) == {**provenance, 'field': 'tags', 'value': tags}
    supplied = native.source_values(store.conn, key)['tags']
    assert supplied['value'] == tags and supplied['provenance']['match_evidence'] == {'query_kind': 'artist'}
    assert 'delivery_id' not in supplied['provenance']
    assert list(map(tuple, store.conn.execute('SELECT * FROM rec_musical_sources'))) == before
    assert not store.conn.execute("SELECT 1 FROM metadata_observations WHERE legacy_owner='metadata_findings'").fetchone()


@pytest.mark.parametrize(('status', 'selected', 'reason'), [(0, None, None), (1, '125', None),
    (1, None, 'unrecorded_legacy_resolution'), (1, 'unknown', 'unsupported_legacy_value'), (2, None, None)])
def test_conflict_conversion_preserves_status_explicit_resolution_and_unknown_scope(catalog, status, selected, reason):
    store = catalog
    entry, _, _, _ = capture(store, fields={})
    offered = [{'provider': 'old-a', 'value': 120}, {'provider': 'old-b', 'value': 130}]
    with store.conn:
        old = store.conn.execute('INSERT INTO enrichment_conflict(track_id,field,candidates,resolved,resolved_value,updated_at) '
            "VALUES (?,'bpm',?,?,?,17)", (entry, json.dumps(offered), status, selected)).lastrowid
    mutate(store, lambda: legacy.backfill_conflicts(store.conn))
    row = store.conn.execute('SELECT * FROM metadata_conflicts WHERE legacy_record_id=?', (old,)).fetchone()
    assert row['status'] == {0: 'unresolved', 1: 'user_resolved', 2: 'provider_converged'}[status]
    assert row['field_scope'] == 'legacy_item' and row['entry_id'] == entry and row['recording_id'] is None
    assert row['legacy_resolution_reason'] == reason
    candidates = store.conn.execute('SELECT o.* FROM metadata_conflict_candidates c '
        'JOIN metadata_observations o ON o.id=c.observation_id WHERE c.conflict_id=? ORDER BY o.legacy_position',
        (row['id'],)).fetchall()
    assert [(r['legacy_source'], r['number_value'], r['recording_id']) for r in candidates] == [
        ('old-a', 120, None), ('old-b', 130, None)]
    if selected == '125':
        decision = store.conn.execute('SELECT * FROM metadata_observations WHERE id=?',
                                      (row['selected_observation_id'],)).fetchone()
        assert decision['origin'] == 'legacy_choice' and decision['number_value'] == 125
    else:
        assert row['selected_observation_id'] is None
    original = store.conn.execute("SELECT delivery_id FROM metadata_legacy_records WHERE legacy_owner='enrichment_conflict' "
                                  'AND legacy_id=?', (old,)).fetchone()[0]
    assert deliveries.response(store.conn, original)['row']['candidates'] == json.dumps(offered)
    before = list(store.conn.iterdump())
    mutate(store, lambda: legacy.backfill_conflicts(store.conn))
    assert list(store.conn.iterdump()) == before
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize(('field', 'raw'), [('bpm', '"invalid"'), ('arbitrary', '42')])
def test_unrepresentable_active_choice_aborts_without_dropping_the_installed_value(catalog, field, raw):
    store = catalog
    entry, _, _, _ = capture(store)
    with store.conn:
        store.conn.execute('INSERT INTO entry_field_choices VALUES (?,?,?,\'user\')', (entry, field, raw))
    before = list(store.conn.iterdump())
    with pytest.raises(ValueError, match='Unsupported active legacy metadata choice'):
        mutate(store, lambda: legacy.backfill_observations(store.conn))
    assert list(store.conn.iterdump()) == before


def test_interruption_after_raw_publication_rolls_back_all_relational_admission(catalog, monkeypatch):
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    with store.conn:
        store.conn.execute('INSERT INTO metadata_findings(entry_id,recording_id,field,provider,value_json,observed_at) '
                           "VALUES (?,?,'bpm','old','120',10)", (entry, recording))
    before = list(store.conn.iterdump())
    original = legacy.capture_batch
    def interrupt(*args):
        original(*args)
        raise RuntimeError('interrupted legacy conversion')
    monkeypatch.setattr(legacy, 'capture_batch', interrupt)
    with pytest.raises(RuntimeError, match='interrupted legacy'):
        mutate(store, lambda: legacy.backfill_observations(store.conn))
    assert list(store.conn.iterdump()) == before
