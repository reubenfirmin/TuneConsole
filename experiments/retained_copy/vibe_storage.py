"""Isolated #692 normalization/retention audit; original samples remain retained."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from . import runtime_metadata
from yt_playlist.core import fao, vibe_history, vibe_provenance, vibe_versions
from yt_playlist.core.db_rebuild import destination, source as source_rows, verification
from yt_playlist.core.store import Store

OWNERS = ('sample_attributes', 'measurement_samples', 'measurements', 'predictions',
          'prediction_inputs', 'prediction_artists', 'recording_vibes', 'vibe_versions')


def sizes(conn):
    return dict(conn.execute('SELECT s.name,sum(d.pgsize) FROM sqlite_schema s JOIN dbstat d '
        'ON d.name=s.name WHERE s.tbl_name IN ('+','.join('?' for _ in OWNERS)+') GROUP BY s.name', OWNERS))


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(source, report):
    source = source.resolve(strict=True)
    started, now = time.monotonic(), time.time()
    original = digest(source)
    before = runtime_metadata.baseline(source, now)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        source_bytes = sizes(reader)
        # Keep the large disposable DB on the source filesystem, with lifetime
        # cleanup. Reports are written separately after the context has closed.
        with tempfile.TemporaryDirectory(prefix='692-vibe-audit-', dir=source.parents[2]) as temporary:
            path = Path(temporary)/'state.db'
            with closing(sqlite3.connect(path)) as target:
                reader.backup(target)
            store = Store(path)
            store.init_schema()
            store.conn.close()
            at = time.monotonic()
            with closing(sqlite3.connect(path)) as target:
                target.row_factory = sqlite3.Row
                target.execute('PRAGMA foreign_keys=OFF')
                guards = list(target.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger'"))
                with target:
                    for name, _ in guards:
                        target.execute('DROP TRIGGER "'+name.replace('"','""')+'"')
                    for table in reversed(vibe_versions.COLUMNS):
                        target.execute('DELETE FROM '+table)
                    vibe_versions.normalize_empty(target)
                for table in vibe_versions.COLUMNS:
                    mapping = destination._mapping(reader, table, source_rows.columns(target, table))
                    query = 'INSERT INTO '+table+'('+','.join(mapping.values())+') VALUES ('+','.join('?' for _ in mapping)+')'
                    cursor = None
                    while True:
                        rows, cursor, done = source_rows.scan(reader, table, cursor, 128)
                        with target:
                            values = [tuple(vibe_versions.intern(target,row[key]) if key == vibe_versions.COLUMNS[table]
                                            else row[key] for key in mapping) for row in rows]
                            target.executemany(query, values)
                        if done:
                            break
                    verification.retained(reader, target, table, table, mapping)
                with target:
                    for _, sql in guards:
                        target.execute(sql)
                assert not target.execute('PRAGMA foreign_key_check').fetchone()
                normalized_bytes = sizes(target)
                version_count = target.execute('SELECT count(*) FROM vibe_versions').fetchone()[0]
                prediction_count = target.execute('SELECT count(*) FROM predictions').fetchone()[0]
                original_measurements = target.execute('SELECT count(*) FROM measurements').fetchone()[0]
                future = target.execute('SELECT max(predicted_at) FROM predictions').fetchone()[0]+vibe_history.RETAIN_SECONDS+1
            normalization_seconds = time.monotonic()-at
            after = runtime_metadata.baseline(path, now)
            assert after == before, [key for key in before if before[key] != after[key]]
            store = Store(path)
            try:
                store.init_schema()
                at = time.monotonic()
                for _ in range(prediction_count//256+2):
                    vibe_history.maintain(store, future, 256)
                    cursor = vibe_provenance.directory(store.conn)/'.history-collection.json'
                    if json.loads(fao.read(cursor).bytes)['after'] == 0:
                        break
                else:
                    raise AssertionError('History traversal did not finish its bounded cycle')
                collection_seconds = time.monotonic()-at
                remaining = store.conn.execute('SELECT count(*) FROM predictions').fetchone()[0]
                assert store.conn.execute('SELECT count(*) FROM measurements').fetchone()[0] == original_measurements
                assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
                combined_bytes = sizes(store.conn)
                # Measure a cold-history cohort with its original representation
                # separately. It retains every original measurement and sample.
                retained_ids = {row[0] for row in store.conn.execute('SELECT id FROM predictions')}
                with closing(sqlite3.connect(':memory:')) as cohort:
                    for kind, sql in reader.execute("SELECT type,sql FROM sqlite_schema WHERE sql IS NOT NULL "
                            "AND tbl_name IN ('sample_attributes','measurement_samples','measurements','predictions',"
                            "'prediction_inputs','prediction_artists','recording_vibes') ORDER BY type='index'"):
                        if kind in ('table', 'index'):
                            cohort.execute(sql)
                    for table in OWNERS[:-1]:
                        rows = verification.rows(reader,table)
                        for row in rows:
                            if table == 'predictions' and row['id'] not in retained_ids or table in (
                                    'prediction_inputs','prediction_artists') and row['prediction_id'] not in retained_ids:
                                continue
                            cohort.execute('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
                    history_bytes = sizes(cohort)
                timings = {}
                for name, action in (('top_100', lambda: store.vibe_predictions.ranked('relax')),
                                     ('all_overlays', lambda: store.vibe_predictions.overlay({row[0]:{} for row in
                                         store.conn.execute('SELECT identity_key FROM recordings')}))):
                    at = time.monotonic()
                    action()
                    timings[name] = time.monotonic()-at
            finally:
                store.conn.close()
            after_collection = runtime_metadata.baseline(path, now)
            assert after_collection == before, [key for key in before if before[key] != after_collection[key]]
    assert digest(source) == original
    result = {'source':str(source), 'source_sha256_unchanged':original, 'app_input_equality':True,
              'source_objects':source_bytes, 'normalization_objects':normalized_bytes,
              'history_only_objects':history_bytes, 'combined_objects':combined_bytes,
              'bytes':{'source':sum(source_bytes.values()),'normalization':sum(normalized_bytes.values()),
                       'history_only':sum(history_bytes.values()),'combined':sum(combined_bytes.values())},
              'version_count':version_count, 'predictions_before':prediction_count,'predictions_after':remaining,
              'original_measurements_retained':original_measurements, 'normalization_s':normalization_seconds,
              'collection_s':collection_seconds,'foreground_s':timings,'total_s':time.monotonic()-started,
              'retention_clock':'controlled future, latest prediction plus 180 days',
              'scope':'Version storage measured separately from #699 contributor externalization; all original inputs retained.',
              'temporary_workspace_removed':True}
    report.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result,sort_keys=True),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('report',type=Path)
    args = parser.parse_args()
    run(args.source,args.report)
