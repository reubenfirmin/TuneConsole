"""Keep published groups while they have readers or retained references."""
from pathlib import Path

import pytest

from tests.repos.test_compact_group_publication import database, finish
from yt_playlist.core import fao, group_documents, group_evidence
from yt_playlist.core.group_preparation_state import ROOTS
from yt_playlist.core.worker_settings import save
from yt_playlist.repos import group_publication


def context(generation='first', version='first-leaf'):
    return {'group_generation': generation, 'musical_groups': [{'version': version, 'group_id': 'leaf'}]}


def enable(store):
    with store.conn:
        save(store.conn, ROOTS, 1)


def drain(store):
    for _ in range(50):
        if not store.taste_groups.cleanup_staged():
            return
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    pytest.fail('Group cleanup did not finish')


def generations(store):
    return [row[0] for row in store.conn.execute('SELECT external_key FROM group_generations ORDER BY id')]


def test_retention_waits_for_existing_evidence_to_be_indexed(database, monkeypatch):
    store, _ = database
    assert not store.taste_groups.retire_history()
    assert generations(store) == ['first', 'second']
    enable(store)
    monkeypatch.setattr(group_publication, 'BATCH_ROWS', 1)
    assert store.taste_groups.retire_history()
    assert store.taste_groups.head_generation() == 'second'
    assert store.taste_groups.definition('first-leaf') is None
    assert store.taste_groups.cleanup_staged()
    assert generations(store) == ['first', 'second']
    drain(store)
    assert generations(store) == ['second']
    assert store.taste_groups.head(members=True)['groups'][1]['members'] == {'one': (.9, .8)}


@pytest.mark.parametrize('kind', ['offer', 'response', 'comparison', 'prepared', 'pending', 'checkpoint'])
def test_each_evidence_owner_keeps_its_exact_generation(database, kind):
    store, _ = database
    enable(store)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_evidence.retain(store.conn, kind, 'actual-reference', context(), 10)
    drain(store)
    assert store.taste_groups.definition('first-leaf', generation='first')['created_at'] == 1
    assert generations(store) == ['first', 'second']
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        group_evidence.release(store.conn, kind, 'actual-reference')
    drain(store)
    assert generations(store) == ['second']


def test_repeated_external_version_keeps_its_origin_while_selected(database):
    store, prepared = database
    enable(store)
    for group in prepared['groups']:
        group['version'] = group['version'].replace('third-', 'first-')
        if group['parent_version']:
            group['parent_version'] = group['parent_version'].replace('third-', 'first-')
    owner = store.taste_groups.stage(prepared)
    drain(store)
    assert generations(store) == ['first', 'second', 'third']
    finish(store, prepared, owner)
    drain(store)
    assert generations(store) == ['first', 'third']
    assert store.taste_groups.definition('first-leaf')['created_at'] == 1
    assert store.taste_groups.definition('first-leaf', generation='third')['created_at'] == 3


def test_runtime_group_file_collection_waits_for_readers(database, monkeypatch):
    store, _ = database
    root = group_documents.directory(store.conn)
    orphan = next(group_documents.publish_many(root, [('generation', {'orphan': True})]))[0]
    collector = store.group_evidence.output_collector(groups=True)
    remove = fao.delete
    deleted = []

    def outside_transaction(path, **kwargs):
        assert not store.conn.in_transaction
        deleted.append(str(path))
        return remove(path, **kwargs)

    monkeypatch.setattr(fao, 'delete', outside_transaction)
    try:
        with store.read_view():
            result = store.group_evidence.collect_output_files(collector)
            assert result.blocked and not deleted
        while store.group_evidence.collect_output_files(collector).pending:
            pass
    finally:
        collector.close()
    assert deleted == [orphan] and not Path(orphan).exists()
    for _, _, kind, filename, digest, size, offset in group_documents.references(store.conn):
        assert isinstance(group_documents.read(filename, digest, size, offset, kind), dict)
