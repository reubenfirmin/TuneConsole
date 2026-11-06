"""Actual-library FAO body evidence; all mutations stay in disposable copies."""
from tests.file_custody_support import export_owner, restore_owner
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from yt_playlist.core import embeddings, embedding_files, fao, profile_files, vibe_provenance
from yt_playlist.core.preparation import embeddings as vector_preparation, profiles, vibe_provenance as provenance
from yt_playlist.core.store import Store
from yt_playlist.rec import ppr


def digest_rows(rows):
    result = hashlib.sha256()
    for row in rows:
        result.update(repr(tuple(row)).encode())
        result.update(b'\n')
    return result.hexdigest()


def sql_bytes(conn, tables):
    names = ','.join('?' for _ in tables)
    return conn.execute('SELECT coalesce(sum(pgsize),0) FROM dbstat WHERE name IN '
        f'(SELECT name FROM sqlite_schema WHERE tbl_name IN ({names}))', tables).fetchone()[0]


def run(source, report):
    source = source.resolve()
    def checksum():
        with source.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    result = {'source': str(source), 'source_sha256': checksum(), 'batch_rows': 128}
    def save():
        report.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    tables = {'profiles': ('rec_musical_profiles','rec_musical_source_profiles','recording_profile_files','source_profile_files'),
        'embeddings': ('recording_embeddings','artist_embeddings','recording_embedding_files','artist_embedding_files'),
        'provenance': ('prediction_inputs','prediction_artists','prediction_provenance','prediction_id_state','prediction_measurement_roots')}
    with tempfile.TemporaryDirectory(prefix='tuneconsole-fao-actual-') as scratch:
        database = Path(scratch) / 'state.db'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as reader:
            with closing(sqlite3.connect(database)) as writer:
                reader.backup(writer)
        store = Store(database)
        try:
            started = time.monotonic()
            store.init_schema()
            result['startup_seconds'] = time.monotonic()-started
            result['before_sql_bytes'] = {name: sql_bytes(store.conn, values) for name, values in tables.items()}
            baselines, keys = {}, {}
            for kind, (table,key,_,_) in profile_files.KINDS.items():
                keys[kind] = [r[0] for r in store.conn.execute(f'SELECT {key} FROM {table} ORDER BY {key}')]
                baselines[kind] = digest_rows((key, profile_files.read(store.conn,kind,key)) for key in keys[kind])
            before_vectors = {name: digest_rows(embeddings.rows(store.conn,name)) for name in embeddings.SPACES}
            result['vector_counts'] = {name: embeddings.count(store.conn,name) for name in embeddings.SPACES}
            result['profile_counts'] = {kind: len(values) for kind,values in keys.items()}
            prediction_ids = [r[0] for r in store.conn.execute('SELECT id FROM predictions ORDER BY id')]
            def lineage_hash(conn):
                return digest_rows((pid,json.dumps(vibe_provenance.read(conn,pid),sort_keys=True)) for pid in prediction_ids)
            lineage = lineage_hash(store.conn)
            score_hash = digest_rows(store.conn.execute('SELECT * FROM recording_vibes ORDER BY recording_id,vibe'))
            rankings = ppr.mode_rankings(store)
            result['mode_rankings_count'] = len(rankings)
            for name, prepare in (('profiles',profiles.prepare),('embeddings',vector_preparation.prepare),('provenance',provenance.prepare)):
                started, longest, batches = time.monotonic(), 0, 0
                while True:
                    unit = time.monotonic()
                    count = prepare(store)
                    longest = max(longest,time.monotonic()-unit)
                    if not count:
                        break
                    batches += 1
                    if batches % 100 == 0:
                        print(json.dumps({'domain':name,'batches':batches}),flush=True)
                result[name+'_conversion'] = {'seconds':time.monotonic()-started,'batches':batches,'longest_batch_seconds':longest}
                save()
            for kind, values in keys.items():
                assert digest_rows((key,profile_files.read(store.conn,kind,key)) for key in values) == baselines[kind]
            assert {name:digest_rows(embeddings.rows(store.conn,name)) for name in embeddings.SPACES} == before_vectors
            assert lineage_hash(store.conn) == lineage
            assert digest_rows(store.conn.execute('SELECT * FROM recording_vibes ORDER BY recording_id,vibe')) == score_hash
            assert ppr.mode_rankings(store) == rankings
            result['exact_profiles_vectors_lineage_scores_and_rankings'] = True
            result['lookup_seconds'] = {}
            for name in embeddings.SPACES:
                selected = [key for key,_ in embeddings.rows(store.conn,name)[:32]]
                started = time.monotonic()
                embeddings.rows(store.conn,name,keys=selected)
                result['lookup_seconds'][name] = time.monotonic()-started
            for kind in keys:
                selected = keys[kind][:32]
                started = time.monotonic()
                cache = profile_files.Cache()
                for key in selected:
                    profile_files.read(store.conn,kind,key,cache=cache)
                result['lookup_seconds']['profile_'+kind] = time.monotonic()-started
            owners = {'profiles':profile_files,'embeddings':embedding_files,'provenance':vibe_provenance}
            result['files'] = {}
            manifests = {}
            for name,owner in owners.items():
                paths = sorted(set(owner.references(store.conn)))
                infos = [fao.read(path).info for path in paths]
                result['files'][name] = {'count':len(paths),'logical':sum(i.logical_bytes for i in infos),
                    'compressed':sum(i.compressed_bytes for i in infos),'allocated':sum(i.allocated_bytes for i in infos)}
                manifests[name] = export_owner(store.conn, owner,Path(scratch)/'backup'/name)
            restored = Store(Path(scratch)/'restored.db')
            try:
                store.conn.backup(restored.conn)
                for owner in owners.values():
                    shutil.rmtree(owner.directory(store.conn))
                for name,owner in owners.items():
                    restore_owner(restored.conn, name, owner,manifests[name])
                assert {name:digest_rows(embeddings.rows(restored.conn,name)) for name in embeddings.SPACES} == before_vectors
                for kind, values in keys.items():
                    assert digest_rows((key,profile_files.read(restored.conn,kind,key)) for key in values) == baselines[kind]
                assert lineage_hash(restored.conn) == lineage
                result['independent_restore_exact'] = True
            finally:
                restored.conn.close()
            with closing(sqlite3.connect(database)) as compact:
                compact.execute('VACUUM')
                result['compacted_sql_bytes'] = {name:sql_bytes(compact,values) for name,values in tables.items()}
            save()
        finally:
            store.conn.close()
    assert checksum() == result['source_sha256']
    result['source_unchanged'] = True
    result['complete'] = True
    save()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('report',type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.source,args.report),sort_keys=True,indent=2))
