"""Installed databases repair automatically, with resumable transactions and WAL."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from yt_playlist.core import db_space, policy_config, policy_upgrade
from yt_playlist.core.policy_recovery import _counts, _missing_references, _verify
from yt_playlist.core.store import Store
from yt_playlist.core.storage_maintenance import StorageMaintenance
from yt_playlist.rec import scorecard


def _legacy(path):
    store = Store(path)
    store.init_schema()
    raw = {'ranker': 'legacy', 'weights': {'decay': 1.23456789}, 'evidence': 'x' * 40000}
    with store.conn:
        store.conn.executemany('INSERT INTO rec_serving_policies VALUES (?,?,?)',
            [(f'old-{n:04}', json.dumps(raw), 1) for n in range(300)]
            + [('recent', json.dumps(raw), time.time())])
        store.conn.execute('DELETE FROM settings WHERE key=?', (policy_upgrade.KEY,))
    store.rec_ledger.record_served_impressions('kept', 'home', [{'key': 'song', 'rank': 1,
        'model_version': 'old-0000', 'provenance': {'pool_policy': 'old-0001'}}], 2)
    store.rec_proposals.put_proposals('active', {'pool_policy': 'old-0002'}, 2)
    store.settings.set_setting('session', '{"pool_policy":"old-0003"}')
    store.settings.set_setting('personal', 'keep exactly')
    return store, raw


def _state(conn):
    return json.loads(conn.execute('SELECT value FROM settings WHERE key=?', (policy_upgrade.KEY,)).fetchone()[0])


@pytest.mark.parametrize('journal', ['wal', 'delete'])
def test_installed_database_repairs_and_shrinks_on_normal_startup(tmp_path, journal):
    path = tmp_path / 'state.db'
    old, raw = _legacy(path)
    before = _counts(old.conn)
    old.conn.execute(f'PRAGMA journal_mode={journal}').fetchall()
    old.conn.close()
    size = path.stat().st_size
    store = Store(path)
    store.init_schema()
    try:
        maintenance = StorageMaintenance(store)
        maintenance.start()
        maintenance.join(30)
        assert _state(store.conn) == dict(phase='done', cursor='recent', deleted=296, shared=5)
        after = _counts(store.conn)
        after['settings'] -= 1  # Internal one-time repair marker.
        assert before == after
        assert path.stat().st_size < size / 2
        assert store.settings.get_setting('personal') == 'keep exactly'
        impression = store.conn.execute('SELECT id FROM rec_served_impressions').fetchone()[0]
        trace = scorecard.served_recommendation(store, impression)
        assert trace['configuration'] == trace['pool_configuration'] == raw
        assert store.rec_ledger.policy_configuration('old-0002') == raw
        assert store.rec_ledger.policy_configuration('old-0003') == raw
        assert store.rec_ledger.policy_configuration('recent') == raw
        assert store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert store.conn.execute('PRAGMA locking_mode').fetchone()[0] == 'normal'
        with closing(sqlite3.connect(path, timeout=0)) as other:
            other.execute("INSERT INTO settings VALUES ('other-connection','works')")
            other.commit()
        traced = []
        store.conn.set_trace_callback(traced.append)
        assert policy_upgrade.run_once(store.conn)
        assert not any('VACUUM' in sql or 'DELETE FROM rec_serving_policies' in sql for sql in traced)
    finally:
        store.conn.close()


def test_interrupted_batches_resume_without_losing_roots_or_repeating_committed_rows(tmp_path, monkeypatch):
    store, raw = _legacy(tmp_path / 'state.db')
    original = policy_upgrade._batch
    calls = 0
    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(*args)
    monkeypatch.setattr(policy_upgrade, '_batch', interrupted)
    with pytest.raises(KeyboardInterrupt):
        policy_upgrade.run_once(store.conn)
    cursor = _state(store.conn)['cursor']
    assert cursor is not None and _state(store.conn)['phase'] == 'sharing'
    assert store.rec_ledger.policy_configuration('old-0000') == raw
    assert store.conn.execute('PRAGMA locking_mode').fetchone()[0] == 'normal'
    with store.conn:
        store.conn.execute('INSERT INTO rec_serving_policies VALUES (?,?,1)', ('000-late-old-producer', json.dumps(raw)))
    monkeypatch.setattr(policy_upgrade, '_batch', original)
    assert policy_upgrade.run_once(store.conn)
    assert _state(store.conn)['deleted'] == 297
    assert _state(store.conn)['shared'] == 5
    _verify(store.conn)
    store.conn.close()


def test_batch_failure_rolls_back_progress_and_every_policy_change(tmp_path, monkeypatch):
    store, raw = _legacy(tmp_path / 'state.db')
    def failed(*args):
        raise ValueError('injected verification failure')
    monkeypatch.setattr(policy_upgrade.policy_storage, 'refresh_references', failed)
    with pytest.raises(ValueError, match='injected verification'):
        policy_upgrade.run_once(store.conn)
    assert store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0] == 301
    assert store.conn.execute('SELECT count(*) FROM rec_policy_variants').fetchone()[0] == 0
    assert store.conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0] == 0
    assert not store.conn.execute('SELECT 1 FROM settings WHERE key=?', (policy_upgrade.KEY,)).fetchone()
    assert store.rec_ledger.policy_configuration('old-0000') == raw
    store.conn.close()


def test_low_space_defers_then_automatically_retries_compaction_on_next_startup(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'state.db'
    store, raw = _legacy(path)
    size = path.stat().st_size
    original = policy_upgrade._compact
    def full(*args):
        raise db_space.LowDiskSpace('injected insufficient temporary space')
    monkeypatch.setattr(policy_upgrade, '_compact', full)
    assert policy_upgrade.run_once(store.conn) is False
    assert _state(store.conn)['phase'] == 'compacting'
    assert store.rec_ledger.policy_configuration('old-0001') == raw
    store.settings.set_setting('written-after-deferral', 'also keep')
    assert 'will retry next startup' in caplog.text
    store.conn.close()
    monkeypatch.setattr(policy_upgrade, '_compact', original)
    restarted = Store(path)
    restarted.init_schema()
    try:
        maintenance = StorageMaintenance(restarted)
        maintenance.start()
        maintenance.join(30)
        assert _state(restarted.conn)['phase'] == 'done'
        assert not maintenance.status()['warning'] and maintenance.status()['message'] == ''
        assert restarted.settings.get_setting('written-after-deferral') == 'also keep'
        assert path.stat().st_size < size / 2
        assert _missing_references(restarted.conn) == {'direct': 0, 'pool': 0}
    finally:
        restarted.conn.close()


def test_low_space_before_first_batch_preserves_entire_original(tmp_path, monkeypatch):
    store, raw = _legacy(tmp_path / 'state.db')
    monkeypatch.setattr(db_space.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1024))
    assert not policy_upgrade.run_once(store.conn)
    assert store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0] == 301
    assert store.rec_ledger.policy_configuration('old-0000') == raw
    store.settings.set_setting('core-write', 'allowed')
    _verify(store.conn)
    store.conn.close()


def test_legacy_full_json_with_existing_variant_link_is_normalized(tmp_path):
    store, raw = _legacy(tmp_path / 'state.db')
    packed, base = policy_config.pack(raw, 1, version='old-0000')
    with store.conn:
        policy_upgrade.policy_storage.publish(store.conn, [packed], [base])
    assert json.loads(store.conn.execute('SELECT configuration FROM rec_serving_policies '
        "WHERE model_version='old-0000'").fetchone()[0]) == raw
    assert policy_upgrade.run_once(store.conn)
    assert store.rec_ledger.policy_configuration('old-0000') == raw
    stored = json.loads(store.conn.execute('SELECT configuration FROM rec_serving_policies '
        "WHERE model_version='old-0000'").fetchone()[0])
    assert policy_config.BASE_FIELD in stored
    assert 'evidence' not in stored
    store.conn.close()


def test_file_with_already_collected_legacy_rows_still_compacts(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    store, _ = _legacy(path)
    original = policy_upgrade._compact
    def full(*args):
        raise db_space.LowDiskSpace('defer')
    monkeypatch.setattr(policy_upgrade, '_compact', full)
    assert not policy_upgrade.run_once(store.conn)
    assert not policy_upgrade._has_legacy(store.conn)
    size = path.stat().st_size
    with store.conn:
        store.conn.execute('DELETE FROM settings WHERE key=?', (policy_upgrade.KEY,))
    monkeypatch.setattr(policy_upgrade, '_compact', original)
    assert policy_upgrade.run_once(store.conn)
    assert _state(store.conn)['phase'] == 'done'
    assert path.stat().st_size < size / 2
    store.conn.close()


def test_native_vacuum_keeps_readers_available_and_serializes_writers(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    store, _ = _legacy(path)
    original = policy_upgrade._compact
    def check(conn, directory):
        checked = []
        def inside_vacuum():
            if checked:
                return 0
            with closing(sqlite3.connect(path, timeout=0)) as other:
                assert other.execute('SELECT count(*) FROM settings').fetchone()[0] > 0
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    other.execute('BEGIN IMMEDIATE')
            checked.append(True)
            return 0
        conn.set_progress_handler(inside_vacuum, 1000)
        original(conn, directory)
        conn.set_progress_handler(None, 0)
        assert checked
    monkeypatch.setattr(policy_upgrade, '_compact', check)
    assert policy_upgrade.run_once(store.conn)
    store.conn.close()


def test_interrupted_native_vacuum_rolls_back_and_preserves_new_writes_on_retry(tmp_path, monkeypatch):
    store, raw = _legacy(tmp_path / 'state.db')
    original = policy_upgrade._compact
    def interrupted(conn, directory):
        conn.set_progress_handler(lambda: 1, 1000)
        original(conn, directory)
    monkeypatch.setattr(policy_upgrade, '_compact', interrupted)
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        policy_upgrade.run_once(store.conn)
    assert _state(store.conn)['phase'] == 'compacting'
    _verify(store.conn)
    assert store.rec_ledger.policy_configuration('old-0000') == raw
    store.settings.set_setting('after-interrupted-vacuum', 'preserve on retry')
    monkeypatch.setattr(policy_upgrade, '_compact', original)
    assert policy_upgrade.run_once(store.conn)
    assert store.settings.get_setting('after-interrupted-vacuum') == 'preserve on retry'
    assert _state(store.conn)['phase'] == 'done'
    store.conn.close()


def test_busy_reader_cannot_accumulate_cleanup_wal_on_retry(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    store, _ = _legacy(path)
    store.conn.execute('PRAGMA busy_timeout=1')
    store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    original, batches = policy_upgrade._batch, []
    def counted(*args):
        batches.append(True)
        return original(*args)
    monkeypatch.setattr(policy_upgrade, '_batch', counted)
    try:
        with closing(sqlite3.connect(path)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM settings').fetchall()
            phases = []
            assert not policy_upgrade.run_once(store.conn, progress=phases.append)
            assert phases[-1] == 'waiting'
            assert len(batches) == 1
            state, wal_bytes = _state(store.conn), Path(str(path) + '-wal').stat().st_size
            assert not policy_upgrade.run_once(store.conn)
            assert len(batches) == 1
            assert _state(store.conn) == state
            assert Path(str(path) + '-wal').stat().st_size == wal_bytes
        assert policy_upgrade.run_once(store.conn)
        assert _state(store.conn)['phase'] == 'done'
    finally:
        store.conn.close()


def test_saved_database_automatic_upgrade(request, tmp_path):
    source = request.config.getoption('policy_recovery_source')
    if not source:
        pytest.skip('Pass --policy-recovery-source to rehearse the automatic upgrade')
    output = request.config.getoption('policy_recovery_output')
    folder = Path(output) if output else tmp_path / 'automatic-upgrade'
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = folder / 'state.db'
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as reader:
        with closing(sqlite3.connect(path)) as target:
            reader.backup(target)
            _verify(target)
            before, missing = _counts(target), _missing_references(target)
            had_repair_state = target.execute('SELECT 1 FROM settings WHERE key=?',
                                             (policy_upgrade.KEY,)).fetchone() is not None
            policies = target.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0]
    path.chmod(0o600)
    original_bytes = path.stat().st_size
    store = Store(path)
    store.init_schema()
    try:
        maintenance = StorageMaintenance(store)
        maintenance.start()
        maintenance.join(3600)
        state = _state(store.conn)
        assert state['phase'] == 'done'
        after = _counts(store.conn)
        after['settings'] -= int(not had_repair_state)
        assert before == after
        assert _missing_references(store.conn) == missing
        assert path.stat().st_size < original_bytes / 2
        _verify(store.conn)
        report = dict(original_bytes=original_bytes, compact_bytes=path.stat().st_size,
            policies_before=policies, policies_after=store.conn.execute(
                'SELECT count(*) FROM rec_serving_policies').fetchone()[0],
            repair=state, integrity_check='ok', foreign_key_check='ok',
            non_policy_counts_unchanged=True, existing_missing_impression_references=missing)
        (folder / 'automatic-report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
    finally:
        store.conn.close()
