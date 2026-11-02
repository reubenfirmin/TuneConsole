"""Measure #699 after #692's current-reference reduction, without double counting."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from yt_playlist.core import fao, vibe_provenance as owner
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.preparation import vibe_provenance as preparation
from yt_playlist.core.store import Store
from yt_playlist.rec import ppr
from .file_bodies import digest_rows, sql_bytes

TABLES = ('prediction_inputs','prediction_artists','prediction_provenance',
          'prediction_id_state','prediction_measurement_roots')


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def run(source, report):
    source = source.resolve(strict=True)
    original = checksum(source)
    result = {'source':str(source),'source_sha256':original,
              'scope':'Current-reference cohort only; #692 history/version savings excluded'}
    with tempfile.TemporaryDirectory(prefix='tuneconsole-current-provenance-') as scratch:
        database = Path(scratch)/'state.db'
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as reader:
            with closing(sqlite3.connect(database)) as writer:
                reader.backup(writer)
        store = Store(database)
        try:
            store.init_schema()
            current = digest_rows(store.conn.execute('SELECT * FROM recording_vibes ORDER BY recording_id,vibe'))
            measurements = digest_rows(store.conn.execute('SELECT * FROM measurements ORDER BY id'))
            rankings = ppr.mode_rankings(store)
            # This temporary accounting index avoids repeatedly scanning every
            # current score while forming the controlled current-only cohort.
            store.conn.execute('CREATE INDEX main.fao_accounting_current_prediction ON recording_vibes(prediction_id)')
            # Controlled accounting cohort, not an operational retention policy.
            # Bound every reservation; retain all original measurements and samples.
            while True:
                with atomic_write(store):
                    rows = store.conn.execute('SELECT id FROM predictions p WHERE NOT EXISTS('
                        'SELECT 1 FROM recording_vibes v WHERE v.prediction_id=p.id) LIMIT 256').fetchall()
                    store.conn.executemany('DELETE FROM predictions WHERE id=?',rows)
                if not rows:
                    break
            with closing(sqlite3.connect(database)) as compact:
                compact.execute('VACUUM')
            result['current_prediction_count'] = store.conn.execute('SELECT count(*) FROM predictions').fetchone()[0]
            result['baseline_sql_bytes'] = sql_bytes(store.conn,('prediction_inputs','prediction_artists'))
            ids = [row[0] for row in store.conn.execute('SELECT id FROM predictions ORDER BY id')]
            expected = {pid:owner.read(store.conn,pid) for pid in ids}
            started,batches,longest = time.monotonic(),0,0
            while True:
                at = time.monotonic()
                count = preparation.prepare(store)
                longest = max(longest,time.monotonic()-at)
                if not count:
                    break
                batches += 1
                if batches%100==0:
                    print(json.dumps({'provenance_batches':batches}),flush=True)
            result['conversion'] = {'batches':batches,'seconds':time.monotonic()-started,
                                    'longest_batch_seconds':longest}
            assert all(owner.read(store.conn,pid)==expected[pid] for pid in ids)
            assert digest_rows(store.conn.execute('SELECT * FROM recording_vibes ORDER BY recording_id,vibe')) == current
            assert digest_rows(store.conn.execute('SELECT * FROM measurements ORDER BY id')) == measurements
            assert ppr.mode_rankings(store) == rankings
            result['exact_lineage_current_scores_originals_and_rankings'] = True
            at = time.monotonic()
            for pid in ids[:32]:
                assert owner.same_inputs(store.conn,pid,expected[pid]['inputs'])
            result['reuse_32_seconds'] = time.monotonic()-at
            paths = set(owner.references(store.conn))
            infos = [fao.stat(path) for path in paths]
            result['files'] = {'count':len(infos),'logical':sum(i.logical_bytes for i in infos),
                              'compressed':sum(i.compressed_bytes for i in infos),
                              'allocated':sum(i.allocated_bytes for i in infos)}
            manifest = owner.export(store.conn,Path(scratch)/'saved')
            restored = Store(Path(scratch)/'restored.db')
            try:
                store.conn.backup(restored.conn)
                shutil.rmtree(owner.directory(store.conn))
                owner.restore(restored.conn,manifest)
                assert all(owner.read(restored.conn,pid)==expected[pid] for pid in ids)
                assert not restored.conn.execute('PRAGMA foreign_key_check').fetchone()
                result['independent_restore_exact'] = True
            finally:
                restored.conn.close()
            with closing(sqlite3.connect(database)) as compact:
                compact.execute('VACUUM')
            result['compacted_sql_bytes'] = {table:sql_bytes(store.conn,(table,)) for table in TABLES}
            result['all_necessary_sql_bytes'] = sum(result['compacted_sql_bytes'].values())
            result['net_sql_reduction'] = result['baseline_sql_bytes']-result['all_necessary_sql_bytes']
            result['net_allocated_reduction_including_files'] = result['net_sql_reduction']-result['files']['allocated']
        finally:
            store.conn.close()
    assert checksum(source)==original
    result.update(source_unchanged=True,scratch_cleaned=True)
    report.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('report',type=Path)
    args = parser.parse_args()
    run(args.source,args.report)
