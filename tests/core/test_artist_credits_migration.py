"""Ordered content reuse preserves distinct captures and selected-credit lineage."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3
from threading import Barrier

import pytest

from yt_playlist.core import artist_credits as credits, artists
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations import v074_artist_credits as migration
from yt_playlist.core.migrations.runner import Context, MigrationError
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path):
    store = Store(tmp_path / 'credits.db')
    migrate(store.conn, registry=registry.MIGRATIONS[:69])
    store._bind_repositories()
    yield store
    store.conn.close()


def apply(store):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        migration.apply(store.conn, Context(4))
    migration.validate(store.conn)


@pytest.fixture
def normalized(predecessor):
    apply(predecessor)
    return predecessor


def entry(store, video='video', *, structured=None, name='First, Second'):
    return store.tracks.upsert_track(video, 'Song', name, None, 200,
        credits=structured if structured is not None else [
            {'name': 'First', 'id': 'first', 'role': 'primary'},
            {'name': 'Second', 'id': 'second', 'role': 'guest'}])


def observe(store, target, structured=None, *, now=20, source='youtube', delivery_key=None):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        artists.observe(store.conn, target, 'First, Second', structured if structured is not None else [
            {'name': 'First', 'id': 'first', 'role': 'primary'},
            {'name': 'Second', 'id': 'second', 'role': 'guest'}],
            source=source, now=now, delivery_key=delivery_key)


def observation(store, target):
    return store.conn.execute('SELECT o.* FROM entry_artist_selections s '
        'JOIN artist_credit_observations o ON o.id=s.observation_id WHERE s.entry_id=?', (target,)).fetchone()


def test_legacy_captures_keep_every_id_source_and_unknown_or_distinct_time(predecessor):
    store = predecessor
    target = entry(store)
    observe(store, target, now=10)
    observe(store, target, now=11)
    observe(store, target, now=11)  # Identical timestamps do not prove one provider delivery.
    with store.conn:
        store.conn.execute('UPDATE artist_credit_observations SET observed_at=NULL WHERE id=1')
    before = list(map(tuple, store.conn.execute('SELECT id,entry_id,source,observed_at FROM artist_credit_observations')))
    projection = list(map(tuple, store.conn.execute('SELECT * FROM track_artists ORDER BY entry_id,position')))
    apply(store)
    after = list(map(tuple, store.conn.execute('SELECT id,entry_id,source,first_seen FROM artist_credit_observations '
                                              "WHERE capture_kind='legacy_capture' ORDER BY id")))
    assert after == before
    assert list(map(tuple, store.conn.execute('SELECT * FROM track_artists ORDER BY entry_id,position'))) == projection
    assert store.conn.execute('SELECT count(*) FROM artist_credits').fetchone()[0] == 2
    assert store.conn.execute('SELECT count(*) FROM artist_credit_members').fetchone()[0] == 4
    assert store.conn.execute("SELECT sum(processing_count) FROM artist_credit_observations WHERE capture_kind='legacy_capture'").fetchone()[0] == len(before)
    assert store.conn.execute('SELECT last_seen FROM artist_credit_observations WHERE id=1').fetchone()[0] is None
    assert store.conn.execute("SELECT count(*) FROM artist_credit_observations WHERE delivery_key IS NOT NULL").fetchone()[0] == 0
    selected = observation(store, target)
    assert selected['capture_kind'] == 'selection_snapshot'
    assert selected['id'] not in {row[0] for row in before}
    assert selected['first_seen'] is None and selected['last_seen'] is None
    assert 'credits_json' not in {row[1] for row in store.conn.execute('PRAGMA table_info(artist_credit_observations)')}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_ambiguous_name_only_history_remains_unresolved_with_explicit_selected_snapshot(predecessor):
    store = predecessor
    target = entry(store, structured=[{'name': 'Same'}], name='Same')
    original = store.conn.execute('SELECT id FROM artist_credit_observations').fetchone()[0]
    selected_id = store.artists.track_credits(target)[0]['artist_id']
    store.artists.ensure_artist('Same', provider='youtube', provider_id='homonym')
    apply(store)
    old = store.conn.execute('SELECT credit_id,first_seen FROM artist_credit_observations WHERE id=?', (original,)).fetchone()
    assert credits.read(store.conn, old['credit_id']).members[0].artist_id is None
    current = observation(store, target)
    assert current['capture_kind'] == 'selection_snapshot'
    assert current['first_seen'] is None and current['last_seen'] is None
    assert credits.read(store.conn, current['credit_id']).members[0].artist_id == selected_id
    assert store.artists.resolve('Same') is None


def test_repeated_processing_reuses_one_capture_without_current_projection_writes(normalized):
    store = normalized
    target = entry(store)
    first = dict(observation(store, target))
    queries = []
    store.conn.set_trace_callback(queries.append)
    try:
        for when in range(20, 220):
            observe(store, target, now=when)
    finally:
        store.conn.set_trace_callback(None)
    current = observation(store, target)
    assert current['id'] == first['id'] and current['processing_count'] == 201
    assert current['first_seen'] == 20 and current['last_seen'] == first['last_seen']
    assert store.conn.execute('SELECT count(*) FROM artist_credit_observations').fetchone()[0] == 1
    assert store.conn.execute('SELECT count(*) FROM artist_credits').fetchone()[0] == 1
    assert not any(sql.upper().startswith(('INSERT INTO TRACK_ARTISTS', 'DELETE FROM TRACK_ARTISTS',
                                           'UPDATE TRACK_ARTISTS')) for sql in queries)


def test_repeated_processing_also_avoids_projection_churn_before_schema_cutover(predecessor):
    target = entry(predecessor)
    queries = []
    predecessor.conn.set_trace_callback(queries.append)
    try:
        observe(predecessor, target)
    finally:
        predecessor.conn.set_trace_callback(None)
    assert not any(sql.upper().startswith(('INSERT INTO TRACK_ARTISTS', 'DELETE FROM TRACK_ARTISTS',
                                           'UPDATE TRACK_ARTISTS')) for sql in queries)


def test_return_to_earlier_credit_is_a_new_state_capture_not_erased_history(normalized):
    store = normalized
    target = entry(store)
    original = dict(observation(store, target))
    observe(store, target, [{'name': 'Second', 'id': 'second'}, {'name': 'First', 'id': 'first'}])
    changed = dict(observation(store, target))
    observe(store, target)
    returned = dict(observation(store, target))
    assert len({original['id'], changed['id'], returned['id']}) == 3
    assert original['credit_id'] == returned['credit_id'] != changed['credit_id']
    assert [(row['credit'], row['position']) for row in store.artists.track_credits(target)] == [('First', 0), ('Second', 1)]


def test_stable_provider_delivery_is_idempotent_and_old_replay_cannot_replace_new_selection(normalized):
    store = normalized
    target = entry(store)
    observe(store, target, delivery_key='revision:1')
    first = dict(observation(store, target))
    observe(store, target, now=21, delivery_key='revision:1')
    assert observation(store, target)['id'] == first['id']
    assert observation(store, target)['processing_count'] == 2
    observe(store, target, [{'name': 'Third', 'id': 'third'}], now=22, delivery_key='revision:2')
    second = dict(observation(store, target))
    observe(store, target, now=23, delivery_key='revision:1')
    assert observation(store, target)['id'] == second['id']
    assert [row['credit'] for row in store.artists.track_credits(target)] == ['Third']
    assert store.conn.execute('SELECT processing_count FROM artist_credit_observations WHERE id=?',
                              (first['id'],)).fetchone()[0] == 3
    before = list(store.conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='delivery cannot change'):
        observe(store, target, [{'name': 'Different', 'id': 'different'}], delivery_key='revision:1')
    assert list(store.conn.iterdump()) == before


def test_source_and_entry_have_independent_delivery_identity(normalized):
    store = normalized
    one, two = entry(store), entry(store, video='other')
    observe(store, one, delivery_key='revision:1')
    observe(store, two, delivery_key='revision:1')
    observe(store, one, source='independent_capture', delivery_key='revision:1')
    rows = store.conn.execute('SELECT entry_id,source FROM artist_credit_observations '
                              "WHERE delivery_key='revision:1'").fetchall()
    assert set(map(tuple, rows)) == {(one, 'youtube'), (two, 'youtube'), (one, 'independent_capture')}


def test_replay_from_another_source_does_not_undo_a_later_selection(normalized):
    store = normalized
    target = entry(store)
    observe(store, target, delivery_key='youtube:1')
    observe(store, target, [{'name': 'Third', 'id': 'third'}], source='other', delivery_key='other:1')
    selected = dict(observation(store, target))
    observe(store, target, delivery_key='youtube:1')
    assert observation(store, target)['id'] == selected['id']
    assert [row['credit'] for row in store.artists.track_credits(target)] == ['Third']


def test_unchanged_source_capture_does_not_undo_a_later_provider_selection(normalized):
    store = normalized
    target = entry(store)
    observe(store, target, [{'name': 'Third', 'id': 'third'}], source='other')
    selected = dict(observation(store, target))
    observe(store, target)
    assert observation(store, target)['id'] == selected['id']


def test_old_delivery_remains_replayable_after_artist_identity_reconciliation(normalized):
    store = normalized
    target = entry(store, structured=[{'name': 'First'}], name='First')
    observe(store, target, [{'name': 'First'}], delivery_key='before-resolution')
    earlier = dict(observation(store, target))
    historical = credits.read(store.conn, earlier['credit_id'])
    known = entry(store, video='known', structured=[{'name': 'First', 'id': 'first'}], name='First')
    observe(store, target, [{'name': 'First', 'id': 'first'}], delivery_key='resolved')
    selected = dict(observation(store, target))
    observe(store, target, [{'name': 'First'}], delivery_key='before-resolution')
    assert observation(store, target)['id'] == selected['id']
    assert credits.read(store.conn, earlier['credit_id']) == historical
    assert store.artists.track_credits(target)[0]['artist_id'] == store.artists.track_credits(known)[0]['artist_id']


def test_repeated_artist_positions_roles_spelling_and_raw_absence_are_distinct_content(normalized):
    store = normalized
    target = entry(store, structured=[{'name': 'First', 'id': 'first', 'role': 'voice'},
                                     {'name': 'First', 'id': 'first', 'role': 'guitar'}])
    value = credits.read(store.conn, observation(store, target)['credit_id'])
    assert len(value.members) == 2 and value.members[0].artist_id == value.members[1].artist_id
    assert [member.role for member in value.members] == ['voice', 'guitar']
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        original = credits.intern(store.conn, value)
        for changed in (replace(value, display_credit='Other spelling'),
                        replace(value, members=value.members[::-1]),
                        replace(value, members=(replace(value.members[0], role='drums'), value.members[1])),
                        replace(value, members=(replace(value.members[0], presence=1), value.members[1]))):
            if changed.members[0].presence == 1:
                changed = replace(changed, members=(replace(changed.members[0], role=None), changed.members[1]))
            assert credits.intern(store.conn, changed) != original
    assert [row['position'] for row in store.artists.track_credits(target)] == [0, 1]


def test_digest_reuse_checks_full_semantic_content(normalized, monkeypatch):
    store = normalized
    target = entry(store)
    original = credits.read(store.conn, observation(store, target)['credit_id'])
    digest = original.digest()
    monkeypatch.setattr(credits.Credit, 'digest', lambda self: digest)
    with pytest.raises(sqlite3.IntegrityError, match='digest collision'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        credits.intern(store.conn, replace(original, display_credit='Distinct evidence'))


def test_artist_reconciliation_keeps_historical_credit_identity_and_new_selected_evidence(normalized):
    store = normalized
    unknown = entry(store, structured=[{'name': 'First'}], name='First')
    old = dict(observation(store, unknown))
    previous = store.artists.track_credits(unknown)[0]['artist_id']
    known = entry(store, video='known', structured=[{'name': 'First', 'id': 'first'}], name='First')
    target = store.artists.track_credits(known)[0]['artist_id']
    historical = credits.read(store.conn, old['credit_id'])
    observe(store, unknown, [{'name': 'First', 'id': 'first'}])
    assert store.artists.resolve(previous) == target
    assert store.artists.track_credits(unknown)[0]['artist_id'] == target
    assert credits.read(store.conn, old['credit_id']) == historical
    assert historical.members[0].artist_id == previous
    assert credits.read(store.conn, observation(store, unknown)['credit_id']).members[0].artist_id == target
    reconciled = store.conn.execute("SELECT credit_id FROM artist_credit_observations WHERE capture_kind='identity_reconciliation'").fetchone()
    assert credits.read(store.conn, reconciled[0]).members[0].artist_id == target
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('problem', ['unknown_field', 'invalid_time', 'invalid_member', 'oversize'])
def test_bad_legacy_evidence_fails_before_schema_changes(predecessor, problem):
    store = predecessor
    entry(store)
    with store.conn:
        if problem == 'invalid_time':
            store.conn.execute("UPDATE artist_credit_observations SET observed_at='unknown'")
        else:
            raw = ('[{"name":"First","unrecognized":1}]' if problem == 'unknown_field' else
                   '[{"name":2}]' if problem == 'invalid_member' else
                   json.dumps([{'name': 'x' * credits.MAX_BYTES}]))
            store.conn.execute('UPDATE artist_credit_observations SET credits_json=?', (raw,))
    before = list(store.conn.iterdump())
    with pytest.raises(MigrationError, match='Invalid artist credit evidence'):
        apply(store)
    assert list(store.conn.iterdump()) == before
    assert not credits.available(store.conn)


def test_interrupted_conversion_rolls_back_original_ids_and_retries(predecessor, monkeypatch):
    store = predecessor
    entry(store)
    before = list(store.conn.iterdump())
    original = migration._selections
    def interrupted(conn):
        original(conn)
        raise RuntimeError('interrupted credit conversion')
    monkeypatch.setattr(migration, '_selections', interrupted)
    with pytest.raises(RuntimeError, match='interrupted credit conversion'):
        apply(store)
    assert list(store.conn.iterdump()) == before
    monkeypatch.setattr(migration, '_selections', original)
    apply(store)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('mutation', [
    "UPDATE artist_credits SET display_credit='changed'",
    "UPDATE artist_credit_members SET role='changed'",
    "INSERT INTO artist_credit_members SELECT credit_id,2,artist_id,credit_text,provider,raw_id,raw_browse_id,role,presence FROM artist_credit_members WHERE position=0",
    "UPDATE artist_credit_observations SET credit_id=credit_id",
    "UPDATE artist_credit_observations SET processing_count=processing_count+2",
])
def test_sql_rejects_mutable_history_and_incoherent_processing(normalized, mutation):
    entry(normalized)
    with pytest.raises(sqlite3.IntegrityError), normalized.conn:
        normalized.conn.execute('BEGIN IMMEDIATE')
        normalized.conn.execute(mutation)


def test_selected_evidence_cannot_borrow_another_entrys_observation(normalized):
    store = normalized
    one, two = entry(store), entry(store, video='other')
    second = observation(store, two)['id']
    with pytest.raises(sqlite3.IntegrityError, match='different owner'), store.conn:
        store.conn.execute('UPDATE entry_artist_selections SET observation_id=? WHERE entry_id=?', (second, one))


def test_concurrent_repeated_delivery_and_restart_keep_one_capture(normalized):
    store = normalized
    target = entry(store)
    gate = Barrier(2)
    def deliver(index):
        opened = Store(store._db_path)
        opened._bind_repositories()
        try:
            gate.wait(timeout=5)
            observe(opened, target, now=30 + index, delivery_key='same-provider-revision')
        finally:
            opened.conn.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(deliver, range(2)))
    opened = Store(store._db_path)
    opened._bind_repositories()
    try:
        rows = opened.conn.execute("SELECT * FROM artist_credit_observations WHERE delivery_key='same-provider-revision'").fetchall()
        assert len(rows) == 1 and rows[0]['processing_count'] == 2
        assert (rows[0]['first_seen'], rows[0]['last_seen']) == (30, 31)
        assert observation(opened, target)['id'] == rows[0]['id']
        migration.validate(opened.conn)
        assert not opened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        opened.conn.close()
