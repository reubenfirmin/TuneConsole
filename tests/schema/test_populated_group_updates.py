"""Real membership reuse, foreground writes and historical evidence backfill."""
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
import time

from tests.schema.test_populated_groups import populated, storage
from tests.schema.test_populated_candidates import predecessor_registry
from yt_playlist.core import group_documents
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations import v072_compact_groups as migration
from yt_playlist.core.store import Store
from yt_playlist.rec import group_root_backfill
from yt_playlist.core import group_reads as group_queries
from yt_playlist.repos import compact_group_publication as compact, group_publication


CONTENT = ('group_versions', 'group_members', 'unresolved_group_members',
           'group_input_manifests', 'group_recording_inputs', 'group_manifest_inputs',
           'unresolved_group_manifest_inputs', 'group_representatives')


def content_counts(conn):
    return {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in CONTENT}


def file_bytes(conn):
    return sum(path.stat().st_size for path in group_documents.directory(conn).rglob('*') if path.is_file())


def test_populated_unchanged_updates_reuse_content_and_inventory_actual_evidence(populated, monkeypatch, request):
    source, conn, output = populated
    migrate(conn, registry=predecessor_registry())
    legacy = source.execute('SELECT g.* FROM rec_group_generations g JOIN rec_group_head h '
                            'ON h.generation=g.generation').fetchone()
    prepared = migration._prepared(source, legacy)
    prepared['unknown'] = [tuple(row) for row in source.execute(
        'SELECT identity_key,reason FROM rec_group_unknown WHERE generation=?', (prepared['generation'],))]
    prepared['edges'] = [tuple(row) for row in source.execute(
        'SELECT left_key,right_key,weight,support FROM rec_group_graph WHERE generation=?',
        (prepared['generation'],))]
    prepared['lineage'] = []  # Unchanged content introduces no split or merge.
    payload = json.loads(prepared['payload'])
    payload.pop('_staging_parent', None)
    payload.pop('_staging_digest', None)
    prepared['payload'] = json.dumps(payload)
    store = Store(output / 'populated.db')
    try:
        store._bind_repositories()
        store.conn.execute('PRAGMA journal_mode=WAL')
        store._snapshot_revisions = Revisions(store.conn)
        expected_head = [prepared['generation']]
        original_write, writes, foreground = group_publication._write, [0], []

        @contextmanager
        def write(repo):
            with original_write(repo) as writer:
                yield writer
            writes[0] += 1
            if writes[0] % 32 == 1:
                started = time.monotonic()
                # A separate foreground connection must acquire the writer
                # reservation between committed preparation batches.
                with sqlite3.connect(output / 'populated.db', timeout=1) as reader:
                    assert group_queries.head_generation(reader) == expected_head[0]
                    reader.execute("INSERT INTO settings VALUES ('schema-foreground','responsive') "
                                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
                foreground.append(time.monotonic() - started)

        monkeypatch.setattr(group_publication, '_write', write)
        baseline, updates = content_counts(store.conn), []
        for serial in range(1, 4):
            value = deepcopy(prepared)
            value.update(generation=f'populated-reuse-{serial}', at=prepared['at'] + serial,
                         serial=prepared['serial'] + serial, _stage_parent=expected_head[0])
            before, files = storage(store.conn), file_bytes(store.conn)
            started = time.monotonic()
            owner = store.taste_groups.stage(value)
            assert group_queries.head_generation(store.conn) == expected_head[0]
            with store.conn:
                store.conn.execute('BEGIN IMMEDIATE')
                compact.finish(store.conn, value['generation'], owner)
            expected_head[0] = value['generation']
            assert content_counts(store.conn) == baseline
            assert store.conn.execute('SELECT count(*) FROM generation_groups x JOIN group_generations g '
                'ON g.id=x.generation_id WHERE g.external_key=?', (value['generation'],)).fetchone()[0] == len(value['groups'])
            updates.append({'seconds': time.monotonic() - started, 'before_bytes': before,
                            'after_bytes': storage(store.conn), 'file_growth_bytes': file_bytes(store.conn) - files})
        assert foreground and not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        backfill = group_root_backfill.Backfill(store)
        started, batches = time.monotonic(), 0
        while backfill.step():
            batches += 1
            assert batches < 10000, 'Historical evidence inventory did not converge'
        assert store.conn.execute('SELECT roots_complete FROM group_retention_state').fetchone()[0] == 1
        roots = {kind: count for kind, count in store.conn.execute(
            'SELECT kind,count(*) FROM group_evidence_owners GROUP BY kind')}
        if request.config.getoption('--schema-evidence-dir'):
            assert roots.get('offer', 0) and roots.get('response', 0)
        report = {'unchanged_content_counts': baseline, 'updates': updates,
                  'foreground_samples': len(foreground), 'foreground_max_seconds': max(foreground),
                  'backfill_seconds': time.monotonic() - started, 'backfill_batches': batches,
                  'evidence_source': request.config.getoption('--schema-evidence-dir'), 'roots': roots,
                  'resolved_versions': store.conn.execute('SELECT count(*) FROM group_evidence_versions').fetchone()[0],
                  'unresolved_versions': store.conn.execute('SELECT count(*) FROM unresolved_group_evidence').fetchone()[0],
                  'unresolved_contexts': store.conn.execute('SELECT count(*) FROM unresolved_group_contexts').fetchone()[0]}
        (output / 'group-updates.json').write_text(json.dumps(report, indent=2) + '\n')
    finally:
        store.conn.close()
