"""Populated capture conversion retains uncertain evidence and recovery originals."""
import json
import sqlite3

import pytest

from tests.historical_captures import legacy_capture
from yt_playlist.core import catalog, repair
from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import current
from yt_playlist.core.migrations import v033_capture_evidence as capture
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store


def _predecessor(path=':memory:'):
    store = Store(path)
    migrate(store.conn, registry=MIGRATIONS[:32])
    with store.conn:
        store.conn.executemany('INSERT INTO identities(id,label,credential_ref,is_master) VALUES (?,?,?,?)',
                               [(1, 'Main', 'main', 1), (2, 'Other', 'other', 0)])
        store.conn.execute("INSERT INTO listening_calendars VALUES (1,'America/Costa_Rica',1)")
        store.conn.execute("INSERT INTO listening_calendars VALUES (2,'UTC',0)")
        store.conn.execute(
            'INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,source_day,day,'
            'source,timezone,precision,first_observed_at,last_observed_at) '
            "VALUES (1,'raw day','day','2026-10-20','2026-10-20','independent',"
            "'America/Costa_Rica','calendar_day',1,2)")
    legacy_capture(store.conn, 1, 1792540800, ['unknown', 'unknown'], source='unknown')
    legacy_capture(store.conn, 1, 1792540801, ['ytm'], source='ytm_history')
    legacy_capture(store.conn, 2, 1792540802, ['unknown'], source='independent')
    legacy_capture(store.conn, 2, 1792540803, [])
    return store


def _registry(apply=capture.apply):
    return (*MIGRATIONS[:32], Migration(33, 'capture_evidence', apply))


def _originals(conn):
    return list(map(tuple, conn.execute(
        'SELECT hs.identity_id,hi.raw_identity_key,hi.identity_key,hi.recording_id,hi.source,'
        'hs.taken_at,hs.id,hi.rowid FROM history_items hi JOIN history_snapshots hs ON hs.id=hi.snapshot_id '
        'ORDER BY hi.rowid')))


def test_populated_conversion_preserves_originals_precision_receipts_and_projection():
    store = _predecessor()
    originals = _originals(store.conn)
    days = list(map(tuple, store.conn.execute('SELECT * FROM listening_days ORDER BY identity_id,identity_key')))
    observations = list(map(tuple, store.conn.execute('SELECT * FROM listening_observations')))
    receipts = list(map(tuple, store.conn.execute('SELECT * FROM history_snapshots ORDER BY id')))
    store.init_schema()
    capture.validate(store.conn)
    assert list(map(tuple, store.conn.execute(
        'SELECT identity_id,raw_identity_key,identity_key,recording_id,source,last_observed_at,'
        "capture_id,capture_item_id FROM listening_observations WHERE precision='legacy_unknown' "
        'ORDER BY capture_item_id'))) == originals
    assert list(map(tuple, store.conn.execute('SELECT * FROM listening_capture_receipts ORDER BY id'))) == receipts
    assert list(map(tuple, store.conn.execute('SELECT * FROM listening_days ORDER BY identity_id,identity_key'))) == days
    assert tuple(store.conn.execute('SELECT * FROM listening_observations WHERE id=1').fetchone())[:12] == observations[0]
    assert list(map(tuple, store.conn.execute(
        "SELECT DISTINCT precision,timezone FROM listening_observations WHERE capture_id IS NOT NULL"))) == [
            ('legacy_unknown', None)]
    assert {r[0] for r in store.conn.execute(
        "SELECT precision FROM listening_activity WHERE identity_key='unknown'")} == {'legacy_unknown'}
    assert not list(store.conn.execute('SELECT 1 FROM play_events'))
    assert tuple(store.conn.execute('SELECT timezone,confirmed FROM listening_calendars WHERE identity_id=1').fetchone()) == (
        'America/Costa_Rica', 1)
    for name in capture.RETIRED:
        assert store.conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is None


def test_file_upgrade_publishes_verified_predecessor_backup_and_reopens(tmp_path):
    store = _predecessor(tmp_path / 'state.db')
    originals = _originals(store.conn)
    migrate(store.conn, registry=_registry())
    store.conn.close()
    backups = list((tmp_path / 'backups').glob('*.db'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert _originals(backup) == originals
        assert backup.execute('SELECT MAX(version) FROM schema_migrations').fetchone() == (32,)
    with sqlite3.connect(tmp_path / 'state.db') as reopened:
        migrate(reopened, registry=_registry())
        capture.validate(reopened)
        assert reopened.execute('SELECT COUNT(*) FROM listening_capture_receipts').fetchone() == (4,)


def test_conversion_failure_restores_original_tables_rows_and_version(tmp_path):
    store = _predecessor(tmp_path / 'state.db')
    originals = _originals(store.conn)

    def interrupted(conn, context):
        capture.apply(conn, context)
        raise RuntimeError('injected interruption after retirement')

    with pytest.raises(MigrationError, match='injected interruption'):
        migrate(store.conn, registry=_registry(interrupted))
    assert _originals(store.conn) == originals
    assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 32
    assert store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='listening_capture_receipts'").fetchone() is None
    migrate(store.conn, registry=_registry())
    capture.validate(store.conn)


def test_first_confirmed_calendar_preserves_unknown_capture_dates_and_precision():
    from yt_playlist.core import listening

    store = _predecessor()
    with store.conn:
        store.conn.execute("UPDATE listening_calendars SET timezone='UTC',confirmed=0 WHERE identity_id=1")
    migrate(store.conn, registry=_registry())
    before = list(map(tuple, store.conn.execute(
        "SELECT * FROM listening_observations WHERE precision='legacy_unknown' ORDER BY id")))
    with store.conn:
        listening.calendar(store.conn, 1, 'America/Costa_Rica')
    assert list(map(tuple, store.conn.execute(
        "SELECT * FROM listening_observations WHERE precision='legacy_unknown' ORDER BY id"))) == before
    capture.validate(store.conn)


def test_converted_capture_removal_and_reset_preserve_other_accounts_and_exact_plays():
    store = _predecessor()
    legacy_capture(store.conn, 2, 1792540801, ['ytm'], source='ytm_history')
    store.init_schema()
    other_rows = list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_observations WHERE identity_id=2 ORDER BY id')))
    other_receipts = list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_capture_receipts WHERE identity_id=2 ORDER BY id')))
    store.history.import_listening_events(1, [('real', None, 1792540800), ('real', None, 1792544400)])
    events = list(map(tuple, store.conn.execute('SELECT * FROM play_events ORDER BY id')))
    store.history.remove_listening_source('ytm_history', 1)
    assert not store.conn.execute(
        "SELECT 1 FROM listening_observations WHERE identity_id=1 AND source='ytm_history'").fetchone()
    assert store.history.listening_day_counts(1) == {'day': 1, 'real': 1, 'unknown': 1}
    assert store.history.event_play_counts(1) == {'real': 2}
    assert list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_observations WHERE identity_id=2 ORDER BY id'))) == other_rows
    store.trends.refresh_first_play()
    store.history.reset_play_history(1)
    assert not store.conn.execute('SELECT 1 FROM listening_observations WHERE identity_id=1').fetchone()
    assert not store.conn.execute('SELECT 1 FROM listening_capture_receipts WHERE identity_id=1').fetchone()
    assert list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_observations WHERE identity_id=2 ORDER BY id'))) == other_rows
    assert list(map(tuple, store.conn.execute(
        'SELECT * FROM listening_capture_receipts WHERE identity_id=2 ORDER BY id'))) == other_receipts
    assert not store.trends.first_play_map('track')
    store.trends.refresh_first_play()
    assert store.settings.get_setting('trend_first_play_source') == 'listening_v33'
    assert store.settings.get_setting('trend_first_play_watermark') is None
    store.history.reset_play_history()
    assert not store.conn.execute('SELECT 1 FROM listening_observations').fetchone()
    assert not store.conn.execute('SELECT 1 FROM listening_capture_receipts').fetchone()
    assert list(map(tuple, store.conn.execute('SELECT * FROM play_events ORDER BY id'))) == events
    current.validate(store.conn)


def test_converted_capture_reassociation_retains_raw_evidence_and_rolls_back(monkeypatch):
    store = _predecessor()
    with store.conn:
        first = catalog.upsert(store.conn, 'old', 'Old', 'Artist', duration_s=200)
        target = catalog.upsert(store.conn, 'correct', 'Correct', 'Artist', duration_s=200)
    old_key, new_key = 'old|artist', 'correct|artist'
    receipt_id = legacy_capture(store.conn, 1, 1792540800, [old_key], source='unknown')
    store.init_schema()
    original = dict(store.conn.execute(
        'SELECT * FROM listening_observations WHERE capture_id=?', (receipt_id,)).fetchone())
    before = list(store.conn.iterdump())
    invalidate = catalog.invalidate_derived

    def interrupted(conn):
        invalidate(conn)
        raise RuntimeError('interrupted reassociation')

    monkeypatch.setattr(catalog, 'invalidate_derived', interrupted)
    recording_id = store.tracks.catalog_subject(target)['recording_id']
    with pytest.raises(RuntimeError, match='interrupted reassociation'):
        store.tracks.associate_youtube_item('old', recording_id, source='verified', evidence={'same': True}, now=1)
    assert list(store.conn.iterdump()) == before
    monkeypatch.setattr(catalog, 'invalidate_derived', invalidate)
    store.tracks.associate_youtube_item('old', recording_id, source='verified', evidence={'same': True}, now=1)
    observed = dict(store.conn.execute(
        'SELECT * FROM listening_observations WHERE capture_id=?', (receipt_id,)).fetchone())
    assert observed == {**original, 'identity_key': new_key, 'recording_id': recording_id}
    assert observed['raw_identity_key'] == old_key
    assert store.tracks.catalog_subject(first)['recording_id'] == recording_id
    current.validate(store.conn)


def test_interrupted_reset_rolls_back_observations_and_retains_receipts():
    store = _predecessor()
    store.init_schema()
    store.trends.refresh_first_play()
    with store.conn:
        store.conn.execute("CREATE TRIGGER interrupt_reset BEFORE DELETE ON listening_capture_receipts "
                           "BEGIN SELECT RAISE(ABORT,'interrupted reset'); END")
    before = list(store.conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='interrupted reset'):
        store.history.reset_play_history(1)
    assert not store.conn.in_transaction
    assert list(store.conn.iterdump()) == before


def test_migrated_repair_backup_restores_complete_capture_evidence(tmp_path):
    store = _predecessor()
    day = 1792540800 // 86400
    store.init_schema()
    with store.conn:
        store.conn.execute(
            'INSERT INTO takeout_imports(identity_id,fingerprint,imported_at,unparsed_dates,'
            'account_verified,complete,coverage_start,coverage_end) VALUES (1,?,1,0,1,1,?,?)',
            ('verified coverage', day * 86400, (day + 1) * 86400))
    original = list(map(tuple, store.conn.execute('SELECT * FROM listening_observations ORDER BY id')))
    receipts = list(map(tuple, store.conn.execute('SELECT * FROM listening_capture_receipts ORDER BY id')))
    backup_path = tmp_path / 'recovery.jsonl'
    assert repair.purge_phantom_history(store.conn, backup_path) == 1
    deleted, = [json.loads(line) for line in backup_path.read_text().splitlines()]
    assert deleted['identity_key'] == 'ytm' and deleted['source'] == 'ytm_history'
    assert deleted['captured_at'] == 1792540801
    assert deleted['precision'] == 'legacy_unknown' and deleted['timezone'] is None
    assert store.history.listening_day_counts(1) == {'day': 1, 'unknown': 1}
    assert store.history.listening_day_counts(2) == {'unknown': 1}
    # The receipt is retained even after its last observation is repaired away.
    assert list(map(tuple, store.conn.execute('SELECT * FROM listening_capture_receipts ORDER BY id'))) == receipts
    del deleted['captured_at']
    with store.conn:
        store.conn.execute('INSERT INTO listening_observations(' + ','.join(deleted) + ') VALUES ('
                           + ','.join('?' for _ in deleted) + ')', list(deleted.values()))
    assert list(map(tuple, store.conn.execute('SELECT * FROM listening_observations ORDER BY id'))) == original
    current.validate(store.conn)


@pytest.mark.parametrize('assignment', ['identity_id=2', 'first_observed_at=0', "day='2026-10-01'"])
def test_current_validation_rejects_capture_receipt_disagreement(assignment):
    store = _predecessor()
    store.init_schema()
    with store.conn:
        store.conn.execute('UPDATE listening_observations SET ' + assignment + ' WHERE capture_item_id=1')
    with pytest.raises(MigrationError, match='capture receipt'):
        current.validate(store.conn)


@pytest.mark.parametrize('precision,timezone,capture_id,item_id', [
    ('exact_timestamp', 'UTC', None, None),
    ('legacy_unknown', 'UTC', 1, 99),
    ('legacy_unknown', None, None, 99),
    ('calendar_day', None, None, None),
    ('calendar_day', 'UTC', 1, 99),
])
def test_unknown_captures_cannot_claim_exact_or_confirmed_calendar_precision(
        precision, timezone, capture_id, item_id):
    store = _predecessor()
    migrate(store.conn, registry=_registry())
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            'INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,source_day,day,source,'
            'timezone,precision,first_observed_at,last_observed_at,capture_id,capture_item_id) '
            "VALUES (1,'x','x','2026-10-20','2026-10-20','unknown',?,?,1,1,?,?)",
            (timezone, precision, capture_id, item_id))
