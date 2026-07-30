"""The sync recorded YouTube's lingering recently-played window as fresh plays every day, because any
row whose shelf title it could not parse ("This week", "Earlier", or any localized label) fell back to
the sync day. Those rows are shaped exactly like real ones.

Only a Takeout import can adjudicate. Takeout is ACCOUNT-WIDE: phone, TV, other browsers. Inside the
span it covers, its silence about a (track, day) proves the play did not happen. The browser extension
is not account-wide, so its silence proves nothing and grants no authority to delete.
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


def _hist(store, ident, day, keys):
    store.history.add_history_snapshot(ident, day * DAY + 43200, keys)


def _ev(store, ident, key, day, source="takeout", sec=100):
    store.conn.execute(
        "INSERT INTO play_events(identity_id,identity_key,video_id,played_at,source) VALUES (?,?,?,?,?)",
        (ident, key, "v", day * DAY + sec, source))
    store.conn.commit()


def _keys(store):
    return sorted(r[0] for r in store.conn.execute("SELECT identity_key FROM history_items"))


# --- the Takeout-authoritative window -------------------------------------------------------------

def test_takeout_window_excludes_the_truncated_export_day(store):
    _ev(store, 1, "a", 10)
    _ev(store, 1, "b", 20)          # the export ran on day 20; that day is partial
    assert repair.takeout_window(store.conn, 1) == (10, 19)


def test_takeout_window_is_none_without_a_takeout_import(store):
    _ev(store, 1, "a", 10, source="live")
    assert repair.takeout_window(store.conn, 1) is None


def test_takeout_window_is_none_for_a_single_day_export(store):
    _ev(store, 1, "a", 10)
    assert repair.takeout_window(store.conn, 1) is None      # lo=10, hi=9: nothing authoritative


# --- the purge ------------------------------------------------------------------------------------

def test_purges_a_row_takeout_contradicts(store):
    _ev(store, 1, "real", 10)
    _ev(store, 1, "other", 20)                     # widen the window: authoritative days 10..19
    _hist(store, 1, 10, ["real"])                  # corroborated
    _hist(store, 1, 15, ["phantom"])               # 'phantom' has no play event anywhere
    assert repair.purge_phantom_history(store.conn) == 1
    assert _keys(store) == ["real"]


def test_keeps_a_row_corroborated_on_the_same_day(store):
    _ev(store, 1, "a", 10)
    _ev(store, 1, "z", 20)
    _hist(store, 1, 10, ["a"])
    assert repair.purge_phantom_history(store.conn) == 0
    assert "a" in _keys(store)


def test_keeps_a_row_corroborated_one_day_off(store):
    """YouTube's "Today" shelf is local-time; play_events are UTC. An evening play lands a day off.
    repos/history.recent_plays_with_ts already widens by one day for exactly this reason."""
    _ev(store, 1, "a", 11)
    _ev(store, 1, "z", 20)
    _hist(store, 1, 10, ["a"])                     # history says day 10, ledger says day 11
    assert repair.purge_phantom_history(store.conn) == 0
    assert "a" in _keys(store)


def test_purges_a_row_two_days_off(store):
    """Beyond the one-day timezone tolerance, an uncorroborated row is a lingering-window phantom."""
    _ev(store, 1, "seed", 5)                       # authoritative days 5..19
    _ev(store, 1, "a", 12)
    _ev(store, 1, "z", 20)
    _hist(store, 1, 10, ["a"])                     # two days from the only real play of "a"
    assert repair.purge_phantom_history(store.conn) == 1
    assert "a" not in _keys(store)


def test_never_purges_after_the_takeout_window(store):
    """Past the export, the extension is the only ledger and it never sees a phone play."""
    _ev(store, 1, "a", 10)
    _ev(store, 1, "b", 20)                         # window is 10..19
    _hist(store, 1, 25, ["played_on_my_phone"])    # after the window
    assert repair.purge_phantom_history(store.conn) == 0
    assert "played_on_my_phone" in _keys(store)


def test_never_purges_before_the_takeout_window(store):
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
    _ev(store, 1, "a", 10)
    _ev(store, 1, "b", 20)
    _hist(store, 2, 15, ["brand_only"])
    assert repair.purge_phantom_history(store.conn) == 0
    assert "brand_only" in _keys(store)


def test_a_live_event_also_corroborates(store):
    """Takeout grants the authority to delete; any play event can rescue a row."""
    _ev(store, 1, "a", 10)
    _ev(store, 1, "z", 20)
    _ev(store, 1, "browser_play", 15, source="live")
    _hist(store, 1, 15, ["browser_play"])
    assert repair.purge_phantom_history(store.conn) == 0


def test_purge_is_idempotent(store):
    _ev(store, 1, "real", 10)
    _ev(store, 1, "real", 20)
    _hist(store, 1, 15, ["phantom"])
    assert repair.purge_phantom_history(store.conn) == 1
    assert repair.purge_phantom_history(store.conn) == 0


def test_a_clean_takeout_era_is_left_alone(store):
    """Takeout-derived history is exactly reconstructible from the ledger: nothing to delete."""
    _ev(store, 1, "a", 10)
    _ev(store, 1, "b", 15)
    _ev(store, 1, "c", 20)
    _hist(store, 1, 10, ["a"])
    _hist(store, 1, 15, ["b"])
    assert repair.purge_phantom_history(store.conn) == 0


def test_the_lingering_window_pattern_is_removed(store):
    """One real play on day 10, re-stamped by the sync on days 11..16."""
    _ev(store, 1, "hum", 10)
    _ev(store, 1, "other", 20)
    for d in range(10, 17):
        _hist(store, 1, d, ["hum"])
    assert repair.purge_phantom_history(store.conn) == 5     # days 12..16; day 11 is inside +/-1
    remaining = [r[0] for r in store.conn.execute(
        "SELECT CAST(hs.taken_at/86400 AS INT) FROM history_items hi "
        "JOIN history_snapshots hs ON hs.id=hi.snapshot_id")]
    assert sorted(remaining) == [10, 11]


# --- reversibility and the run-once guard ---------------------------------------------------------

def test_deleted_rows_are_backed_up(store, tmp_path):
    _ev(store, 1, "real", 10)
    _ev(store, 1, "real", 20)
    _hist(store, 1, 15, ["phantom"])
    p = tmp_path / "backup.jsonl"
    assert repair.purge_phantom_history(store.conn, backup_path=p) == 1
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    assert rows == [{"identity_id": 1, "identity_key": "phantom", "taken_at": 15 * DAY + 43200}]


def test_no_backup_file_when_nothing_is_deleted(store, tmp_path):
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
    assert store.get_setting(repair.PHANTOM_PURGE_SEEN) is None


def test_run_once_fires_after_a_takeout_import_and_then_stops(store, monkeypatch, tmp_path):
    monkeypatch.setattr("yt_playlist.core.paths.backups_dir", lambda: tmp_path)
    _ev(store, 1, "real", 10)
    _ev(store, 1, "real", 20)
    _hist(store, 1, 15, ["phantom"])
    store.set_setting("takeout_imported_at", "1000.0")
    repair.run_once(store)
    assert "phantom" not in _keys(store)
    assert store.get_setting(repair.PHANTOM_PURGE_SEEN) == "1000.0"

    _hist(store, 1, 16, ["phantom2"])                   # a fresh phantom after the repair ran
    repair.run_once(store)                              # same import: must not run again
    assert "phantom2" in _keys(store)


def test_run_once_fires_again_after_a_NEW_takeout_import(store, monkeypatch, tmp_path):
    """A second export widens the authoritative window over rows the first run had to leave alone."""
    monkeypatch.setattr("yt_playlist.core.paths.backups_dir", lambda: tmp_path)
    _ev(store, 1, "real", 10)
    _ev(store, 1, "real", 20)
    store.set_setting("takeout_imported_at", "1000.0")
    repair.run_once(store)

    _hist(store, 1, 16, ["phantom2"])
    store.set_setting("takeout_imported_at", "2000.0")  # a fresh import
    repair.run_once(store)
    assert "phantom2" not in _keys(store)
    assert store.get_setting(repair.PHANTOM_PURGE_SEEN) == "2000.0"
