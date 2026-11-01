"""Isolated actual-library preparation and retention measurement for #688."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from yt_playlist.core import group_documents
from yt_playlist.core.preparation import mix_choices as preparation
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence
from yt_playlist.repos import compact_group_publication, group_retention


def run(source, report):
    source = source.resolve()
    def checksum():
        with source.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    before = checksum()
    result = {'source': str(source), 'source_sha256': before}
    def save():
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    with tempfile.TemporaryDirectory(prefix='tuneconsole-mix-') as temporary:
        database = Path(temporary) / 'state.db'
        reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        target = sqlite3.connect(database)
        try:
            reader.backup(target)
        finally:
            reader.close()
            target.close()
        shutil.copytree(source.with_name(source.name + '.evidence'), database.with_name(database.name + '.evidence'))
        store = Store(database)
        try:
            store.init_schema()
            # Every mutable file domain belongs to this temporary workspace.
            # Old SQL filenames may point at the native or retained read-only
            # source; copy their exact immutable packs and rebind only this DB.
            with store.conn:
                for table, keys, prefix, _kind in group_documents.OWNERS:
                    for filename, in store.conn.execute(f'SELECT DISTINCT {prefix}_filename FROM {table}').fetchall():
                        path = Path(filename)
                        copied = group_documents.directory(store.conn) / path.parent.name / path.name
                        copied.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, copied)
                        store.conn.execute(f'UPDATE {table} SET {prefix}_filename=? WHERE {prefix}_filename=?', (str(copied), filename))
            def sizes():
                return dict(store.conn.execute('SELECT name,sum(pgsize) FROM dbstat WHERE '
                    "name LIKE '%group%' OR name LIKE '%mix_choice%' OR name LIKE '%choice_context_projection%' GROUP BY name"))
            result['before_sql'] = sizes()
            ledger = store.rec_ledger.evidence()
            offers = ledger.records('opportunity')
            responses = ledger.records('response')
            now = max(row['observed_at'] for row in offers + responses) + 1
            envelope = lambda rows: [{k: row.get(k) for k in ('id','format','at','observed_at','episode_id','prediction','comparison','monitor')}
                                    | {'options': [{k:o.get(k) for k in ('id','position','score','assignment')} for o in row['options']]} for row in rows]
            original_envelope = envelope(offers)
            result['mix_menus'] = sum(o['format'] == 'mixes' for o in offers)
            result['responses'] = len(responses)
            started = time.monotonic()
            steps = 0
            while preparation.step(store, now):
                steps += 1
                if steps % 100 == 0:
                    print(json.dumps({'preparation': preparation.status(store.conn), 'steps': steps}), flush=True)
            result['preparation_s'] = time.monotonic() - started
            result['preparation_steps'] = steps
            assert envelope(ledger.records('opportunity')) == original_envelope
            assert ledger.records('response') == responses
            result['envelope_equal'] = True
            result['descriptors'] = store.conn.execute('SELECT count(*) FROM mix_choice_descriptors').fetchone()[0]
            result['genre_sets'] = store.conn.execute('SELECT count(*) FROM mix_choice_genre_sets').fetchone()[0]
            result['incomplete_options'] = store.conn.execute('SELECT count(*) FROM mix_choice_options WHERE genres_complete=0').fetchone()[0]
            result['remaining_roots'] = [list(row) for row in store.conn.execute('SELECT kind,external_key FROM group_evidence_owners')]
            result['prepared_sql'] = sizes()
            save()
            # Existing root inventory/publication fences remain authoritative.
            from yt_playlist.rec import group_root_backfill
            while group_root_backfill.maintain(store):
                pass
            # Storage experiment: drain the existing collector with larger
            # batches in this disposable process. Production's 128-row turns
            # remain unchanged and are covered by the focused lifetime tests.
            from yt_playlist.repos import group_publication
            group_publication.BATCH_ROWS = 8192
            result['experiment_collection_batch_rows'] = group_publication.BATCH_ROWS
            started = time.monotonic()
            cleanup_steps = 0
            for _ in range(150000):
                progressed = compact_group_publication.cleanup(store.taste_groups)
                if not progressed:
                    progressed = group_retention.step(store.taste_groups)
                if not progressed:
                    break
                cleanup_steps += 1
                if cleanup_steps % 1000 == 0:
                    print(json.dumps({'cleanup_steps': cleanup_steps,
                        'generations': store.conn.execute('SELECT count(*) FROM group_generations').fetchone()[0],
                        'versions': store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0]}), flush=True)
            else:
                raise AssertionError('Retention did not converge within admitted work')
            result['cleanup_s'] = time.monotonic() - started
            result['cleanup_steps'] = cleanup_steps
            result['after_sql'] = sizes()
            result['generations'] = store.conn.execute('SELECT count(*) FROM group_generations').fetchone()[0]
            result['versions'] = store.conn.execute('SELECT count(*) FROM group_versions').fetchone()[0]
            result['current_groups'] = len(store.taste_groups.head()['groups'])
            result['current_members'] = sum(len(g['members']) for g in store.taste_groups.head(members=True)['groups'])
            save()
            # Export/restore repacks the retained ranges without copying dead
            # ranges sharing an old immutable pack. All paths belong to temp.
            store.conn.execute('BEGIN')
            try:
                manifest = group_documents.export(store.conn, Path(temporary) / 'retained-groups')
            finally:
                store.conn.rollback()
            group_documents.restore(store.conn, manifest)
            group_documents.collect_orphans(store.conn)
            files = list(group_documents.directory(store.conn).glob('*/*.json'))
            result['group_files'] = {'count': len(files), 'bytes': sum(p.stat().st_size for p in files),
                                    'allocated_bytes': sum(p.stat().st_blocks * 512 for p in files)}
            result['report'] = choice_evidence.compile_report(store, now)
            assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
            result['foreign_keys'] = True
            save()
        finally:
            store.conn.close()
    assert checksum() == before
    result['source_unchanged'] = True
    save()
    print(json.dumps({k:v for k,v in result.items() if k not in ('report','before_sql','prepared_sql','after_sql')}, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    run(args.source, args.report)
