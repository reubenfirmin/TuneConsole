"""Rehearse policy cleanup on a verified copy; never replace the source database.

Run with ``python -m yt_playlist.core.policy_recovery --help``. The output keeps
an untouched verified backup as well as the compact recovery candidate.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

from yt_playlist.core import db_space, policy_config
from yt_playlist.core.migrations import migrate
from yt_playlist.repos import policy_storage


def _progress(message):
    print(message, flush=True)


def _open(path, *, readonly=False):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) if readonly else sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _verify(conn):
    result = [row[0] for row in conn.execute('PRAGMA integrity_check')]
    if result != ['ok']:
        raise ValueError(f'Database integrity check failed: {result[:5]}')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('Database foreign-key check failed')


def _counts(conn):
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite_%'")]
    return {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for name in tables if name not in {'schema_migrations', 'rec_serving_policies',
                                              'rec_policy_bases', 'rec_policy_variants'}}


def _missing_references(conn):
    direct = conn.execute('SELECT count(*) FROM rec_served_impressions i WHERE NOT EXISTS ('
        'SELECT 1 FROM rec_serving_policies p WHERE p.model_version=i.model_version)').fetchone()[0]
    pool = conn.execute("SELECT count(*) FROM rec_served_impressions i WHERE "
        "json_extract(i.provenance,'$.pool_policy') IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM rec_serving_policies p WHERE p.model_version=CAST("
        "json_extract(i.provenance,'$.pool_policy') AS TEXT))").fetchone()[0]
    return {'direct': direct, 'pool': pool}


def _fingerprint(configuration):
    return hashlib.sha256(policy_config.canonical(configuration).encode()).hexdigest()


def _clean_copy(conn, now, progress):
    protected = policy_storage.protected_references(conn)
    candidates = [row[0] for row in conn.execute('SELECT p.model_version FROM rec_serving_policies p '
        'WHERE created_at<? AND ' + policy_storage.UNSERVED, (now - policy_storage.GRACE_SECONDS,))
        if row[0] not in protected]
    for start in range(0, len(candidates), 256):
        batch = candidates[start:start + 256]
        with conn:
            marks = ','.join('?' for _ in batch)
            conn.execute(f'DELETE FROM rec_serving_policies WHERE model_version IN ({marks})', batch)
        if start % 8192 == 0:
            progress(f'Collected {min(start + 256, len(candidates))}/{len(candidates)} unused policies')
    fingerprints, after = {}, ''
    while True:
        batch = conn.execute('SELECT * FROM rec_serving_policies WHERE model_version>? '
                             'ORDER BY model_version LIMIT 256', (after,)).fetchall()
        if not batch:
            break
        rows, bases = [], {}
        for row in batch:
            original = policy_storage.configuration(conn, row['model_version'])
            fingerprints[row['model_version']] = _fingerprint(original)
            packed, base = policy_config.pack(original, row['created_at'], version=row['model_version'])
            rows.append(packed)
            bases[base[0]] = base
        with conn:
            policy_storage.publish(conn, rows, bases.values())
            conn.executemany('UPDATE rec_serving_policies SET configuration=? WHERE model_version=?',
                             [(row[1], row[0]) for row in rows])
        after = batch[-1]['model_version']
        if len(fingerprints) % 4096 == 0:
            progress(f'Shared configuration bodies for {len(fingerprints)} retained policies')
    with conn:
        conn.execute('DELETE FROM rec_policy_bases WHERE NOT EXISTS ('
            'SELECT 1 FROM rec_policy_variants v WHERE v.base_version=rec_policy_bases.base_version)')
    return len(candidates), fingerprints


def recover(source, output_dir, *, now=None, progress=_progress):
    source, output_dir = Path(source).resolve(), Path(output_dir).resolve()
    if not source.is_file():
        raise ValueError(f'Source database does not exist: {source}')
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup, work, compact = (output_dir / name for name in ('verified-original.db', 'working.db', 'compact.db'))
    with closing(_open(source, readonly=True)) as reader:
        size = reader.execute('PRAGMA page_count').fetchone()[0] * reader.execute('PRAGMA page_size').fetchone()[0]
        db_space.require_space(output_dir, 3 * size)
        progress(f'Backing up {size} database bytes to {backup}')
        deadline = time.monotonic() + 300
        def copied(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError('Online backup exceeded five minutes; stop application writers and retry in a new directory')
        with closing(_open(backup)) as target:
            reader.backup(target, pages=512, progress=copied)
            progress('Checking integrity and foreign keys of the untouched backup')
            _verify(target)
    # The verified backup is closed and never opened for writes again.
    shutil.copyfile(backup, work)
    backup.chmod(0o600)
    work.chmod(0o600)
    with closing(_open(work)) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        # Erasure is unnecessary on this disposable private copy: VACUUM INTO
        # excludes freed pages. Avoid journaling/zeroing gigabytes of unused JSON.
        conn.execute('PRAGMA secure_delete=OFF')
        migrate(conn)
        # The migration runner validates each schema transition. Establish the
        # cleanup baseline only after upgrading: migrations can add, replace or
        # transform non-policy tables without implying that cleanup lost data.
        _verify(conn)
        counts, missing = _counts(conn), _missing_references(conn)
        before = conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0]
        deleted, expected = _clean_copy(conn, time.time() if now is None else now, progress)
        progress(f'Creating compact database at {compact}')
        conn.execute('VACUUM INTO ?', (str(compact),))
    compact.chmod(0o600)
    with closing(_open(compact, readonly=True)) as conn:
        _verify(conn)
        if _counts(conn) != counts:
            raise ValueError('Recovery changed non-policy table counts; source and verified backup are untouched')
        if _missing_references(conn) != missing:
            raise ValueError('Recovery changed impression reference resolution')
        actual = {row[0]: _fingerprint(policy_storage.configuration(conn, row[0]))
                  for row in conn.execute('SELECT model_version FROM rec_serving_policies')}
        if actual != expected:
            raise ValueError('Recovery changed retained policy configurations')
        report = {'source': str(source), 'verified_backup': str(backup), 'compact_database': str(compact),
            'original_bytes': backup.stat().st_size, 'compact_bytes': compact.stat().st_size,
            'policies_before': before, 'policies_deleted': deleted, 'policies_retained': len(actual),
            'shared_bodies': conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0],
            'non_policy_table_counts': counts, 'existing_missing_impression_references': missing,
            'integrity_check': 'ok', 'foreign_key_check': 'ok', 'retained_policy_fingerprints': 'identical',
            'source_replaced': False}
    (output_dir / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    # Only the disposable working copy is removed; leave the verified original.
    work.unlink()
    progress(json.dumps({key: value for key, value in report.items() if key != 'non_policy_table_counts'}, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path, help='Source SQLite database, opened read-only')
    parser.add_argument('--output-dir', required=True, type=Path, help='New directory on a filesystem with ample free space')
    args = parser.parse_args()
    recover(args.source, args.output_dir)


if __name__ == '__main__':
    main()
