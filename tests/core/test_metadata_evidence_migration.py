"""Typed catalog evidence and retained choices preserve exact lineage."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core import metadata_deliveries as deliveries, metadata_evidence as evidence
from yt_playlist.core import catalog as catalog_owner, metadata
from yt_playlist.core.migrations import (v074_artist_credits as credits, v075_metadata_deliveries as delivery_schema,
                                        v080_metadata_evidence as migration)
from yt_playlist.core.migrations.runner import Context
from yt_playlist.core.store import Store


@pytest.fixture
def additive_catalog(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'evidence.db')
    with closing(store.conn):
        _pristine_installed_schema(69).backup(store.conn)
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            for module in (credits, delivery_schema, migration):
                module.apply(store.conn, Context(4))
        migration.validate(store.conn)
        store._bind_repositories()
        yield store


@pytest.fixture
def catalog(additive_catalog):
    with additive_catalog.conn:
        additive_catalog.conn.execute('BEGIN IMMEDIATE')
        migration.convert_legacy(additive_catalog.conn)
    return additive_catalog


def capture(store, video='video', key='capture:1', *, fields=None):
    fields = {'bpm': 120} if fields is None else fields
    entry = store.tracks.upsert_track(video, 'Song', 'Artist', None, 200)
    recording, = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()
    delivery = deliveries.Delivery('provider', key, 'item', video, 20, entry, recording, video, response=fields)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        owner, = deliveries.record_many(store.conn, [delivery])
        result = evidence.record_provider(store.conn, owner, fields, units={'bpm': 'bpm'} if 'bpm' in fields else {})
    return entry, recording, owner, result


def mutate(store, call):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        return call()


def test_delivery_replay_is_idempotent_and_selection_identifies_the_actual_evidence(catalog):
    store = catalog
    entry, recording, owner, first = capture(store)
    assert mutate(store, lambda: evidence.record_provider(store.conn, owner, {'bpm': 120}, units={'bpm': 'bpm'})) == first
    assert store.conn.execute('SELECT count(*) FROM metadata_observations WHERE delivery_id=?', (owner,)).fetchone()[0] == 1
    mutate(store, lambda: evidence.select(store.conn, entry, 'bpm', first['bpm'], now=21))
    selected = store.conn.execute('SELECT * FROM recording_metadata_selections WHERE recording_id=? AND field=?',
                                  (recording, 'bpm')).fetchone()
    assert selected['observation_id'] == first['bpm'] and selected['attribution_status'] == 'evidenced'
    assert selected['legacy_source'] is None and selected['selected_at'] == 21
    assert evidence.effective(store.conn, entry, 'bpm') == 120
    with pytest.raises(sqlite3.IntegrityError, match='cannot change'):
        mutate(store, lambda: evidence.record_provider(store.conn, owner, {'bpm': 121}, units={'bpm': 'bpm'}))


def test_catalog_ingestion_reuses_unchanged_capture_and_actual_field_credit_lineage(catalog):
    store = catalog
    def ingest(name='Album'):
        return store.tracks.upsert_track('video', 'Song', 'Artist', name, 200,
            credits=[{'name': 'Artist', 'id': 'artist'}], thumbnail='https://example.test/cover.jpg')
    entry = ingest()
    initial = list(map(tuple, store.conn.execute('SELECT * FROM entry_metadata_selections ORDER BY entry_id,field')))
    before = store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0]
    first = store.conn.execute("SELECT delivery_id FROM metadata_capture_heads WHERE entry_id=? AND capture_slot='catalog-input'", (entry,)).fetchone()[0]
    for _ in range(20):
        assert ingest() == entry
    assert list(map(tuple, store.conn.execute('SELECT * FROM entry_metadata_selections ORDER BY entry_id,field'))) == initial
    assert store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] == before
    assert store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == 1
    supplied = store.conn.execute('SELECT DISTINCT o.delivery_id FROM entry_metadata_selections s '
                                 'JOIN metadata_observations o ON o.id=s.observation_id WHERE s.entry_id=?', (entry,)).fetchall()
    assert list(map(tuple, supplied)) == [(first,)]
    credit = store.conn.execute('SELECT o.metadata_delivery_id,o.processing_count FROM entry_artist_selections s '
                                'JOIN artist_credit_observations o ON o.id=s.observation_id WHERE s.entry_id=?', (entry,)).fetchone()
    assert tuple(credit) == (first, 21)
    ingest('Changed album')
    ingest()
    assert store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == 3
    assert store.conn.execute("SELECT count(DISTINCT raw_digest) FROM metadata_deliveries").fetchone()[0] == 2


def test_ordinary_provider_precedence_retains_the_losing_evidence_without_repeated_findings(catalog):
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    order = ('preferred', 'fallback')
    for _ in range(3):
        mutate(store, lambda: metadata.apply_provider(store.conn, entry, 'preferred', {'bpm': 125}, order, now=30))
        mutate(store, lambda: metadata.apply_provider(store.conn, entry, 'fallback', {'bpm': 135}, order, now=31))
    assert evidence.effective(store.conn, entry, 'bpm') == 125
    assert metadata.selected_source(store.conn, entry, 'bpm') == 'preferred'
    assert list(map(tuple, store.conn.execute("SELECT d.provider,o.number_value FROM metadata_observations o "
        "JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.field='bpm' ORDER BY o.id"))) == [
            ('preferred', 125), ('fallback', 135)]
    selected = store.conn.execute("SELECT s.observation_id FROM recording_metadata_selections s "
        "JOIN metadata_observations o ON o.id=s.observation_id JOIN metadata_deliveries d ON d.id=o.delivery_id "
        "WHERE s.recording_id=? AND s.field='bpm' AND d.provider='preferred'", (recording,)).fetchone()
    assert selected is not None
    assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='metadata_findings'").fetchone()


def test_ordinary_catalog_choices_have_retained_lineage_above_continuing_provider_selection(catalog):
    store = catalog
    entry, _, _, _ = capture(store, fields={})
    mutate(store, lambda: catalog_owner.choose_field(store.conn, entry, 'bpm', None, now=30))
    mutate(store, lambda: metadata.apply_provider(store.conn, entry, 'provider', {'bpm': 135}, ('provider',), now=31))
    assert evidence.effective(store.conn, entry, 'bpm') is None
    assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] is None
    mutate(store, lambda: catalog_owner.choose_field(store.conn, entry, 'bpm', '', now=32))
    assert evidence.effective(store.conn, entry, 'bpm') == ''
    assert list(map(tuple, store.conn.execute("SELECT value_kind,observed_at FROM metadata_observations "
        "WHERE origin='user' ORDER BY id"))) == [('null', 30), ('blank', 32)]
    assert store.conn.execute('SELECT count(*) FROM metadata_decisions').fetchone()[0] == 2


def test_equivalent_recording_selection_transfer_preserves_the_original_subject_and_lineage(catalog):
    store = catalog
    entry, previous, _, first = capture(store)
    mutate(store, lambda: evidence.select(store.conn, entry, 'bpm', first['bpm'], now=21))
    target_entry = store.tracks.upsert_track('target', 'Equivalent', 'Artist', None, 200)
    target = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (target_entry,)).fetchone()[0]
    original = tuple(store.conn.execute('SELECT * FROM metadata_observations WHERE id=?', (first['bpm'],)).fetchone())
    mutate(store, lambda: metadata.reuse_recording_selections(store.conn, previous, target))
    transferred = store.conn.execute("SELECT o.* FROM recording_metadata_selections s "
        "JOIN metadata_observations o ON o.id=s.observation_id WHERE s.recording_id=? AND s.field='bpm'",
        (target,)).fetchone()
    assert transferred['origin'] == 'selection_transfer'
    assert transferred['derived_from_observation_id'] == first['bpm']
    assert transferred['recording_id'] == target and transferred['entry_id'] == target_entry
    assert evidence.effective(store.conn, target_entry, 'bpm') == 120
    assert metadata.selected_source(store.conn, target_entry, 'bpm') == 'provider'
    assert tuple(store.conn.execute('SELECT * FROM metadata_observations WHERE id=?', (first['bpm'],)).fetchone()) == original
    count = store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0]
    mutate(store, lambda: metadata.reuse_recording_selections(store.conn, previous, target))
    assert store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] == count
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize(('column', 'changed'), [('number_value', 121), ('legacy_source', 'other')])
def test_database_rejects_transferred_values_or_providers_that_differ_from_the_original(catalog, column, changed):
    store = catalog
    entry, recording, _, observations = capture(store)
    video = store.conn.execute('SELECT video_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
    row = {'entry_id': entry, 'recording_id': recording, 'input_video_id': video,
           'field_scope': 'recording', 'field': 'bpm', 'value_kind': 'value', 'number_value': 120,
           'unit': 'bpm', 'observed_at': 22, 'origin': 'selection_transfer', 'legacy_source': 'provider',
           'derived_from_observation_id': observations['bpm'], column: changed}
    with pytest.raises(sqlite3.IntegrityError, match='differs from its retained evidence'):
        mutate(store, lambda: store.conn.execute(
            'INSERT INTO metadata_observations(' + ','.join(row) + ') VALUES ('
            + ','.join('?' for _ in row) + ')', tuple(row.values())))


def test_genre_value_without_a_key_or_explicit_resolution_cannot_pass_a_null_check(catalog):
    store = catalog
    _, _, owner, _ = capture(store, fields={})
    row = store.conn.execute('SELECT input_entry_id,input_recording_id,input_video_id FROM metadata_deliveries WHERE id=?',
                              (owner,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        mutate(store, lambda: store.conn.execute('INSERT INTO metadata_observations(delivery_id,entry_id,recording_id,input_video_id,'
            'field_scope,field,value_kind,text_value,observed_at,origin) '
            "VALUES (?,?,?,?,'recording','genre','value','Unknown',20,'provider')", (owner, *tuple(row))))


def test_null_blank_and_withdrawal_keep_decisions_and_reveal_continuing_provider_updates(catalog):
    store = catalog
    entry, _, _, first = capture(store)
    mutate(store, lambda: evidence.select(store.conn, entry, 'bpm', first['bpm']))
    null = mutate(store, lambda: evidence.choose(store.conn, entry, 'bpm', None, now=22))
    assert evidence.effective(store.conn, entry, 'bpm') is None
    _, _, _, second = capture(store, key='capture:2', fields={'bpm': 130})
    mutate(store, lambda: evidence.select(store.conn, entry, 'bpm', second['bpm']))
    assert evidence.effective(store.conn, entry, 'bpm') is None
    blank = mutate(store, lambda: evidence.choose(store.conn, entry, 'bpm', '', now=23))
    assert evidence.effective(store.conn, entry, 'bpm') == ''
    withdrawn = mutate(store, lambda: evidence.choose(store.conn, entry, 'bpm', withdraw=True, now=24))
    assert evidence.effective(store.conn, entry, 'bpm') == 130
    choices = list(map(tuple, store.conn.execute("SELECT id,value_kind FROM metadata_observations WHERE origin='user' ORDER BY id")))
    assert choices == [(null, 'null'), (blank, 'blank'), (withdrawn, 'withdrawn')]
    for identity, _ in choices:
        with pytest.raises(sqlite3.IntegrityError, match='retained'):
            mutate(store, lambda: store.conn.execute('DELETE FROM metadata_observations WHERE id=?', (identity,)))
    reopened = sqlite3.connect(store._db_path)
    try:
        reopened.row_factory = sqlite3.Row
        assert evidence.effective(reopened, entry, 'bpm') == 130
        migration.validate(reopened)
    finally:
        reopened.close()


def test_decisions_remain_entry_local_while_base_recording_selection_is_shared(catalog):
    store = catalog
    entry, recording, _, observations = capture(store)
    with store.conn:
        related = store.conn.execute('INSERT INTO catalog_entries(recording_id,source_ref) VALUES (?,?)',
                                     (recording, 'related:input')).lastrowid
        store.conn.execute('INSERT INTO unresolved_items(entry_id,title,artist) VALUES (?,?,?)',
                           (related, 'Same song', 'Artist'))
    mutate(store, lambda: evidence.select(store.conn, related, 'bpm', observations['bpm']))
    mutate(store, lambda: evidence.choose(store.conn, entry, 'bpm', 90))
    assert evidence.effective(store.conn, entry, 'bpm') == 90
    assert evidence.effective(store.conn, related, 'bpm') == 120


def test_reassociation_cannot_select_old_subject_evidence(catalog):
    store = catalog
    entry, _, _, observations = capture(store)
    other, _, _, other_evidence = capture(store, video='other')
    with pytest.raises(ValueError, match='different subject'):
        mutate(store, lambda: evidence.select(store.conn, other, 'bpm', observations['bpm']))
    with pytest.raises(ValueError, match='different subject'):
        mutate(store, lambda: evidence.select(store.conn, entry, 'bpm', other_evidence['bpm']))
    assert store.conn.execute('SELECT count(*) FROM recording_metadata_selections').fetchone()[0] == 0


@pytest.mark.parametrize('with_alias', [False, True], ids=['without-alias', 'with-alias'])
def test_ambiguous_catalog_genre_capture_keeps_the_original_label(catalog, with_alias):
    store = catalog
    label = 'Review shared genre'
    with store.conn:
        store.conn.executemany('INSERT INTO genre_catalog(key,name,source) VALUES (?,?,?)',
                               [('review-shared-a', label, 'test'), ('review-shared-b', label, 'test')])
        if with_alias:
            store.conn.execute('INSERT INTO genre_alias(label_key,label,genre_key,source) VALUES (?,?,?,?)',
                               ('review shared genre', label, 'review-shared-a', 'test'))

    _, _, _, observations = capture(store, fields={'genre': label})
    row = store.conn.execute('SELECT text_value,genre_key,resolution_reason FROM metadata_observations WHERE id=?',
                             (observations['genre'],)).fetchone()
    assert tuple(row) == (label, None, 'ambiguous_genre')


def test_replay_does_not_reinterpret_an_unknown_genre_after_a_new_alias_is_added(catalog):
    store = catalog
    _, _, owner, observations = capture(store, fields={'genre': 'Unrecognized'})
    with store.conn:
        store.conn.execute("INSERT INTO genre_catalog(key,name,source) VALUES ('known','Unrecognized','test')")
    replayed = mutate(store, lambda: evidence.record_provider(store.conn, owner, {'genre': 'Unrecognized'}))
    assert replayed == observations
    row = store.conn.execute('SELECT genre_key,resolution_reason FROM metadata_observations WHERE id=?',
                              (observations['genre'],)).fetchone()
    assert tuple(row) == (None, 'unknown_genre')


@pytest.mark.parametrize('column,value', [
    ('field', 'arbitrary_attribute'), ('field_scope', 'artist'), ('unit', 'hz'), ('number_value', '120'),
    ('number_value', float('inf')), ('number_value', -1), ('value_kind', 'blank'), ('value_kind', 'withdrawn'),
    ('recording_id', -999), ('input_video_id', None),
])
def test_database_rejects_invalid_types_units_and_captured_subjects(catalog, column, value):
    store = catalog
    _, _, _, observations = capture(store)
    columns = ['delivery_id', 'entry_id', 'recording_id', 'input_video_id', 'field_scope', 'field', 'value_kind',
               'text_value', 'number_value', 'genre_key', 'unit', 'resolution_reason', 'observed_at', 'origin']
    original = store.conn.execute('SELECT ' + ','.join(columns) + ' FROM metadata_observations WHERE id=?',
                                  (observations['bpm'],)).fetchone()
    changed = dict(zip(columns, original))
    # Use an unused delivery, so a duplicate-field constraint cannot hide a
    # missing type/unit/scope constraint in the row being tested.
    _, _, unused, _ = capture(store, key='invalid-test', fields={})
    changed['delivery_id'] = unused
    changed[column] = value
    with pytest.raises(sqlite3.IntegrityError):
        mutate(store, lambda: store.conn.execute('INSERT INTO metadata_observations(' + ','.join(columns) + ') '
            'VALUES (' + ','.join('?' for _ in columns) + ')', tuple(changed.values())))


def test_conflicts_reference_compatible_observations_and_have_exactly_one_subject(catalog):
    store = catalog
    entry, recording, _, first = capture(store)
    _, _, _, second = capture(store, key='second', fields={'bpm': 130})
    other, _, _, wrong = capture(store, video='other')
    with store.conn:
        conflict = store.conn.execute("INSERT INTO metadata_conflicts(recording_id,field_scope,field,status,updated_at) VALUES (?,'recording','bpm','unresolved',21)",
                                       (recording,)).lastrowid
        store.conn.executemany('INSERT INTO metadata_conflict_candidates(conflict_id,observation_id) VALUES (?,?)',
                              [(conflict, first['bpm']), (conflict, second['bpm'])])
    with pytest.raises(sqlite3.IntegrityError, match='different subject'):
        mutate(store, lambda: store.conn.execute('INSERT INTO metadata_conflict_candidates(conflict_id,observation_id) VALUES (?,?)', (conflict, wrong['bpm'])))
    for subject in (None, entry):
        with pytest.raises(sqlite3.IntegrityError):
            mutate(store, lambda: store.conn.execute("INSERT INTO metadata_conflicts(entry_id,recording_id,field_scope,field,status) VALUES (?,?,'recording','bpm','unresolved')",
                                                     (subject, None if subject is None else recording)))
    chosen = mutate(store, lambda: evidence.choose(store.conn, entry, 'bpm', 125, now=22))
    mutate(store, lambda: store.conn.execute("UPDATE metadata_conflicts SET status='user_resolved',selected_observation_id=? WHERE id=?",
                                            (chosen, conflict)))
    row = store.conn.execute('SELECT status,selected_observation_id FROM metadata_conflicts WHERE id=?', (conflict,)).fetchone()
    assert tuple(row) == ('user_resolved', chosen)
    other_choice = mutate(store, lambda: evidence.choose(store.conn, other, 'bpm', 110))
    other_recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (other,)).fetchone()[0]
    mutate(store, lambda: store.conn.execute('INSERT INTO metadata_conflicts(recording_id,field_scope,field,status,selected_observation_id) '
        "VALUES (?,'recording','bpm','user_resolved',?)", (other_recording, other_choice)))
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_legacy_attribution_stays_explicitly_unresolved_instead_of_matching_equal_text(catalog):
    store = catalog
    _, recording, _, observations = capture(store)
    with store.conn:
        store.conn.execute('INSERT INTO recording_metadata_selections(recording_id,field,attribution_status,legacy_source,selected_at) '
                           "VALUES (?,'bpm','legacy_unresolved','old_provider',NULL)", (recording,))
    row = store.conn.execute('SELECT observation_id,legacy_source,selected_at FROM recording_metadata_selections').fetchone()
    assert tuple(row) == (None, 'old_provider', None)
    assert observations['bpm'] is not None
    with pytest.raises(sqlite3.IntegrityError):
        mutate(store, lambda: store.conn.execute("UPDATE recording_metadata_selections SET attribution_status='evidenced'"))


def test_unproven_legacy_scope_keeps_its_original_and_cannot_become_recording_evidence(catalog):
    store = catalog
    entry, recording, _, _ = capture(store)
    original = {'id': 97, 'track_id': entry, 'recording_id': None,
                'field': 'bpm', 'field_scope': 'legacy_item', 'resolved': 0,
                'candidates': '[{"provider":"old","value":120}]'}
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        delivery, = deliveries.record_many(store.conn, [deliveries.Delivery(
            'legacy', 'enrichment_conflict:97', 'legacy_unknown', None, None,
            key_contract='legacy_record', response=original)])
        store.conn.execute('INSERT INTO metadata_legacy_records(legacy_owner,legacy_id,delivery_id,entry_id,disposition) '
                           "VALUES ('enrichment_conflict',97,?,?,'unproven_scope')", (delivery, entry))
        observation = store.conn.execute('INSERT INTO metadata_observations(delivery_id,entry_id,field_scope,field,'
            'value_kind,number_value,unit,origin,legacy_owner,legacy_id,legacy_source) '
            "VALUES (?,?,'legacy_item','bpm','value',120,'bpm','legacy_conflict','enrichment_conflict',97,'old')",
            (delivery, entry)).lastrowid
        conflict = store.conn.execute('INSERT INTO metadata_conflicts(entry_id,field_scope,field,status,legacy_record_id) '
                                     "VALUES (?,'legacy_item','bpm','unresolved',97)", (entry,)).lastrowid
        store.conn.execute('INSERT INTO metadata_conflict_candidates(conflict_id,observation_id) VALUES (?,?)', (conflict, observation))
    assert deliveries.response(store.conn, delivery) == original
    assert store.conn.execute('SELECT recording_id FROM metadata_observations WHERE id=?', (observation,)).fetchone()[0] is None
    with pytest.raises(sqlite3.IntegrityError, match='different subject'):
        mutate(store, lambda: store.conn.execute('INSERT INTO recording_metadata_selections('
            'recording_id,field,observation_id,attribution_status) VALUES (?,\'bpm\',?,\'evidenced\')',
            (recording, observation)))
    with pytest.raises(sqlite3.IntegrityError, match='different captured input'):
        mutate(store, lambda: store.conn.execute('INSERT INTO metadata_observations(delivery_id,entry_id,recording_id,'
            'field_scope,field,value_kind,number_value,unit,origin,legacy_owner,legacy_id,legacy_position) '
            "VALUES (?,?,?,'recording','bpm','value',120,'bpm','legacy_conflict','enrichment_conflict',97,1)",
            (delivery, entry, recording)))
    # A subsequent explicit choice describes today's admitted subject. It can
    # resolve this retained conflict without rewriting its old candidate/input.
    chosen = mutate(store, lambda: evidence.choose(store.conn, entry, 'bpm', 125, now=40))
    mutate(store, lambda: store.conn.execute('UPDATE metadata_conflicts SET status=\'user_resolved\','
        'selected_observation_id=? WHERE id=?', (chosen, conflict)))
    mutate(store, lambda: store.conn.execute("UPDATE metadata_conflicts SET status='provider_converged' WHERE id=?",
                                            (conflict,)))
    mutate(store, lambda: store.conn.execute("UPDATE metadata_conflicts SET status='unresolved' WHERE id=?", (conflict,)))
    assert store.conn.execute('SELECT field_scope FROM metadata_conflicts WHERE id=?', (conflict,)).fetchone()[0] == 'legacy_item'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
