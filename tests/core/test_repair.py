"""The sync recorded YouTube's lingering recently-played window as fresh plays every day, because any
row whose shelf title it could not parse ("This week", "Earlier", or any localized label) fell back to
the sync day. Those rows are shaped exactly like real ones.

Only a separately verified, complete, account-scoped Takeout interval can adjudicate known
YouTube observations. Event timestamps and ordinary uploads never establish that authority.
"""
import json

import pytest

from yt_playlist.core import repair
from yt_playlist.core.store import Store

DAY = 86400


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    s.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'me','c.json',1)")
    s.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (2,'brand','c.json',0)")
    s.conn.commit()
    return s


def _hist(store, ident, day, keys, source="ytm_history"):
    store.history.add_history_snapshot(ident, day * DAY + 43200, keys, source=source)


def _coverage(store, lo=10, hi=19, ident=1, **overrides):
    values = dict(identity_id=ident, fingerprint='verified-fixture', imported_at=1,
                  unparsed_dates=0, account_verified=1, complete=1,
                  coverage_start=lo * DAY, coverage_end=(hi + 1) * DAY)
    values.update(overrides)
    cur = store.conn.execute(
        'INSERT INTO takeout_imports(' + ','.join(values) + ') VALUES (' +
        ','.join('?' for _ in values) + ')', list(values.values()))
    store.conn.commit()
    return cur.lastrowid


def _ev(store, ident, key, day, source="takeout", sec=100):
    store.conn.execute(
        "INSERT INTO play_events(identity_id,identity_key,video_id,played_at,source) VALUES (?,?,?,?,?)",
        (ident, key, "v", day * DAY + sec, source))
    store.conn.commit()


def _keys(store):
    return sorted(r[0] for r in store.conn.execute("SELECT identity_key FROM history_items"))


# --- the Takeout-authoritative window -------------------------------------------------------------

def test_takeout_window_excludes_the_truncated_export_day(store):
    _coverage(store, coverage_start=10 * DAY + 100, coverage_end=20 * DAY + 100)
    assert repair.takeout_windows(store.conn, 1) == [(11, 19)]


def test_takeout_window_is_none_without_a_takeout_import(store):
    _ev(store, 1, "a", 10)
    _ev(store, 1, "b", 20)
    assert repair.takeout_windows(store.conn, 1) == []


def test_takeout_window_is_none_for_a_single_day_export(store):
    _coverage(store, coverage_start=10 * DAY + 100, coverage_end=10 * DAY + 200)
    assert repair.takeout_windows(store.conn, 1) == []


# --- the purge ------------------------------------------------------------------------------------

def test_purges_a_row_takeout_contradicts(store, tmp_path):
    _coverage(store)
    _ev(store, 1, "real", 10)
    _ev(store, 1, "other", 20)                     # widen the window: authoritative days 10..19
    _hist(store, 1, 10, ["real"])                  # corroborated
    _hist(store, 1, 15, ["phantom"])               # 'phantom' has no play event anywhere
    assert repair.purge_phantom_history(store.conn, tmp_path / 'backup.jsonl') == 1
    assert _keys(store) == ["real"]


def test_keeps_a_row_corroborated_on_the_same_day(store):
    _coverage(store)
    _ev(store, 1, "a", 10)
    _ev(store, 1, "z", 20)
    _hist(store, 1, 10, ["a"])
    assert repair.purge_phantom_history(store.conn) == 0
    assert "a" in _keys(store)


def test_keeps_a_row_corroborated_one_day_off(store):
    """YouTube's "Today" shelf is local-time; play_events are UTC. An evening play lands a day off.
    repos/history.recent_plays_with_ts already widens by one day for exactly this reason."""
    _coverage(store)
    _ev(store, 1, "a", 11)
    _ev(store, 1, "z", 20)
    _hist(store, 1, 10, ["a"])                     # history says day 10, ledger says day 11
    assert repair.purge_phantom_history(store.conn) == 0
    assert "a" in _keys(store)


def test_purges_a_row_two_days_off(store, tmp_path):
    """Beyond the one-day timezone tolerance, an uncorroborated row is a lingering-window phantom."""
    _coverage(store, 5, 19)
    _ev(store, 1, "seed", 5)
    _ev(store, 1, "a", 12)
    _ev(store, 1, "z", 20)
    _hist(store, 1, 10, ["a"])                     # two days from the only real play of "a"
    assert repair.purge_phantom_history(store.conn, tmp_path / 'backup.jsonl') == 1
    assert "a" not in _keys(store)


def test_never_purges_after_the_takeout_window(store):
    """Past the export, the extension is the only ledger and it never sees a phone play."""
    _coverage(store)
    _ev(store, 1, "a", 10)
    _ev(store, 1, "b", 20)                         # window is 10..19
    _hist(store, 1, 25, ["played_on_my_phone"])    # after the window
    assert repair.purge_phantom_history(store.conn) == 0
    assert "played_on_my_phone" in _keys(store)


def test_never_purges_before_the_takeout_window(store):
    _coverage(store)
    _ev(store, 1, "a", 10)
    _ev(store, 1, "b", 20)
    _hist(store, 1, 5, ["ancient"])
    assert repair.purge_phantom_history(store.conn) == 0
    assert "ancient" in _keys(store)


def test_the_extension_alone_grants_no_authority_to_delete(store):
    """A user who never imported Takeout must never lose a row, however much the extension missed."""
    _ev(store, 1, "seen", 10, source="live")
    _ev(store, 1, "seen", 20, source="live")
    _hist(store, 1, 15, ["played_on_my_phone"])
    assert repair.purge_phantom_history(store.conn) == 0
    assert "played_on_my_phone" in _keys(store)


def test_an_identity_without_takeout_is_untouched(store):
    """identity 2 is a brand account: no extension, no Takeout. Its rows are all the evidence there is."""
    _coverage(store)
    _ev(store, 1, "a", 10)
    _ev(store, 1, "b", 20)
    _hist(store, 2, 15, ["brand_only"])
    assert repair.purge_phantom_history(store.conn) == 0
    assert "brand_only" in _keys(store)


def test_a_live_event_also_corroborates(store):
    """Takeout grants the authority to delete; any play event can rescue a row."""
    _coverage(store)
    _ev(store, 1, "a", 10)
    _ev(store, 1, "z", 20)
    _ev(store, 1, "browser_play", 15, source="live")
    _hist(store, 1, 15, ["browser_play"])
    assert repair.purge_phantom_history(store.conn) == 0


def test_purge_is_idempotent(store, tmp_path):
    _coverage(store)
    _ev(store, 1, "real", 10)
    _ev(store, 1, "real", 20)
    _hist(store, 1, 15, ["phantom"])
    assert repair.purge_phantom_history(store.conn, tmp_path / 'backup.jsonl') == 1
    assert repair.purge_phantom_history(store.conn) == 0


def test_a_clean_takeout_era_is_left_alone(store):
    """Takeout-derived history is exactly reconstructible from the ledger: nothing to delete."""
    _coverage(store)
    _ev(store, 1, "a", 10)
    _ev(store, 1, "b", 15)
    _ev(store, 1, "c", 20)
    _hist(store, 1, 10, ["a"])
    _hist(store, 1, 15, ["b"])
    assert repair.purge_phantom_history(store.conn) == 0


def test_the_lingering_window_pattern_is_removed(store, tmp_path):
    """One real play on day 10, re-stamped by the sync on days 11..16."""
    _coverage(store)
    _ev(store, 1, "hum", 10)
    _ev(store, 1, "other", 20)
    for d in range(10, 17):
        _hist(store, 1, d, ["hum"])
    assert repair.purge_phantom_history(store.conn, tmp_path / 'backup.jsonl') == 5
    remaining = [r[0] for r in store.conn.execute(
        "SELECT CAST(hs.taken_at/86400 AS INT) FROM history_items hi "
        "JOIN history_snapshots hs ON hs.id=hi.snapshot_id")]
    assert sorted(remaining) == [10, 11]


# --- reversibility and the run-once guard ---------------------------------------------------------

def test_deleted_rows_are_backed_up(store, tmp_path):
    _coverage(store)
    _ev(store, 1, "real", 10)
    _ev(store, 1, "real", 20)
    _hist(store, 1, 15, ["phantom"])
    p = tmp_path / "backup.jsonl"
    assert repair.purge_phantom_history(store.conn, backup_path=p) == 1
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    row, = rows
    assert row['identity_id'] == 1 and row['identity_key'] == 'phantom'
    assert row['taken_at'] == 15 * DAY + 43200 and row['source'] == 'ytm_history'
    assert row['snapshot_id'] and row['rid'] and 'raw_identity_key' in row


def test_no_backup_file_when_nothing_is_deleted(store, tmp_path):
    _coverage(store)
    _ev(store, 1, "a", 10)
    _ev(store, 1, "z", 20)
    p = tmp_path / "backup.jsonl"
    assert repair.purge_phantom_history(store.conn, backup_path=p) == 0
    assert not p.exists()


def test_run_once_does_nothing_without_a_takeout_import(store, monkeypatch, tmp_path):
    """No Takeout, no authority. The purge must never fire for an extension-only user."""
    monkeypatch.setattr("yt_playlist.core.paths.backups_dir", lambda: tmp_path)
    _ev(store, 1, "real", 10)
    _ev(store, 1, "real", 20)
    _hist(store, 1, 15, ["phantom"])
    repair.run_once(store)                              # takeout_imported_at is unset
    assert "phantom" in _keys(store)
    assert store.settings.get_setting(repair.PHANTOM_PURGE_SEEN) is None


def test_run_once_fires_after_a_takeout_import_and_then_stops(store, monkeypatch, tmp_path):
    _coverage(store)
    monkeypatch.setattr("yt_playlist.core.paths.backups_dir", lambda: tmp_path)
    _ev(store, 1, "real", 10)
    _ev(store, 1, "real", 20)
    _hist(store, 1, 15, ["phantom"])
    store.settings.set_setting("takeout_imported_at", "1000.0")
    repair.run_once(store)
    assert "phantom" not in _keys(store)
    watermark = store.settings.get_setting(repair.PHANTOM_PURGE_SEEN)
    assert watermark

    _hist(store, 1, 16, ["phantom2"])                   # a fresh phantom after the repair ran
    repair.run_once(store)                              # same import: must not run again
    assert "phantom2" in _keys(store)
    assert store.settings.get_setting(repair.PHANTOM_PURGE_SEEN) == watermark


def test_run_once_fires_again_after_a_NEW_takeout_import(store, monkeypatch, tmp_path):
    """A second export widens the authoritative window over rows the first run had to leave alone."""
    monkeypatch.setattr("yt_playlist.core.paths.backups_dir", lambda: tmp_path)
    _coverage(store)
    _ev(store, 1, "real", 10)
    _ev(store, 1, "real", 20)
    store.settings.set_setting("takeout_imported_at", "1000.0")
    repair.run_once(store)
    first_watermark = store.settings.get_setting(repair.PHANTOM_PURGE_SEEN)

    _hist(store, 1, 16, ["phantom2"])
    _coverage(store)
    store.settings.set_setting("takeout_imported_at", "2000.0")  # a fresh import
    repair.run_once(store)
    assert "phantom2" not in _keys(store)
    assert store.settings.get_setting(repair.PHANTOM_PURGE_SEEN) != first_watermark


def test_run_once_notices_an_older_import_verified_later(store, monkeypatch, tmp_path):
    older = _coverage(store, 10, 19, account_verified=0)
    _coverage(store, 40, 49)
    _hist(store, 1, 15, ['older-interval'])
    monkeypatch.setattr('yt_playlist.core.paths.backups_dir', lambda: tmp_path)
    repair.run_once(store)
    assert _keys(store) == ['older-interval']
    store.conn.execute('UPDATE takeout_imports SET account_verified=1 WHERE id=?', (older,))
    store.conn.commit()
    repair.run_once(store)
    assert _keys(store) == []


def test_disjoint_imports_never_authorize_the_gap(store, tmp_path):
    _coverage(store, 10, 19)
    _coverage(store, 40, 49)
    for day in (15, 30, 45):
        _hist(store, 1, day, [str(day)])
    assert repair.takeout_windows(store.conn, 1) == [(10, 19), (40, 49)]
    assert repair.purge_phantom_history(store.conn, tmp_path / 'backup.jsonl') == 2
    assert _keys(store) == ['30']


def test_overlapping_verified_intervals_back_up_each_observation_once(store, tmp_path):
    _coverage(store, 10, 19)
    _coverage(store, 15, 24)
    _hist(store, 1, 16, ['observed'])
    backup = tmp_path / 'backup.jsonl'
    assert repair.purge_phantom_history(store.conn, backup) == 1
    assert len(backup.read_text().splitlines()) == 1


def test_exact_events_and_coverage_are_scoped_to_their_account(store, tmp_path):
    _coverage(store, ident=1)
    _hist(store, 1, 15, ['same-song'])
    _hist(store, 2, 15, ['same-song'])
    _ev(store, 2, 'same-song', 15)
    assert repair.purge_phantom_history(store.conn, tmp_path / 'backup.jsonl') == 1
    assert [row[0] for row in store.conn.execute(
        'SELECT hs.identity_id FROM history_items hi '
        'JOIN history_snapshots hs ON hs.id=hi.snapshot_id')] == [2]


@pytest.mark.parametrize('flags', [dict(account_verified=0), dict(complete=0),
                                  dict(unparsed_dates=1), dict(coverage_start=None)])
def test_incomplete_authority_preserves_observations(store, flags):
    _coverage(store, **flags)
    _hist(store, 1, 15, ['observed'])
    assert repair.purge_phantom_history(store.conn) == 0
    assert _keys(store) == ['observed']


def test_unknown_and_independent_observations_survive_complete_coverage(store, tmp_path):
    _coverage(store)
    _hist(store, 1, 15, ['legacy'], source='unknown')
    _hist(store, 1, 15, ['spotify'], source='independent')
    _hist(store, 1, 15, ['ytm'])
    assert repair.purge_phantom_history(store.conn, tmp_path / 'backup.jsonl') == 1
    assert _keys(store) == ['legacy', 'spotify']


@pytest.mark.parametrize('writer', ['live', 'import'])
def test_independent_observation_protects_an_existing_daily_row(store, writer):
    _coverage(store)
    store.history.add_history_snapshot(1,15*DAY+43200,['song'],source='ytm_history')
    if writer == 'live':
        store.history.record_history_plays(1, 15 * DAY + 43200, ['song'], source_timezone='UTC')
    else:
        store.history.import_plays(1, [('song', 15 * DAY + 100)])
    assert repair.purge_phantom_history(store.conn) == 0
    assert _keys(store) == ['song']


def test_missing_backup_refuses_without_leaking_deletions_to_a_later_commit(store):
    _coverage(store)
    _hist(store, 1, 15, ['observed'])
    with pytest.raises(ValueError, match='recovery backup'):
        repair.purge_phantom_history(store.conn)
    store.settings.set_setting('unrelated', 'commit')
    assert _keys(store) == ['observed'] and not store.conn.in_transaction


@pytest.mark.parametrize('failure', ['open', 'write', 'partial_write', 'fsync', 'publish'])
def test_backup_failure_preserves_history_and_cleans_partial_file(store, monkeypatch, tmp_path, failure):
    _coverage(store)
    _hist(store, 1, 15, ['a', 'b'])

    def fail(*args, **kwargs):
        raise OSError('injected backup failure')

    if failure == 'open':
        monkeypatch.setattr(repair.tempfile, 'NamedTemporaryFile', fail)
    elif failure == 'write':
        from contextlib import contextmanager
        from types import SimpleNamespace
        original = repair.tempfile.NamedTemporaryFile

        @contextmanager
        def unwritable(*args, **kwargs):
            with original(*args, **kwargs) as fh:
                yield SimpleNamespace(name=fh.name, write=fail)

        monkeypatch.setattr(repair.tempfile, 'NamedTemporaryFile', unwritable)
    elif failure == 'partial_write':
        dumps = repair.json.dumps
        calls = []

        def partial(row):
            calls.append(row)
            if len(calls) == 2:
                fail()
            return dumps(row)

        monkeypatch.setattr(repair.json, 'dumps', partial)
    else:
        monkeypatch.setattr(repair.os, 'fsync' if failure == 'fsync' else 'link', fail)
    with pytest.raises(OSError, match='injected backup failure'):
        repair.purge_phantom_history(store.conn, tmp_path / 'backup.jsonl')
    store.settings.set_setting('unrelated', 'commit')
    assert _keys(store) == ['a', 'b'] and not store.conn.in_transaction
    assert list(tmp_path.iterdir()) == []


def test_existing_backup_is_never_overwritten_and_no_deletion_is_committed(store, tmp_path):
    _coverage(store)
    _hist(store, 1, 15, ['observed'])
    backup = tmp_path / 'backup.jsonl'
    backup.write_text('prior recovery evidence\n')
    with pytest.raises(FileExistsError):
        repair.purge_phantom_history(store.conn, backup)
    store.settings.set_setting('unrelated', 'commit')
    assert _keys(store) == ['observed']
    assert backup.read_text() == 'prior recovery evidence\n'
    assert list(tmp_path.iterdir()) == [backup]


def test_failed_delete_rolls_back_prior_deletions_and_leaves_complete_backup(store, tmp_path):
    import sqlite3
    _coverage(store)
    _hist(store, 1, 15, ['a', 'b'])
    store.conn.execute("CREATE TRIGGER fail_repair BEFORE DELETE ON history_items "
                       "WHEN OLD.identity_key='b' BEGIN SELECT RAISE(ABORT,'injected deletion failure'); END")
    backup = tmp_path / 'backup.jsonl'
    with pytest.raises(sqlite3.IntegrityError, match='injected deletion failure'):
        repair.purge_phantom_history(store.conn, backup)
    store.settings.set_setting('unrelated', 'commit')
    assert _keys(store) == ['a', 'b'] and not store.conn.in_transaction
    assert {json.loads(line)['identity_key'] for line in backup.read_text().splitlines()} == {'a', 'b'}


def test_run_once_backup_directory_failure_preserves_history_and_watermark(store, monkeypatch, caplog):
    _coverage(store)
    _hist(store, 1, 15, ['observed'])

    def unavailable():
        raise OSError('directory unavailable')

    monkeypatch.setattr('yt_playlist.core.paths.backups_dir', unavailable)
    repair.run_once(store)
    store.settings.set_setting('unrelated', 'commit')
    assert _keys(store) == ['observed']
    assert store.settings.get_setting(repair.PHANTOM_PURGE_SEEN) is None
    assert 'history preserved' in caplog.text and 'recoverable from' not in caplog.text


def test_purge_refuses_a_caller_transaction_without_committing_it(store, tmp_path):
    _coverage(store)
    _hist(store, 1, 15, ['observed'])
    store.conn.execute("INSERT INTO settings(key,value) VALUES ('pending','keep uncommitted')")
    with pytest.raises(ValueError, match='own transaction'):
        repair.purge_phantom_history(store.conn, tmp_path / 'backup.jsonl')
    assert store.conn.in_transaction and _keys(store) == ['observed']
    store.conn.rollback()
    assert store.settings.get_setting('pending') is None
