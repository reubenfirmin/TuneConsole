"""Isolated actual-library evidence for #691 / #698; removes scratch on exit."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from yt_playlist.core import artist_credits
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.preparation import runtime_history
from yt_playlist.core.store import Store
from yt_playlist.rec import ppr


def digest_rows(conn, query):
    digest = hashlib.sha256()
    for row in conn.execute(query):
        digest.update(repr(tuple(row)).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def sizes(conn):
    return {domain: sum(row[0] for row in conn.execute(
        'SELECT sum(pgsize) FROM dbstat WHERE name IN (SELECT name FROM sqlite_schema WHERE '
        'tbl_name IN (' + ','.join('?' for _ in tables) + '))', tables) if row[0] is not None)
        for domain, tables in {
            'credits': ('artist_credit_observations', 'artist_credit_replays', 'artist_credit_retention'),
            'modes': ('taste_modes', 'taste_mode_retired_payloads', 'taste_mode_retention'),
        }.items()}


def run(source, report):
    source = source.resolve()
    def checksum():
        with source.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    source_before = checksum()
    result = {'source': str(source), 'source_sha256': source_before, 'batch_rows': 128}
    def save():
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    with tempfile.TemporaryDirectory(prefix='tuneconsole-runtime-history-') as scratch:
        database = Path(scratch) / 'state.db'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(database)) as target:
                original.backup(target)
        store = Store(database)
        try:
            started = time.monotonic()
            store.init_schema()
            result['startup_s'] = time.monotonic() - started
            result['before_sql_bytes'] = sizes(store.conn)
            result['observations_before'] = store.conn.execute('SELECT count(*) FROM artist_credit_observations').fetchone()[0]
            protected = {
                'selected_artists': 'SELECT * FROM ' + artist_credits.projection_table(store.conn) + ' ORDER BY entry_id,position',
                'selections': 'SELECT * FROM entry_artist_selections ORDER BY entry_id',
                'heads': 'SELECT * FROM artist_credit_capture_heads ORDER BY entry_id,source',
                'credits': 'SELECT * FROM artist_credits ORDER BY id',
                'credit_members': 'SELECT * FROM artist_credit_members ORDER BY credit_id,position',
                'active_modes': 'SELECT * FROM taste_modes WHERE active=1 ORDER BY mode_id',
                'mode_identity': 'SELECT mode_id,label,size,active,first_seen,last_seen,space FROM taste_modes ORDER BY mode_id',
                'mode_impressions': 'SELECT * FROM rec_mode_impressions ORDER BY epoch,lane',
                'mode_picks': 'SELECT * FROM rec_mode_picks ORDER BY membership_ref_id',
            }
            before = {name: digest_rows(store.conn, query) for name, query in protected.items()}
            started = time.monotonic()
            original_rankings = ppr.mode_rankings(store)
            result['rankings_before_s'] = time.monotonic() - started
            result['ranked_modes'] = len(original_rankings)
            result['ranked_items'] = sum(len(keys) for keys in original_rankings.values())
            now = time.time()
            for domain in ('credits', 'modes'):
                started = time.monotonic()
                batches, longest = 0, 0
                while True:
                    prefix = time.monotonic()
                    more = runtime_history.step(store, domain, now, activate=True)
                    longest = max(longest, time.monotonic() - prefix)
                    if not more:
                        break
                    batches += 1
                    if batches % 1000 == 0:
                        print(json.dumps({'domain': domain, 'batches': batches}), flush=True)
                result[domain + '_preparation'] = {'batches': batches, 'seconds': time.monotonic() - started,
                                                  'max_batch_seconds': longest}
                save()
            assert all(digest_rows(store.conn, query) == before[name] for name, query in protected.items())
            result['unchanged_hashes'] = before
            assert ppr.mode_rankings(store) == original_rankings
            result['ppr_rankings_unchanged'] = True
            result['after_sql_bytes'] = sizes(store.conn)
            result['observations_after'] = store.conn.execute('SELECT count(*) FROM artist_credit_observations').fetchone()[0]
            result['replays'] = store.conn.execute('SELECT count(*) FROM artist_credit_replays').fetchone()[0]
            result['retired_mode_payloads'] = store.conn.execute('SELECT count(*) FROM taste_mode_retired_payloads').fetchone()[0]
            result['active_modes'] = len(store.modes.list_modes())
            result['next_mode_id'] = store.modes.next_mode_id()
            save()
            # Expiry simulation checks every retained delivery key, including
            # stale source heads, with no current selection/projection changes.
            future = now + 31 * 86400
            while runtime_history.step(store, 'credits', future):
                pass
            before_repeat = store.conn.execute('SELECT count(*) FROM artist_credit_observations').fetchone()[0]
            replayed = 0
            for entry, source_name, key, identity, credit in store.conn.execute(
                    'SELECT entry_id,source,delivery_key,observation_id,credit_id FROM artist_credit_replays').fetchall():
                with atomic_write(store):
                    actual, may_select = artist_credits.capture(store.conn, entry, credit, source_name, future,
                                                               delivery_key=key)
                    assert actual == identity
                    selected = store.conn.execute('SELECT observation_id FROM entry_artist_selections WHERE entry_id=?', (entry,)).fetchone()
                    assert not may_select or selected and selected[0] == identity
                replayed += 1
            assert store.conn.execute('SELECT count(*) FROM artist_credit_observations').fetchone()[0] == before_repeat
            assert digest_rows(store.conn, protected['selected_artists']) == before['selected_artists']
            assert digest_rows(store.conn, protected['selections']) == before['selections']
            assert digest_rows(store.conn, protected['heads']) == before['heads']
            result['post_expiry_replays'] = replayed
            result['post_expiry_observations'] = before_repeat
            result['post_expiry_sql_bytes'] = sizes(store.conn)
            save()
            # Real continued source processing changes neither state count nor
            # current artists when an unchanged delivery-less head is repeated.
            samples = store.conn.execute('SELECT o.entry_id,o.credit_id,o.source FROM artist_credit_capture_heads h '
                'JOIN artist_credit_observations o ON o.id=h.observation_id '
                "WHERE o.capture_kind='capture' AND o.delivery_key IS NULL LIMIT 32").fetchall()
            for _ in range(3):
                with atomic_write(store):
                    for entry, credit, source_name in samples:
                        artist_credits.capture(store.conn, entry, credit, source_name, future)
            assert store.conn.execute('SELECT count(*) FROM artist_credit_observations').fetchone()[0] == before_repeat
            result['repeated_capture_writes'] = len(samples) * 3
            assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
            # Independent SQL backup remains usable after the producer closes.
            restored = Path(scratch) / 'restored.db'
            with closing(sqlite3.connect(restored)) as target:
                store.conn.backup(target)
            store.conn.close()
            store = Store(restored)
            store.init_schema()
            assert digest_rows(store.conn, protected['active_modes']) == before['active_modes']
            assert digest_rows(store.conn, protected['selected_artists']) == before['selected_artists']
            assert store.modes.next_mode_id() == result['next_mode_id']
            result['backup_restart_unchanged'] = True
            # A plain connection has no compatibility TEMP views shadowing
            # installed table names while SQLite recreates their indexes.
            with closing(sqlite3.connect(restored)) as compactor:
                compactor.execute('VACUUM')
                result['compacted_sql_bytes'] = sizes(compactor)
            result['external_files_added'] = 0
            result['foreign_keys_ok'] = not bool(store.conn.execute('PRAGMA foreign_key_check').fetchone())
        finally:
            store.conn.close()
    assert checksum() == source_before
    result['source_unchanged'] = True
    result['complete'] = True
    save()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.report), sort_keys=True, indent=2))
