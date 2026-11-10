"""Retry state references immutable results and retains unknown legacy inputs."""
import json
import sqlite3

import pytest

from tests.core.test_metadata_evidence_migration import additive_catalog, catalog, capture, mutate
from yt_playlist.core import metadata_attempts as attempts, metadata_legacy as legacy
from yt_playlist.core.activity import WorkBudgetExceeded


def test_retry_field_merge_cycle_reset_and_old_replay_do_not_roll_frontier_back(catalog):
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    first, inserted = mutate(store, lambda: attempts.record(store.conn, entry, 'a',
        {'genre': 'Jazz', 'year': '2000', 'mb_release_ids': ['release']}, cycle=1, run_id='first', now=10))
    assert inserted
    second, _ = mutate(store, lambda: attempts.record(store.conn, entry, 'a', {'bpm': 120},
        cycle=1, run_id='retry', retry=True, now=20, retry_after=80, error='temporary'))
    state = store.conn.execute('SELECT * FROM enrichment_provider_state').fetchone()
    assert (state['status'], state['failures'], state['retry_at']) == ('retry', 1, 80)
    assert attempts.state_fields(store.conn, recording, 'a') == {
        'genre': 'Jazz', 'year': '2000', 'mb_release_ids': ['release'], 'bpm': 120}
    assert attempts.attempt_fields(store.conn, first) == {'genre': 'Jazz', 'year': '2000', 'mb_release_ids': ['release']}
    assert attempts.attempt_fields(store.conn, second) == {'bpm': 120}
    with pytest.raises(WorkBudgetExceeded):
        attempts.state_fields(store.conn, recording, 'a', max_bytes=1)
    before = list(store.conn.iterdump())
    replay = mutate(store, lambda: attempts.record(store.conn, entry, 'a',
        {'genre': 'Jazz', 'year': '2000', 'mb_release_ids': ['release']}, cycle=1, run_id='first', now=30))
    assert replay == (first, False)
    assert list(store.conn.iterdump()) == before
    mutate(store, lambda: attempts.record(store.conn, entry, 'a', {}, cycle=2, run_id='new-cycle', now=40))
    assert attempts.state_fields(store.conn, recording, 'a') == {}
    assert store.conn.execute('SELECT failures FROM enrichment_provider_state').fetchone()[0] == 0
    assert attempts.completed_genres(store.conn, recording) == {'a': None}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_one_result_supplies_attempt_and_current_fields_without_copying_its_values(catalog):
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    fields = {'genre': 'Jazz', 'bpm': 120, 'isrc': ['ONE', 'TWO']}
    delivery, observations = mutate(store, lambda: attempts.capture_result(store.conn, entry, 'a', fields,
                                                                          run_id='one', now=10))
    before = store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0]
    attempt, inserted = mutate(store, lambda: attempts.record(store.conn, entry, 'a', fields,
        delivery_id=delivery, observations=observations, cycle=1, now=11))
    assert inserted
    assert store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] == before
    assert list(map(tuple, store.conn.execute('SELECT field,observation_id FROM provider_attempt_fields '
        'WHERE attempt_id=? ORDER BY field', (attempt,)))) == [('bpm', observations['bpm']), ('genre', observations['genre']), ('isrc', None)]
    assert attempts.completed_genres(store.conn, recording) == {'a': 'Jazz'}
    assert 'fields_json' not in {row[1] for row in store.conn.execute('PRAGMA table_info(provider_attempts)')}


def test_legacy_attempt_and_state_keep_separate_evidence_without_guessing_a_matching_delivery(additive_catalog):
    store = additive_catalog
    _, recording, _, _ = capture(store, fields={})
    with store.conn:
        old = store.conn.execute('INSERT INTO provider_attempts(recording_id,input_video_id,provider,at,status,fields_json) '
            "VALUES (?,'video','a',10,'found',?)", (recording, json.dumps({'genre': 'Jazz', 'isrc': ['ONE']}))).lastrowid
        store.conn.execute('INSERT INTO enrichment_provider_state VALUES (?,\'a\',1,10,\'found\',?,0,0,\'\')',
                           (recording, json.dumps({'genre': 'Jazz', 'bpm': 120})))
    mutate(store, lambda: legacy.backfill_operational(store.conn))
    assert attempts.attempt_fields(store.conn, old) == {'genre': 'Jazz', 'isrc': ['ONE']}
    assert attempts.state_fields(store.conn, recording, 'a') == {'genre': 'Jazz', 'bpm': 120}
    observations = store.conn.execute("SELECT * FROM metadata_observations WHERE origin IN ('legacy_attempt','legacy_state')").fetchall()
    assert len(observations) == 3 and len({row['delivery_id'] for row in observations}) == 2
    assert all(row['entry_id'] is None and row['recording_id'] == recording and row['field_scope'] == 'legacy_recording'
               for row in observations)
    assert attempts.completed_genres(store.conn, recording) == {'a': 'Jazz'}
    before = list(store.conn.iterdump())
    mutate(store, lambda: legacy.backfill_operational(store.conn))
    assert list(store.conn.iterdump()) == before
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_provider_state_rejects_cross_provider_evidence_and_cannot_admit_a_bare_finite_field(catalog):
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    mutate(store, lambda: attempts.record(store.conn, entry, 'a', {'bpm': 120}, cycle=1, run_id='a', now=10))
    mutate(store, lambda: attempts.record(store.conn, entry, 'b', {'bpm': 130}, cycle=1, run_id='b', now=11))
    wrong = store.conn.execute("SELECT delivery_id,observation_id FROM provider_state_fields WHERE provider='b'").fetchone()
    with pytest.raises(sqlite3.IntegrityError, match='different subject'):
        mutate(store, lambda: store.conn.execute("UPDATE provider_state_fields SET delivery_id=?,observation_id=? WHERE provider='a'",
                                                 tuple(wrong)))
    with pytest.raises(sqlite3.IntegrityError):
        mutate(store, lambda: store.conn.execute("UPDATE provider_state_fields SET observation_id=NULL WHERE provider='a'"))
    assert attempts.state_fields(store.conn, recording, 'a') == {'bpm': 120}


def test_migrated_provider_state_can_refresh_without_deleting_its_retained_result(additive_catalog):
    store = additive_catalog
    _, recording, _, _ = capture(store, fields={})
    repo = store.enrichment_state
    repo._configure_providers({'a': {'fingerprint': 'first', 'status': 'available'}})
    with store.conn:
        store.conn.execute('INSERT INTO enrichment_provider_state VALUES (?,\'a\',1,10,\'found\',?,0,0,\'\')',
                           (recording, json.dumps({'genre': 'Jazz', 'bpm': 120})))
    mutate(store, lambda: legacy.backfill_operational(store.conn))
    assert attempts.state_fields(store.conn, recording, 'a') == {'genre': 'Jazz', 'bpm': 120}
    before = {table: list(map(tuple, store.conn.execute(f'SELECT * FROM {table} ORDER BY 1,2')))
              for table in ('metadata_deliveries', 'metadata_observations', 'metadata_legacy_records')}
    current = {'fingerprint': 'second', 'status': 'available'}
    repo._configure_providers({'a': current})
    assert repo._advance_provider_refresh('a', current)
    assert not store.conn.execute('SELECT 1 FROM enrichment_provider_state').fetchone()
    assert not store.conn.execute('SELECT 1 FROM provider_state_fields').fetchone()
    for table, rows in before.items():
        assert list(map(tuple, store.conn.execute(f'SELECT * FROM {table} ORDER BY 1,2'))) == rows
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_result_consumers_share_exact_evidence_and_identifier_lineage(catalog):
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    fields = {'genre': 'Jazz', 'year': '2000', 'bpm': 120, 'mb_recording_id': 'mbid', 'isrc': ['ONE', 'TWO']}
    delivery, observations = store.enrichment_state.capture_provider_result(entry, 'a', fields, run_id='run', now=10)
    for field, value in fields.items():
        store.enrichment_state.log_enrichment(entry, 'run', 'a', field, value, delivery_id=delivery)
    store.enrichment.apply_provider_fields(entry, 'a', fields, providers=('a',), delivery_id=delivery)
    mutate(store, lambda: attempts.record(store.conn, entry, 'a', fields, cycle=1, run_id='run',
                                          delivery_id=delivery, now=11))
    selected = dict(store.conn.execute('SELECT field,observation_id FROM recording_metadata_selections '
                                       'WHERE recording_id=?', (recording,)))
    selected.update(store.conn.execute('SELECT field,observation_id FROM entry_metadata_selections '
                                       "WHERE entry_id=? AND field='mb_year'", (entry,)))
    assert selected == observations
    identifiers = list(map(tuple, store.conn.execute('SELECT value,delivery_id,observation_id '
        'FROM recording_identifiers ORDER BY value')))
    assert identifiers == [('ONE', delivery, None), ('TWO', delivery, None), ('mbid', delivery, observations['mb_recording_id'])]
    assert store.conn.execute('SELECT count(*) FROM metadata_observations WHERE delivery_id=?', (delivery,)).fetchone()[0] == 4
    before = list(store.conn.iterdump())
    store.enrichment_state.log_enrichment(entry, 'run', 'a', 'isrc', ['ONE', 'TWO'], delivery_id=delivery)
    assert list(store.conn.iterdump()) == before
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize(('provider', 'run_id', 'field', 'value'), [
    ('b', 'run', 'bpm', 120), ('a', 'different-run', 'bpm', 120),
    ('a', 'run', 'genre', None), ('a', 'run', 'available', True),
])
def test_log_rejects_borrowed_delivery_missing_null_and_changed_scalar_type(catalog, provider, run_id, field, value):
    store = catalog
    entry, _, _, _ = capture(store, fields={})
    delivery, _ = store.enrichment_state.capture_provider_result(entry, 'a', {'bpm': 120, 'available': 1},
                                                                 run_id='run', now=10)
    before = list(store.conn.iterdump())
    with pytest.raises(ValueError):
        store.enrichment_state.log_enrichment(entry, run_id, provider, field, value, delivery_id=delivery)
    assert list(store.conn.iterdump()) == before


def test_waterfall_links_conflicts_and_attempts_to_the_one_result_per_provider(catalog):
    from tests.test_waterfall import FakeProv, _cfg
    from yt_playlist.providers.waterfall import run_waterfall
    store = catalog
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200,
                                      thumbnail='https://example.test/cover.jpg')
    track = dict(store.tracks.track_for_waterfall(entry))
    a = FakeProv('a', {'Song': {'genre': 'Jazz', 'bpm': 120}})
    b = FakeProv('b', {'Song': {'genre': 'Rock', 'bpm': 130}})
    done = run_waterfall(store, [track], _cfg(('a', True), ('b', True)), lambda _event: None,
                         registry={'a': a, 'b': b}, run_id='one-request', now_fn=lambda: 10)
    assert done == [entry]
    assert store.tracks.get_track_enrichment(entry)[0] == 'Jazz'
    assert list(map(tuple, store.conn.execute("SELECT provider,count(*) FROM metadata_deliveries "
        "WHERE provider IN ('a','b') GROUP BY provider ORDER BY provider"))) == [('a', 1), ('b', 1)]
    candidates = list(map(tuple, store.conn.execute('SELECT d.provider,o.field,o.number_value,o.text_value '
        'FROM metadata_conflict_candidates c JOIN metadata_observations o ON o.id=c.observation_id '
        'JOIN metadata_deliveries d ON d.id=o.delivery_id ORDER BY d.provider,o.field')))
    assert candidates == [('a', 'bpm', 120, None), ('a', 'genre', None, 'Jazz'),
                          ('b', 'bpm', 130, None), ('b', 'genre', None, 'Rock')]
    assert not store.conn.execute('SELECT 1 FROM provider_attempts a JOIN provider_attempt_fields f ON f.attempt_id=a.id '
        'JOIN metadata_observations o ON o.id=f.observation_id WHERE a.delivery_id<>o.delivery_id').fetchone()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_waterfall_reuses_completed_answers_without_manufacturing_deliveries(catalog):
    from tests.test_waterfall import FakeProv, _cfg
    from yt_playlist.providers.waterfall import run_waterfall
    store = catalog
    entry = store.tracks.upsert_track('reuse-video', 'Song', 'Artist', None, 200,
                                      thumbnail='https://example.test/cover.jpg')
    track = dict(store.tracks.track_for_waterfall(entry))
    a = FakeProv('a', {'Song': {'genre': 'Jazz', 'bpm': 120}})
    b = FakeProv('b', {'Song': {'genre': 'Rock', 'bpm': 130}})
    configuration = _cfg(('a', True), ('b', True))
    arguments = {'registry': {'a': a, 'b': b}}
    assert run_waterfall(store, [track], configuration, lambda _: None,
                         run_id='original', now_fn=lambda: 10, **arguments) == [entry]
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM metadata_observations ORDER BY id')]
    deliveries = store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0]
    assert run_waterfall(store, [track], configuration, lambda _: None,
                         run_id='reuse', now_fn=lambda: 20, reuse_completed=True, **arguments) == [entry]
    assert a.calls == b.calls == ['Song']
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM metadata_observations ORDER BY id')] == before
    assert store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == deliveries
    assert store.tracks.get_track_enrichment(entry)[0] == 'Jazz'


def test_reuse_of_legacy_recording_state_keeps_its_original_missing_item_scope(additive_catalog):
    store = additive_catalog
    entry, recording, _, _ = capture(store, fields={})
    with store.conn:
        store.conn.execute("INSERT INTO enrichment_provider_state VALUES (?,'a',1,10,'found',?,0,0,'')",
                           (recording, json.dumps({'genre': 'Jazz', 'year': '2000'})))
    mutate(store, lambda: legacy.backfill_operational(store.conn))
    original = dict(store.conn.execute("SELECT * FROM metadata_observations WHERE origin='legacy_state' AND field='genre'").fetchone())
    deliveries = store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0]
    observations = store.enrichment.reuse_provider_fields(entry, 'a', {'genre': 'Jazz', 'year': '2000'},
                                                         providers=('a',), now=20)
    assert set(observations) == {'genre'}
    derived = store.conn.execute('SELECT * FROM metadata_observations WHERE id=?', (observations['genre'],)).fetchone()
    assert derived['origin'] == 'selection_transfer' and derived['derived_from_observation_id'] == original['id']
    assert derived['entry_id'] == entry and derived['recording_id'] == recording
    assert original['entry_id'] is None and original['field_scope'] == 'legacy_recording'
    assert dict(store.conn.execute('SELECT * FROM metadata_observations WHERE id=?', (original['id'],)).fetchone()) == original
    assert store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == deliveries
    assert store.conn.execute('SELECT genre FROM recordings WHERE id=?', (recording,)).fetchone()[0] == 'Jazz'
    assert not store.conn.execute("SELECT 1 FROM entry_metadata_selections WHERE entry_id=? AND field='mb_year'", (entry,)).fetchone()
    count = store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0]
    assert store.enrichment.reuse_provider_fields(entry, 'a', {'genre': 'Jazz', 'year': '2000'},
                                                  providers=('a',), now=30) == observations
    assert store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] == count
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_genre_refresh_selects_the_existing_alternative_observation(catalog):
    from yt_playlist.core import metadata
    store = catalog
    entry, recording, _, _ = capture(store, fields={})
    for provider, genre, at in (('a', 'Jazz', 10), ('b', 'Rock', 11)):
        delivery, _ = mutate(store, lambda: attempts.capture_result(
            store.conn, entry, provider, {'genre': genre}, run_id=provider, now=at))
        store.enrichment.apply_provider_fields(entry, provider, {'genre': genre}, providers=('a', 'b'), delivery_id=delivery)
        mutate(store, lambda: attempts.record(store.conn, entry, provider, {'genre': genre}, cycle=1,
                                              run_id=provider, delivery_id=delivery, now=at))
    expected = store.conn.execute("SELECT observation_id FROM provider_state_fields WHERE recording_id=? AND provider='b' AND field='genre'",
                                  (recording,)).fetchone()[0]
    counts = tuple(store.conn.execute('SELECT (SELECT count(*) FROM metadata_observations),'
                                      '(SELECT count(*) FROM metadata_deliveries)').fetchone())
    mutate(store, lambda: metadata.refresh_genre(store.conn, recording, {'a': None, 'b': 'Rock'}, ('a', 'b')))
    assert store.conn.execute("SELECT observation_id FROM recording_metadata_selections WHERE recording_id=? AND field='genre'",
                              (recording,)).fetchone()[0] == expected
    assert tuple(store.conn.execute('SELECT (SELECT count(*) FROM metadata_observations),'
                                     '(SELECT count(*) FROM metadata_deliveries)').fetchone()) == counts
    assert store.conn.execute('SELECT genre FROM recordings WHERE id=?', (recording,)).fetchone()[0] == 'Rock'


def test_coverage_refresh_keeps_unchanged_provider_candidate_evidence(catalog):
    from tests.test_waterfall import FakeProv, _cfg
    from yt_playlist.providers.waterfall import run_waterfall
    store = catalog
    entry = store.tracks.upsert_track('coverage-video', 'Song', 'Artist', None, 200,
                                      thumbnail='https://example.test/cover.jpg')
    track = dict(store.tracks.track_for_waterfall(entry))
    a = FakeProv('a', {'Song': {'genre': 'Jazz', 'bpm': 120}})
    b = FakeProv('b', {'Song': {'genre': 'Rock', 'bpm': 130}})
    configuration = _cfg(('a', True), ('b', True))
    arguments = {'registry': {'a': a, 'b': b}}
    assert run_waterfall(store, [track], configuration, lambda _: None,
                         run_id='coverage-original', now_fn=lambda: 10, **arguments) == [entry]
    original = list(map(tuple, store.conn.execute("SELECT o.field,o.id FROM metadata_conflict_candidates c "
        "JOIN metadata_observations o ON o.id=c.observation_id JOIN metadata_deliveries d ON d.id=o.delivery_id "
        "WHERE d.provider='a' ORDER BY o.field")))
    a_count = store.conn.execute("SELECT count(*) FROM metadata_deliveries WHERE provider='a'").fetchone()[0]
    assert run_waterfall(store, [{**track, 'provider_jobs': ['b']}], configuration, lambda _: None,
                         run_id='coverage-new-provider', now_fn=lambda: 20, **arguments) == [entry]
    assert a.calls == ['Song'] and b.calls == ['Song', 'Song']
    assert store.conn.execute("SELECT count(*) FROM metadata_deliveries WHERE provider='a'").fetchone()[0] == a_count
    assert list(map(tuple, store.conn.execute("SELECT o.field,o.id FROM metadata_conflict_candidates c "
        "JOIN metadata_observations o ON o.id=c.observation_id JOIN metadata_deliveries d ON d.id=o.delivery_id "
        "WHERE d.provider='a' ORDER BY o.field"))) == original
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_legacy_completed_conflicts_use_derived_evidence_with_the_original_parent(additive_catalog):
    from tests.test_waterfall import FakeProv, _cfg
    from yt_playlist.providers.waterfall import run_waterfall
    store = additive_catalog
    entry, recording, _, _ = capture(store, fields={})
    with store.conn:
        store.conn.execute('UPDATE recordings SET last_enriched_at=1 WHERE id=?', (recording,))
        store.conn.executemany('INSERT INTO enrichment_provider_state VALUES (?,?,1,10,\'found\',?,0,0,\'\')',
            [(recording, 'a', json.dumps({'genre': 'Jazz', 'bpm': 120})),
             (recording, 'b', json.dumps({'genre': 'Rock', 'bpm': 130}))])
    mutate(store, lambda: legacy.backfill_operational(store.conn))
    a, b = FakeProv('a', {}), FakeProv('b', {})
    before = store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0]
    assert run_waterfall(store, [dict(store.tracks.track_for_waterfall(entry))], _cfg(('a', True), ('b', True)),
        lambda _: None, registry={'a': a, 'b': b}, run_id='reuse-legacy', now_fn=lambda: 20,
        reuse_completed=True) == [entry]
    assert a.calls == b.calls == []
    rows = store.conn.execute('SELECT o.origin,o.entry_id,o.recording_id,p.origin AS parent_origin,'
        'p.entry_id AS parent_entry,p.field_scope AS parent_scope FROM metadata_conflict_candidates c '
        'JOIN metadata_observations o ON o.id=c.observation_id '
        'JOIN metadata_observations p ON p.id=o.derived_from_observation_id').fetchall()
    assert len(rows) == 4
    assert all(tuple(row) == ('selection_transfer', entry, recording, 'legacy_state', None, 'legacy_recording')
               for row in rows)
    assert store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == before
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
