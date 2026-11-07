"""Actual-library policy/archive evidence, confined to disposable SQL/files."""
from tests.file_custody_support import export_owner, restore_owner
from yt_playlist.core import file_collection
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from yt_playlist.core import fao, file_map, legacy_evidence, legacy_journals, policy_history
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.preparation import legacy_journals as archives, policy_history as history
from yt_playlist.core.store import Store
from yt_playlist.repos import policy_storage
from .file_bodies import digest_rows, sql_bytes


def run(source,report,domain='both'):
    source = source.resolve()
    def checksum():
        with source.open('rb') as stream:
            return hashlib.file_digest(stream,'sha256').hexdigest()
    result = {'source':str(source),'source_sha256':checksum(),'scope':domain}
    def save():
        report.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    with tempfile.TemporaryDirectory(prefix='tuneconsole-cold-files-') as scratch:
        scratch = Path(scratch)
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as reader:
            with closing(sqlite3.connect(scratch/'state.db')) as writer:
                reader.backup(writer)
        store = Store(scratch/'state.db')
        try:
            store.init_schema()
            now = time.time()
            protected_queries = {
                'current_vibes':'SELECT * FROM recording_vibes ORDER BY recording_id,vibe',
                'catalog':'SELECT * FROM catalog_entries ORDER BY id',
                'recordings':'SELECT * FROM recordings ORDER BY id',
                'playlist_scopes':'SELECT * FROM playlist_scopes ORDER BY 1',
                'impressions':'SELECT * FROM rec_served_impressions ORDER BY id',
            }
            before = {name:digest_rows(store.conn.execute(query)) for name,query in protected_queries.items()}
            archive_tables = tuple(legacy_evidence.DOMAINS.values())+('legacy_archive_heads','legacy_archive_cutovers')
            policy_tables = ('recommendation_policies','recommendation_policy_bodies','recommendation_impression_policies',
                'recommendation_policy_history','historical_impression_policies','policy_history_archive')
            result['before_sql_bytes'] = {'archives':sql_bytes(store.conn,archive_tables),'policies':sql_bytes(store.conn,policy_tables)}
            originals, classifications = {},Counter()
            for archive_domain,table in (legacy_evidence.DOMAINS.items() if domain!='policies' else ()):
                records = []
                for row in store.conn.execute(f'SELECT * FROM {table} ORDER BY kind,source_id'):
                    raw = legacy_evidence._read(archive_domain,row['filename'],row['content_digest'],row['byte_size'],row['file_offset'])[1]
                    retention,basis = legacy_journals.disposition(archive_domain,row['kind'],raw,row['reason'])
                    classifications[archive_domain+'/'+row['kind']+'/'+retention] += 1
                    records.append((row['kind'],row['source_id'],raw,row['reason'],row['archived_at'],
                        {column:row[column] for column in legacy_evidence.SUBJECTS[archive_domain]},row['resolution_status']))
                originals[archive_domain] = records
            result['archive_classification_counts'] = dict(classifications)
            result['archive_originals'] = sum(map(len,originals.values()))
            started,batches,longest = time.monotonic(),0,0
            while True:
                unit = time.monotonic()
                count = archives.prepare(store) if domain!='policies' else 0
                longest = max(longest,time.monotonic()-unit)
                if not count:
                    break
                batches += 1
                if batches%25==0:
                    print(json.dumps({'archives_batches':batches}),flush=True)
            result['archive_conversion'] = {'seconds':time.monotonic()-started,'batches':batches,'longest_batch_seconds':longest}
            def verify_archives(conn):
                for archive_domain,records in originals.items():
                    expected = {legacy_journals.key(kind,source_id):(raw,reason,archived_at,subjects,status)
                        for kind,source_id,raw,reason,archived_at,subjects,status in records}
                    after,seen = None,set()
                    while True:
                        leaf = file_map.next_leaf(legacy_journals.directory(conn)/archive_domain,
                            legacy_journals.head(conn,archive_domain),after)
                        if leaf is None:
                            break
                        after,page = leaf
                        for identity,saved in page['entries'].items():
                            assert (saved['payload'],saved['reason'],saved['archived_at'],saved['subjects'],saved['resolution_status']) == expected[identity]
                            seen.add(identity)
                    assert seen==expected.keys()
            verify_archives(store.conn)
            result['exact_archive_payloads_reasons_times_subjects'] = bool(originals)
            if originals:
                with atomic_write(store):
                    for archive_domain in originals:
                        legacy_journals.retire_verified(store.conn,archive_domain,
                            legacy_journals.head(store.conn,archive_domain),now)
                assert not store.conn.execute('SELECT 1 FROM sqlite_schema WHERE name IN ('+
                    ','.join('?' for _ in legacy_evidence.DOMAINS)+')',tuple(legacy_evidence.DOMAINS.values())).fetchone()
                store.init_schema()
                result['all_eight_verified_receipt_tables_retired'] = True
            save()
            retained = policy_storage.retained_closure(store.conn,now)
            closure = retained['recommendation_policies']
            bases = retained['recommendation_policy_bodies']
            result['policy_before_count'] = store.conn.execute('SELECT count(*) FROM recommendation_policies').fetchone()[0]
            result['required_policy_count_before_history'] = len(closure)
            result['required_base_count_before_history'] = len(bases)
            configs = {row[0]:policy_storage.configuration(store.conn,row[0]) for row in store.conn.execute(
                'SELECT version FROM recommendation_policies p WHERE created_at<? AND '+policy_storage.operational_protection(store.conn)+
                ' AND EXISTS(SELECT 1 FROM recommendation_impression_policies i WHERE i.policy_version=p.version)',
                (now-policy_storage.GRACE_SECONDS,))} if domain!='archives' else {}
            started,batches = time.monotonic(),0
            while domain!='archives' and history.prepare(store,now=now):
                batches += 1
                if batches%25==0:
                    print(json.dumps({'policy_batches':batches}),flush=True)
            result['historical_policy_conversion'] = {'seconds':time.monotonic()-started,'batches':batches,'count':len(configs)}
            assert all(policy_storage.configuration(store.conn,version)==value for version,value in configs.items())
            started,batches,deleted = time.monotonic(),0,0
            while domain!='archives':
                versions,cursor = policy_storage.retention_candidates(store.conn,now)
                with atomic_write(store):
                    stats = policy_storage.prune(store.conn,versions,cursor)
                deleted += stats['policies_deleted']
                batches += 1
                if cursor is None and stats['bodies_deleted']<policy_storage.SCAN_LIMIT:
                    break
            result['policy_cleanup'] = {'seconds':time.monotonic()-started,'batches':batches,'deleted':deleted,
                'remaining':store.conn.execute('SELECT count(*) FROM recommendation_policies').fetchone()[0]}
            if domain!='archives':
                assert {r[0] for r in store.conn.execute('SELECT version FROM recommendation_policies')} == closure-set(configs)
            assert {name:digest_rows(store.conn.execute(query)) for name,query in protected_queries.items()} == before
            result['current_catalog_scores_scopes_impressions_unchanged'] = True
            with closing(sqlite3.connect(scratch/'state.db')) as compact:
                compact.execute('VACUUM')
            result['compacted_sql_bytes'] = {'archives':sql_bytes(store.conn,archive_tables),'policies':sql_bytes(store.conn,policy_tables)}
            result['files'] = {}
            manifests = {}
            owners = {'archives':legacy_journals,'policies':policy_history}
            if domain!='both':
                owners = {domain:owners[domain]}
            for name,owner in owners.items():
                live = set(owner.references(store.conn))
                all_paths = list(owner.directory(store.conn).glob('**/*.json'))
                result['files'][name] = {}
                for category,paths in (('live',live),('including_superseded_pages',all_paths)):
                    infos = [fao.read(path).info for path in paths]
                    result['files'][name][category] = {'count':len(infos),'logical':sum(i.logical_bytes for i in infos),
                        'compressed':sum(i.compressed_bytes for i in infos),'allocated':sum(i.allocated_bytes for i in infos)}
                manifests[name] = export_owner(store.conn, owner,scratch/'saved'/name)
            restored = Store(scratch/'restored.db')
            try:
                store.conn.backup(restored.conn)
                for name,owner in owners.items():
                    shutil.rmtree(owner.directory(store.conn))
                    restore_owner(restored.conn, {'archives':'legacy-journals','policies':'policy-history'}[name], owner,manifests[name])
                restored.init_schema()
                verify_archives(restored.conn)
                assert all(policy_storage.configuration(restored.conn,version)==value for version,value in configs.items())
                assert not restored.conn.execute('PRAGMA foreign_key_check').fetchone()
                result['independent_archive_and_history_restore'] = True
            finally:
                restored.conn.close()
        finally:
            store.conn.close()
    assert checksum()==result['source_sha256']
    result['source_unchanged'] = True
    result['scratch_cleaned'] = True
    save()


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('report',type=Path)
    parser.add_argument('--domain',choices=('both','archives','policies'),default='both')
    args = parser.parse_args()
    run(args.source,args.report,args.domain)
