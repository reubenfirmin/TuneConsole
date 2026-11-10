"""Queried saved-album membership and its account publication checkpoint.

Callers own the writer transaction. Unattributed legacy membership is a separate
set whose frozen cohort includes inactive accounts until they are observed.
"""
import math


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='account_album_state'").fetchone() is not None


def read_relation(conn):
    return 'saved_album_memberships' if available(conn) else 'album_library_memberships'


def account_column(conn):
    return 'account_id' if available(conn) else 'account_id'


def _writing(conn):
    if not conn.in_transaction:
        raise RuntimeError('Album membership changes require an owner transaction')


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Album observation time must be finite epoch seconds')
    return value


def revision(conn, account_id):
    row = conn.execute('SELECT revision FROM account_album_state WHERE account_id=?', (account_id,)).fetchone()
    return row[0] if row else 0


def advance(conn, account_id=None):
    _writing(conn)
    if account_id is None:
        conn.execute('INSERT INTO account_album_state(account_id,revision) SELECT id,1 FROM accounts WHERE 1 '
                     'ON CONFLICT(account_id) DO UPDATE SET revision=revision+1')
    else:
        conn.execute('INSERT INTO account_album_state(account_id,revision) VALUES (?,1) '
                     'ON CONFLICT(account_id) DO UPDATE SET revision=revision+1', (account_id,))


def start_legacy_cohort(conn):
    _writing(conn)
    if not conn.execute('SELECT 1 FROM unattributed_album_memberships LIMIT 1').fetchone():
        conn.execute('UPDATE account_album_state SET legacy_cohort_present=0,legacy_cohort_observed_at=NULL')
        conn.execute('INSERT INTO account_album_state(account_id,legacy_cohort_present) SELECT id,1 FROM accounts WHERE 1 '
                     'ON CONFLICT(account_id) DO UPDATE SET legacy_cohort_present=1,legacy_cohort_observed_at=NULL')
    else:
        # New evidence resets observations, without adding accounts that did
        # not own the original ambiguous batch.
        conn.execute('UPDATE account_album_state SET legacy_cohort_observed_at=NULL WHERE legacy_cohort_present=1')


def save(conn, album_id, account_id=None, *, source='youtube', saved_at=None):
    _writing(conn)
    if saved_at is not None:
        _clock(saved_at)
    if account_id is None:
        conn.execute('INSERT INTO unattributed_album_memberships(album_id,source,saved_at) VALUES (?,?,?) '
                     'ON CONFLICT(album_id) DO NOTHING', (album_id, source, saved_at))
    else:
        conn.execute('INSERT INTO account_albums(account_id,album_id,source,saved_at) VALUES (?,?,?,?) '
                     'ON CONFLICT(account_id,album_id) DO NOTHING', (account_id, album_id, source, saved_at))


def remove(conn, album_id, account_id=None):
    _writing(conn)
    if account_id is None:
        conn.execute('DELETE FROM unattributed_album_memberships WHERE album_id=?', (album_id,))
    else:
        conn.execute('DELETE FROM account_albums WHERE account_id=? AND album_id=?', (account_id, album_id))


def observe_snapshot(conn, account_id, at):
    """Advance revision and both observations as one coherent checkpoint write."""
    _writing(conn)
    at = _clock(at)
    conn.execute('INSERT INTO account_album_state(account_id,revision,snapshot_observed_at) VALUES (?,1,?) '
        'ON CONFLICT(account_id) DO UPDATE SET revision=revision+1,snapshot_observed_at=excluded.snapshot_observed_at,'
        'legacy_cohort_observed_at=CASE WHEN legacy_cohort_present=1 THEN excluded.snapshot_observed_at '
        'ELSE legacy_cohort_observed_at END', (account_id, at))


def retire_observed_legacy(conn):
    _writing(conn)
    cohort = conn.execute('SELECT count(*),sum(legacy_cohort_observed_at IS NULL) FROM account_album_state '
                          'WHERE legacy_cohort_present=1').fetchone()
    if cohort[0] and not cohort[1]:
        conn.execute('DELETE FROM unattributed_album_memberships')


def merge(conn, target, retired):
    """Rebind current membership on an independently proven release merge."""
    _writing(conn)
    if target == retired:
        return
    for table, owner in (('account_albums', 'account_id'), ('unattributed_album_memberships', None)):
        rows = conn.execute(f'SELECT * FROM {table} WHERE album_id=?', (retired,)).fetchall()
        for row in rows:
            predicate = 'album_id=?' + (f' AND {owner}=?' if owner else '')
            values = (target, row[owner]) if owner else (target,)
            prior = conn.execute(f'SELECT saved_at FROM {table} WHERE {predicate}', values).fetchone()
            if prior is None:
                conn.execute(f'UPDATE {table} SET album_id=? WHERE album_id=?' +
                             (f' AND {owner}=?' if owner else ''),
                             (target, retired, row[owner]) if owner else (target, retired))
            else:
                times = [value for value in (prior['saved_at'], row['saved_at']) if value is not None]
                at = min(times) if times else None
                conn.execute(f'UPDATE {table} SET saved_at=? WHERE {predicate}', (at, *values))
                conn.execute(f'DELETE FROM {table} WHERE album_id=?' + (f' AND {owner}=?' if owner else ''),
                             (retired, row[owner]) if owner else (retired,))
    advance(conn)
