"""Listening display publication retains its exact bytes and file ownership."""
import hashlib
import json
import sqlite3

import pytest

from yt_playlist.core import listening_outputs as owner, recommendation_outputs as files
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import migrate, registry, v090_listening_outputs
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store


def bundle(label='one'):
    raw = json.dumps({'schema': 2, 'columns': ['day', 'track'],
                      'rows': [['2026-10-29', label]], 'catalog': {label: {'title': label}}})
    key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    manifest = json.dumps({'schema': 2, 'built_at': 123.5, 'timezone': 'UTC',
        'generation': hashlib.sha256(key.encode()).hexdigest()[:24],
        'chunks': [{'key': key, 'from': '2026-10-29', 'until': '2026-10-29', 'bytes': len(raw.encode())}]})
    edition = json.dumps({'available': True, 'id': label, 'stories': []})
    return {key: raw}, manifest, edition


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema, record_property):
    import time

    store = Store(tmp_path / 'listening.db')
    try:
        began = time.perf_counter()
        template = _pristine_installed_schema(89)
        record_property('legacy_prefix_seconds', time.perf_counter() - began)
        began = time.perf_counter()
        template.backup(store.conn)
        store.conn.execute('PRAGMA journal_mode=WAL')
        store._bind_repositories()
        assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 89
        record_property('legacy_clone_seconds', time.perf_counter() - began)
        yield store
    finally:
        store.conn.close()


def cutover(store):
    migrate(store.conn, registry=registry.MIGRATIONS)
    store._bind_repositories()
    store._snapshot_revisions = Revisions(store.conn)


def seed(store):
    chunks, manifest, edition = bundle()
    orphan, _, _ = bundle('previous')
    values = {'listening_analytics': manifest, 'listening_edition': edition,
              **{'listening_chunk:' + key: raw for key, raw in (chunks | orphan).items()}}
    with store.conn:
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)',
                               [(surface, raw, 100 + i) for i, (surface, raw) in enumerate(values.items())])
    return values


def test_cutover_preserves_exact_chunk_bytes_index_times_and_previous_files(predecessor):
    values = seed(predecessor)
    cutover(predecessor)
    repo = predecessor.listening_analytics
    assert {surface: repo.encoded(surface) for surface in values} == values
    assert not predecessor.conn.execute("SELECT 1 FROM rec_proposals WHERE surface LIKE 'listening_%'").fetchone()
    assert [row[0] for row in predecessor.conn.execute('SELECT built_at FROM listening_output_files ORDER BY built_at')] == [100, 101, 102, 103]
    assert len(predecessor.conn.execute('SELECT * FROM listening_output_chunks').fetchall()) == 1
    assert predecessor.conn.execute('SELECT revision_reason FROM listening_output_publications').fetchone()[0] == 'legacy_not_recorded'
    assert not predecessor.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_outer_rollback_and_file_failure_preserve_the_last_good_publication(predecessor, monkeypatch):
    values = seed(predecessor)
    cutover(predecessor)
    plan = owner.prepare(*bundle('replacement'), 200, input_revision=12)
    before = list(predecessor.conn.iterdump())
    with pytest.raises(InterruptedError), predecessor.conn.atomic():
        owner.publish(predecessor.conn, plan)
        raise InterruptedError('Interrupted before the head switch commits')
    assert list(predecessor.conn.iterdump()) == before
    publish = files.publish
    calls = []
    def fail_second(root, encoded):
        calls.append(encoded)
        if len(calls) == 2:
            raise OSError('Output volume unavailable')
        return publish(root, encoded)
    monkeypatch.setattr(files, 'publish', fail_second)
    with pytest.raises(OSError), predecessor.conn.atomic():
        owner.publish(predecessor.conn, plan)
    assert list(predecessor.conn.iterdump()) == before
    assert {surface: predecessor.listening_analytics.encoded(surface) for surface in values} == values


def test_two_generation_retention_and_pinned_reader_collection(predecessor):
    values = seed(predecessor)
    cutover(predecessor)
    first_key = json.loads(values['listening_analytics'])['chunks'][0]['key']
    with predecessor.read_snapshot() as snapshot:
        pinned = snapshot.store
        assert pinned.listening_analytics.encoded('listening_chunk:' + first_key)
        for label in ('two', 'three'):
            with predecessor.conn.atomic():
                owner.publish(predecessor.conn, owner.prepare(*bundle(label), 200, input_revision=12))
        assert predecessor.listening_analytics.encoded('listening_chunk:' + first_key) is None
        assert pinned.listening_analytics.encoded('listening_chunk:' + first_key)
        assert files.collect_orphans(predecessor.conn) == 0
    assert files.collect_orphans(predecessor.conn) >= 3
    assert predecessor.conn.execute('SELECT count(*) FROM listening_output_publications').fetchone()[0] == 2


def test_backup_restore_includes_both_retained_editions(predecessor, tmp_path):
    seed(predecessor)
    cutover(predecessor)
    with predecessor.conn.atomic():
        owner.publish(predecessor.conn, owner.prepare(*bundle('two'), 200, input_revision=12))
    expected = {surface: predecessor.listening_analytics.encoded(surface) for surface in owner.SURFACES}
    with predecessor.conn.atomic():
        manifest = files.export(predecessor.conn, tmp_path / 'backup')
    restored = Store(tmp_path / 'restored.db')
    try:
        predecessor.conn.backup(restored.conn)
        files.restore(restored.conn, manifest)
        restored._bind_repositories()
        assert {surface: restored.listening_analytics.encoded(surface) for surface in owner.SURFACES} == expected
        assert all(str(row[0]).startswith(str(files.directory(restored.conn)))
                   for row in restored.conn.execute('SELECT filename FROM listening_output_files'))
    finally:
        restored.conn.close()


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_unavailable_chunk_is_a_cache_miss(predecessor, damage):
    from pathlib import Path
    values = seed(predecessor)
    cutover(predecessor)
    key = json.loads(values['listening_analytics'])['chunks'][0]['key']
    target = Path(predecessor.conn.execute('SELECT filename FROM listening_output_files WHERE chunk_key=?', (key,)).fetchone()[0])
    if damage == 'missing':
        target.unlink()
    else:
        target.write_text('damaged')
    assert predecessor.listening_analytics.encoded('listening_chunk:' + key) is None


@pytest.mark.parametrize('raw', ['{"value":NaN}', '{"value":Infinity}', '{"value":1e999}'])
def test_nonfinite_json_never_reaches_an_output_file(raw):
    with pytest.raises(ValueError):
        owner.prepare({}, raw, None, 1)


@pytest.mark.parametrize('damage', ['missing_chunk', 'wrong_hash', 'wrong_bytes', 'invalid_json', 'oversized'])
def test_malformed_legacy_output_retains_all_original_rows_and_schema(predecessor, damage):
    values = seed(predecessor)
    manifest = json.loads(values['listening_analytics'])
    key = manifest['chunks'][0]['key']
    with predecessor.conn:
        if damage == 'missing_chunk':
            predecessor.conn.execute('DELETE FROM rec_proposals WHERE surface=?', ('listening_chunk:' + key,))
        elif damage in ('wrong_hash', 'invalid_json', 'oversized'):
            raw = '{}' if damage == 'wrong_hash' else '{' if damage == 'invalid_json' else json.dumps({'padding': 'x' * files.MAX_BYTES})
            predecessor.conn.execute('UPDATE rec_proposals SET payload=? WHERE surface=?', (raw, 'listening_chunk:' + key))
        else:
            manifest['chunks'][0]['bytes'] += 1
            predecessor.conn.execute("UPDATE rec_proposals SET payload=? WHERE surface='listening_analytics'", (json.dumps(manifest),))
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError), predecessor.conn.atomic():
        v090_listening_outputs.apply(predecessor.conn, None)
    assert list(predecessor.conn.iterdump()) == before


def test_sql_rejects_incomplete_publication_orphan_index_and_mutation(predecessor):
    seed(predecessor)
    cutover(predecessor)
    conn = predecessor.conn
    with pytest.raises(sqlite3.IntegrityError), conn.atomic():
        conn.execute("INSERT INTO rec_proposals VALUES ('listening_edition','{}',1)")
    with pytest.raises(sqlite3.IntegrityError), conn.atomic():
        conn.execute("UPDATE listening_output_files SET content_digest=?", ('0' * 64,))
    with pytest.raises(sqlite3.IntegrityError), conn.atomic():
        conn.execute('DELETE FROM listening_output_chunks')
    with pytest.raises(sqlite3.IntegrityError), conn.atomic():
        identity = conn.execute('INSERT INTO listening_output_publications(output_count,chunk_count,revision_reason) '
            "VALUES (1,0,'legacy_not_recorded') RETURNING id").fetchone()[0]
        conn.execute("UPDATE listening_output_publications SET status='published' WHERE id=?", (identity,))
    with pytest.raises(sqlite3.IntegrityError), conn.atomic():
        conn.execute("INSERT INTO listening_output_chunks VALUES (999,0,'listening_chunk',?,'2026-10-29','2026-10-29',1)", ('0' * 24,))
