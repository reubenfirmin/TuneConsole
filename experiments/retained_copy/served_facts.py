"""Read-only source audit and disposable actual-library #687 acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from yt_playlist.core.store import Store
from yt_playlist.core import served_facts as facts
from yt_playlist.rec import scorecard


def run(source, report):
    source = source.resolve()
    started = time.monotonic()
    with source.open('rb') as stream:
        before = hashlib.file_digest(stream, 'sha256').hexdigest()
    with tempfile.TemporaryDirectory(prefix='tuneconsole-served-') as temporary:
        path = Path(temporary) / 'state.db'
        reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        target = sqlite3.connect(path)
        try:
            reader.backup(target)
        finally:
            reader.close()
            target.close()
        store = Store(path)
        try:
            store.init_schema()
            first, last, count = store.conn.execute('SELECT min(served_at),max(served_at),count(*) '
                                                    'FROM rec_served_impressions').fetchone()
            def reports():
                at = time.monotonic()
                results = {
                    'all': scorecard.recommendation_scorecard(store, first, last + 1),
                    '30d': scorecard.recommendation_scorecard(store, last - 30 * 86400, last + 1),
                    'boundary': scorecard.recommendation_scorecard(store, last - 86400, last),
                    'experiments': scorecard.experiment_report(store, first, last + 1, last + 1),
                    'exposure': store.rec_ledger.lane_impression_counts()}
                return results, time.monotonic() - at
            baseline, baseline_s = reports()
            prep_started = time.monotonic()
            while not facts.active(store.conn):
                with store.conn:
                    facts.prepare_batch(store.conn)
            prep_s = time.monotonic() - prep_started
            prepared, prepared_s = reports()
            assert prepared == baseline
            sql = dict(store.conn.execute('SELECT name,sum(pgsize) FROM dbstat '
                "WHERE name LIKE 'served_%' OR name LIKE 'ix_served_%' OR name LIKE '%rec_served%' GROUP BY name"))
            files = list(facts.directory(store.conn).glob('*/*.json'))
            file_bytes = sum(p.stat().st_size for p in files)
            file_allocated = sum(p.stat().st_blocks * 512 for p in files)
            with store.conn:
                while facts.expire(store.conn, last + facts.RETAIN_SECONDS + 1):
                    pass
            expired, expired_s = reports()
            assert expired == baseline
            assert scorecard.served_recommendation(store, 1)['diagnostic_status'] == 'expired'
            assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
            # New writes use the compact shape and retain request/rank dedup.
            next_at = last + 10
            item = {'identity_key': 'unresolved-experiment-only', 'rank': 1,
                    'model_version': 'missing-experiment-policy',
                    'provenance': {'ranker': 'experiment', 'candidate': {'video_id': 'uncatalogued'},
                                   'diagnostic': 'x' * 16384}}
            for _ in range(2):
                store.rec_ledger.record_served_impressions('isolated-growth-check', 'experiment', [item], next_at)
            compact = store.conn.execute('SELECT id,length(provenance),recording_id,raw_identity_key '
                "FROM rec_served_impressions WHERE request_id='isolated-growth-check'").fetchall()
            assert len(compact) == 1 and compact[0][1] == 2 and compact[0][2] is None
            result = {'source': str(source), 'source_sha256': before, 'rows': count,
                      'report_equality': True, 'expired_report_equality': True,
                      'baseline_report_s': baseline_s, 'prepared_report_s': prepared_s,
                      'expired_report_s': expired_s, 'preparation_s': prep_s,
                      'sql_pages_by_owner': sql, 'diagnostic_files': len(files),
                      'diagnostic_bytes': file_bytes, 'diagnostic_allocated_bytes': file_allocated,
                      'compact_continued_write': list(compact[0]),
                      'compatibility_note': 'Historical provenance remains for #626; SQL figures include it.',
                      'report': prepared}
        finally:
            store.conn.close()
    with source.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == before
    result['source_unchanged'] = True
    result['total_s'] = time.monotonic() - started
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'report'}, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    run(args.source, args.report)
