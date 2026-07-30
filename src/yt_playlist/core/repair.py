"""One-time data repairs, run from Store.init_schema(). Each is guarded by a settings key so it
executes once per database, and each is written so running it twice is harmless anyway.

The only repair so far: phantom history rows.

`get_history()` returns YouTube's whole recently-played window on every sync, and each row's `played`
bucket is the SHELF TITLE ("Today", "Yesterday", "This week", "Earlier"), localized. The old
`_parse_played_date` fell back to the sync day for anything it could not parse, so every row under a
"This week" or "Earlier" shelf was recorded as played today, again, every day. That is fixed at the
source (repos/history._parse_played_date now returns None), but the rows it already wrote remain.

The rows cannot be identified by shape. They are identified by EVIDENCE, and only where the evidence
is strong enough to be proof:

  * A Takeout import is ACCOUNT-WIDE. It records plays from the phone, the TV, another browser,
    anything. Inside the span a Takeout export covers, its silence about a (track, day) is proof that
    the play did not happen.
  * The browser extension is NOT account-wide. It sees a play only when the browser is playing and the
    app is running. Its silence proves nothing, so no row may be deleted on its authority.

Hence: purge only inside the Takeout-authoritative span, and only rows Takeout does not corroborate.
Outside it, leave everything alone. A user with no Takeout import loses nothing and gains nothing.
"""
import json
import logging

CORROBORATION_TOLERANCE_DAYS = 1
# Keyed on the Takeout import, not on "have we ever run". The purge is meaningless without Takeout
# (nothing else can prove a play did not happen) and must run AGAIN after each new import, which may
# widen the authoritative window over history rows a previous run had to leave alone. Mirrors the
# takeout_imported_at watermark rec/trend_rollups._build_first_play_index already uses.
PHANTOM_PURGE_SEEN = "repair_phantom_takeout_seen"

logger = logging.getLogger(__name__)


def takeout_window(conn, identity_id):
    """(lo_day, hi_day) inclusive UTC day bounds in which Takeout is authoritative, or None.

    `lo` is Takeout's first play. `hi` is the day BEFORE its last, because the export's final day is
    truncated at the moment the export ran: plays later that day are missing from it through no fault
    of the user's listening. Days inside the span with no Takeout events at all are still covered:
    Takeout saying nothing about a day it observed means nothing was played, which is the whole point.
    """
    row = conn.execute(
        "SELECT MIN(played_at) a, MAX(played_at) b FROM play_events "
        "WHERE identity_id = ? AND source = 'takeout'", (identity_id,)).fetchone()
    if not row or row["a"] is None:
        return None
    lo, hi = int(row["a"] // 86400), int(row["b"] // 86400) - 1
    return (lo, hi) if hi >= lo else None


def _phantom_rows(conn, identity_id, lo, hi):
    """history_items rowids in [lo, hi] with no play_event for the same track within +/- 1 day.

    The tolerance is not slack, it is a correction. YouTube's "Today" shelf is in the user's LOCAL
    timezone while play_events carry absolute UTC timestamps, so an evening play lands one UTC day
    off. repos/history.recent_plays_with_ts already widens by exactly one day for exactly this reason.
    Corroboration accepts ANY play_event (a live event also proves a play happened); only the AUTHORITY
    to delete comes from Takeout.
    """
    return conn.execute(
        "SELECT hi.rowid rid, hi.identity_key k, hs.taken_at ts "
        "FROM history_items hi JOIN history_snapshots hs ON hs.id = hi.snapshot_id "
        "WHERE hs.identity_id = ? "
        "  AND CAST(hs.taken_at / 86400 AS INTEGER) BETWEEN ? AND ? "
        "  AND NOT EXISTS (SELECT 1 FROM play_events pe "
        "                  WHERE pe.identity_id = hs.identity_id AND pe.identity_key = hi.identity_key "
        "                  AND ABS(CAST(pe.played_at / 86400 AS INTEGER) "
        "                        - CAST(hs.taken_at / 86400 AS INTEGER)) <= ?)",
        (identity_id, lo, hi, CORROBORATION_TOLERANCE_DAYS)).fetchall()


def purge_phantom_history(conn, backup_path=None) -> int:
    """Delete history rows a Takeout import positively contradicts. Returns the number deleted.

    Idempotent: a second run finds nothing, because the rows are gone and the ledger is unchanged.
    Writes every deleted row to `backup_path` as JSONL first, so the operation is reversible.
    """
    deleted, backup = 0, []
    for r in conn.execute("SELECT DISTINCT identity_id i FROM play_events WHERE source = 'takeout'"):
        ident = r["i"]
        win = takeout_window(conn, ident)
        if win is None:
            continue
        lo, hi = win
        rows = _phantom_rows(conn, ident, lo, hi)
        if not rows:
            continue
        backup.extend({"identity_id": ident, "identity_key": row["k"], "taken_at": row["ts"]}
                      for row in rows)
        conn.executemany("DELETE FROM history_items WHERE rowid = ?", [(row["rid"],) for row in rows])
        deleted += len(rows)
        logger.info("repair: identity %s, Takeout-authoritative days %d..%d, purged %d phantom rows",
                    ident, lo, hi, len(rows))
    if deleted and backup_path is not None:
        with open(backup_path, "w", encoding="utf-8") as fh:
            for row in backup:
                fh.write(json.dumps(row) + "\n")
        logger.info("repair: %d deleted rows backed up to %s", deleted, backup_path)
    conn.commit()
    return deleted


def run_once(store) -> None:
    """Purge phantom history once per Takeout import. Safe to call on every startup and after an import.

    No Takeout, no purge: nothing else can prove a play did not happen, so there is nothing to act on.
    """
    ti = store.get_setting("takeout_imported_at")
    if ti is None or ti == store.get_setting(PHANTOM_PURGE_SEEN):
        return
    backup_path = None
    try:
        from yt_playlist.core import paths
        backup_path = paths.backups_dir() / f"phantom_history_purge_{ti}.jsonl"
    except Exception:  # noqa: BLE001 - a missing backups dir must not block the repair
        logger.warning("repair: could not resolve a backup path; purging without one", exc_info=True)
    n = purge_phantom_history(store.conn, backup_path)
    store.set_setting(PHANTOM_PURGE_SEEN, str(ti))
    if n:
        logger.info("repair: purged %d phantom history rows contradicted by the Takeout import; "
                    "they are recoverable from %s", n, backup_path)
