"""Exercise current native metadata on an isolated copy of a supplied library.

uv run python tests/core/check_native_metadata_freshness_actual.py DATABASE REPORT
Only selected native documents are copied; this is a focused admission check,
not acceptance of unrelated file owners or of the complete running application.
"""
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

from yt_playlist.core import metadata_native, metadata_runtime_files as files
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.core.store import Store


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def digest_rows(conn, table):
    digest = hashlib.sha256()
    for row in conn.execute('SELECT * FROM '+table+' ORDER BY 1,2'):
        digest.update(repr(tuple(row)).encode())
    return digest.hexdigest()


def check(database, report):
    database = database.resolve()
    original_hash = digest_file(database)
    source = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    try:
        # A source's existing link must be the only one for its provider/scope
        # on this recording, so this experiment does not request reassociation.
        rows = source.execute('WITH candidates AS ('
            'SELECT s.*,l.recording_id,(SELECT min(id) FROM catalog_entries e WHERE e.recording_id=l.recording_id) AS entry_id,'
            'row_number() OVER (PARTITION BY s.provider,s.scope ORDER BY s.source_key,l.recording_id) AS ordinal '
            'FROM rec_musical_sources s JOIN rec_musical_links l ON l.source_key=s.source_key '
            'WHERE (SELECT count(*) FROM rec_musical_links other JOIN rec_musical_sources os ON os.source_key=other.source_key '
            'WHERE other.recording_id=l.recording_id AND os.provider=s.provider AND os.scope=s.scope)=1) '
            'SELECT * FROM candidates WHERE ordinal<=4 AND entry_id IS NOT NULL').fetchall()
        assert rows
        with tempfile.TemporaryDirectory(prefix='tuneconsole-706-actual-') as workspace:
            store = Store(Path(workspace)/'state.db')
            try:
                source.backup(store.conn)
                store.init_schema()
                selected = {}
                for row in rows:
                    key = row['source_key']
                    if key in selected:
                        continue
                    relative = files.relative('native', (key,), row['revision'])
                    target = files.directory(store.conn)/relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(files.root_for_database(database, None)/relative, target)
                    selected[key] = row
                tables = ('rec_musical_sources', 'rec_musical_pending', 'rec_content_pending', 'rec_musical_source_profiles')
                before = {table: digest_rows(store.conn, table) for table in tables}
                documents = {path: path.read_bytes() for path in files.directory(store.conn).rglob('*.json')}
                scopes = Counter()
                for key, row in selected.items():
                    original = metadata_native.source_values(store.conn, key)
                    observations = []
                    for field, value in original.items():
                        provenance = {name: item for name, item in value['provenance'].items() if name != 'schema'}
                        observations.append(MetadataObservation(field=field, value=value['value'], **provenance))
                    assert observations
                    def admit(values, run, at):
                        store.enrichment_state.record_metadata_observations(row['entry_id'], row['provider'],
                            values, run_id=run, now=at)
                    admit(observations, '706-first', 10)
                    admit(observations, '706-newest', 30)
                    first = observations[0]
                    changed = replace(first, value=.01 if first.value != .01 else .02)
                    admit([changed], '706-late', 20)
                    assert metadata_native.source_values(store.conn, key) == original
                    try:
                        admit([changed], '706-newest', 30)
                    except ValueError as error:
                        assert 'cannot change its result' in str(error)
                    else:
                        raise AssertionError('Changed accepted run was admitted')
                    scopes[row['provider']+':'+row['scope']] += 1
                assert before == {table: digest_rows(store.conn, table) for table in tables}
                assert documents == {path: path.read_bytes() for path in files.directory(store.conn).rglob('*.json')}
                assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
                receipt_count = store.conn.execute('SELECT count(*) FROM metadata_native_delivery_heads').fetchone()[0]
                receipt_bytes = store.conn.execute("SELECT sum(pgsize) FROM dbstat WHERE name='metadata_native_delivery_heads'").fetchone()[0]
                result = {'source': str(database), 'source_sha256': original_hash, 'selected_sources': len(selected),
                    'cohorts': dict(scopes), 'current_receipts': receipt_count, 'receipt_sql_bytes': receipt_bytes,
                    'unchanged_documents': len(documents), 'input_and_work_hashes': before,
                    'older_changes_ignored': True, 'changed_latest_runs_rejected': True,
                    'foreign_keys_valid': True, 'complete': True}
            finally:
                store.conn.close()
    finally:
        source.close()
    assert digest_file(database) == original_hash
    result['source_unchanged'] = True
    report.write_text(json.dumps(result, indent=2, sort_keys=True)+'\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
