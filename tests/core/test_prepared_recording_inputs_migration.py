"""Accepted inputs keep their encoder semantics and explicit unresolved identity."""
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core import prepared_inputs
from yt_playlist.core.migrations import Migration, MigrationError, migrate, registry
from yt_playlist.core.migrations import v069_prepared_recording_inputs as migration
from yt_playlist.repos.prepared_content import PreparedContentRepo


UPGRADE = Migration(69, 'typed_prepared_recording_inputs', migration.apply)


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    conn = sqlite3.connect(tmp_path / 'inputs.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    _pristine_installed_schema(68).backup(conn)
    with conn:
        conn.executemany('INSERT INTO recordings(id,identity_key) VALUES (?,?)', [(1, 'one'), (2, 'two')])
        conn.execute('UPDATE rec_preparation SET content_changes=25 WHERE id=1')
    yield conn
    conn.close()


def insert(conn, key, payload, revision=25):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with conn:
        conn.execute('INSERT INTO rec_prepared_content VALUES (?,?,?,?,?)', (key, raw, digest, 123.5, revision))
    return digest


def upgrade(conn):
    migrate(conn, registry=(*registry.MIGRATIONS[:68], UPGRADE))


def features(audio=None):
    return {'genre': '', 'year': None, 'audio': {} if audio is None else audio}


def test_all_scoring_fields_masks_nulls_empty_values_and_retractions_round_trip(predecessor):
    conn = predecessor
    original = {'library': features({'bpm': 0.0, 'energy': .3, 'music_key': '', 'mood_happy': None}),
                'discovered': {'genre': 'Rock', 'year': '2024', 'audio': {'music_scale': 'major'}}}
    digest = insert(conn, 'one', original)
    empty = {'library': None, 'discovered': None}
    empty_digest = insert(conn, 'missing', empty, 24)
    unknown = {'library': None, 'discovered': features({'loudness': -7.5})}
    unknown_digest = insert(conn, 'unresolved', unknown, 23)
    upgrade(conn)
    migration.validate(conn)
    row = conn.execute('SELECT * FROM prepared_recording_inputs WHERE recording_id=1').fetchone()
    assert prepared_inputs.payload(row) == original
    assert tuple(row[field] for field in ('digest', 'input_revision', 'built_at')) == (digest, 25, 123.5)
    for key, payload, reason, expected_digest in (
            ('missing', empty, 'empty_retraction', empty_digest),
            ('unresolved', unknown, 'missing_owner', unknown_digest)):
        row = conn.execute('SELECT * FROM unresolved_preparation_inputs WHERE raw_key=?', (key,)).fetchone()
        assert prepared_inputs.payload(row) == payload
        assert row['reason'] == reason and row['digest'] == expected_digest
    assert conn.execute('SELECT content_changes FROM rec_preparation').fetchone()[0] == 25
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    before = conn.total_changes
    upgrade(conn)
    assert conn.total_changes == before


@pytest.mark.parametrize('invalid', ['digest', 'unknown_field', 'nonfinite', 'wrong_shape'])
def test_unknown_or_corrupt_originals_are_diagnosed_without_removal(predecessor, invalid):
    conn = predecessor
    insert(conn, 'one', {'library': features(), 'discovered': None})
    with conn:
        if invalid == 'digest':
            conn.execute("UPDATE rec_prepared_content SET digest='bad'")
        else:
            payload = ({'library': features({'new_unrecognized_field': 1}), 'discovered': None}
                       if invalid == 'unknown_field' else
                       [] if invalid == 'wrong_shape' else
                       {'library': features({'energy': float('inf')}), 'discovered': None})
            raw = json.dumps(payload)
            conn.execute('UPDATE rec_prepared_content SET payload=?,digest=?',
                         (raw, hashlib.sha256(raw.encode()).hexdigest()))
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match='Invalid prepared input'):
        upgrade(conn)
    assert list(conn.iterdump()) == before


def test_predecessor_rejects_malformed_payload_update_without_removal(predecessor):
    conn = predecessor
    insert(conn, 'one', {'library': features(), 'discovered': None})
    before = list(conn.iterdump())
    raw = 'original malformed document'
    # The predecessor's JSON-dependent partial index rejects this before v69.
    # Wrong-shaped valid JSON above remains representable and reaches preflight.
    with pytest.raises(sqlite3.OperationalError, match='malformed JSON'), conn:
        conn.execute('UPDATE rec_prepared_content SET payload=?,digest=?',
                     (raw, hashlib.sha256(raw.encode()).hexdigest()))
    assert list(conn.iterdump()) == before


def test_database_rejects_missing_null_owners_and_inconsistent_input_masks(predecessor):
    conn = predecessor
    upgrade(conn)
    columns = ','.join(prepared_inputs.COLUMNS)
    values = (*prepared_inputs.flatten({'library': features({'bpm': 1}), 'discovered': None}), 1, 'a' * 64, 1)
    sql = f'INSERT INTO prepared_recording_inputs(recording_id,{columns}) VALUES (' + ','.join('?' for _ in range(36)) + ')'
    assert len(values) == 35
    for subject in (None, 99):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(sql, (subject, *values))
    with pytest.raises(sqlite3.IntegrityError), conn:
        wrong = list(values)
        wrong[3] = 0
        conn.execute(sql, (1, *wrong))


def test_new_admission_replaces_empty_raw_retraction_without_reinterpreting_other_evidence(predecessor):
    conn = predecessor
    insert(conn, 'later', {'library': None, 'discovered': None})
    upgrade(conn)
    with conn:
        conn.execute("INSERT INTO recordings(id,identity_key) VALUES (3,'later')")
        prepared_inputs.upsert(conn, 'later', {'library': features(), 'discovered': None}, 'b' * 64, 124, 26)
    assert not conn.execute("SELECT 1 FROM unresolved_preparation_inputs WHERE raw_key='later'").fetchone()
    assert prepared_inputs.payload(conn.execute('SELECT * FROM prepared_recording_inputs').fetchone()) == {
        'library': features(), 'discovered': None}


def test_upgrade_preserves_group_serial_and_later_input_changes_invalidate_groups(predecessor):
    conn = predecessor
    inputs = {'library': features(), 'discovered': None}
    digest = insert(conn, 'one', inputs)
    serial = conn.execute('SELECT serial FROM rec_group_clock').fetchone()[0]
    upgrade(conn)
    assert conn.execute('SELECT serial FROM rec_group_clock').fetchone()[0] == serial
    with conn:
        prepared_inputs.upsert(conn, 'two', inputs, digest, 124, 26)
        assert conn.execute('SELECT serial FROM rec_group_clock').fetchone()[0] == serial + 1
        prepared_inputs.upsert(conn, 'two', {'library': None, 'discovered': None}, 'b' * 64, 125, 27)
        assert conn.execute('SELECT serial FROM rec_group_clock').fetchone()[0] == serial + 2
        conn.execute('DELETE FROM prepared_recording_inputs WHERE recording_id=2')
    assert conn.execute('SELECT serial FROM rec_group_clock').fetchone()[0] == serial + 3


def test_retirement_invalidates_inputs_without_treating_the_survivor_as_their_owner(predecessor):
    conn = predecessor
    inputs = {'library': features({'energy': .7}), 'discovered': None}
    digest = insert(conn, 'one', inputs)
    upgrade(conn)
    original = tuple(conn.execute('SELECT * FROM prepared_recording_inputs').fetchone())
    serial = conn.execute('SELECT serial FROM rec_group_clock').fetchone()[0]
    with conn:
        conn.execute("INSERT INTO recording_redirects VALUES (1,2,'fixture','exact correction')")
    assert not conn.execute('SELECT 1 FROM prepared_recording_inputs').fetchone()
    assert conn.execute('SELECT serial FROM rec_group_clock').fetchone()[0] == serial + 1
    with pytest.raises(sqlite3.IntegrityError, match='owner is retired'), conn:
        conn.execute('INSERT INTO prepared_recording_inputs VALUES ('
                     + ','.join('?' for _ in original) + ')', original)
    with conn:
        prepared_inputs.upsert(conn, 'one', inputs, digest, 124, 26)
    row = conn.execute('SELECT * FROM unresolved_preparation_inputs').fetchone()
    assert row['raw_key'] == 'one' and row['reason'] == 'retired_owner'
    assert prepared_inputs.payload(row) == inputs
    assert not conn.execute('SELECT 1 FROM prepared_recording_inputs').fetchone()
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_corpus_byte_budget_is_checked_before_fetching_audio_values(predecessor):
    conn = predecessor
    inputs = {'library': features({'energy': .7}), 'discovered': None}
    insert(conn, 'one', inputs)
    upgrade(conn)
    repo = PreparedContentRepo(SimpleNamespace(conn=conn, _lock=threading.RLock()))

    def forbid_audio_values(operation, table, column, _database, _source):
        if operation == sqlite3.SQLITE_READ and table == 'prepared_recording_inputs' and column == 'library_energy':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(forbid_audio_values)
    try:
        assert repo.corpus(10, max_bytes=1) is None
        # Prove that the guard would reject the data-fetch query itself.
        with pytest.raises(sqlite3.DatabaseError, match='prohibited'):
            repo.corpus(10, max_bytes=1024 * 1024)
    finally:
        conn.set_authorizer(None)
    assert repo.corpus(10, max_bytes=1024 * 1024)[0][1] == inputs
