"""Shared policy files retain compact variants, integrity and writer-fenced lifetime."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from yt_playlist.core import policy_documents as files
from yt_playlist.core import fao
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations.runner import Context, MigrationError
from yt_playlist.core.migrations import v070_policy_documents as migration
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.store import Store
from yt_playlist.repos import policy_storage


@pytest.fixture
def predecessor(tmp_path):
    conn = sqlite3.connect(tmp_path / 'policies.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    migrate(conn, registry=registry.MIGRATIONS[:69])
    base = json.dumps({'shared': {'setting': ['preserved', 1]}, 'unicode': 'é'})
    with conn:
        conn.execute('INSERT INTO rec_policy_bases(base_version,configuration,byte_size,created_at) '
                     'VALUES (?,?,?,?)', ('shared', base, len(base.encode()), 1))
        for version, ranker in [('one', 'primary'), ('two', 'alternative')]:
            raw = json.dumps({'_policy_base': 'shared', 'ranker': ranker})
            conn.execute('INSERT INTO rec_serving_policies VALUES (?,?,?)', (version, raw, 2))
            conn.execute('INSERT INTO rec_policy_variants(model_version,base_version) VALUES (?,?)',
                         (version, 'shared'))
        # Existing tables have distinct identities even when their text keys coincide.
        conn.execute('INSERT INTO rec_serving_policies VALUES (?,?,?)',
                     ('shared', json.dumps({'standalone': True}), 3))
    yield conn
    conn.close()


def apply(conn):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        migration.apply(conn, Context(4))
    migration.validate(conn)


def configuration(conn, version):
    row = conn.execute('SELECT * FROM recommendation_policies WHERE version=?', (version,)).fetchone()
    value = read_record(row)
    base = row['base_version']
    value.pop('_policy_base', None)
    if base is not None:
        body = conn.execute('SELECT * FROM recommendation_policy_bodies WHERE version=?', (base,)).fetchone()
        value = {**read_record(body), **value}
    return value


def read_record(row):
    return files.read(row['configuration_filename'], row['content_digest'],
                      offset=row['configuration_offset'], size=row['byte_size'])


def owned_files(conn):
    return {Path(row[0]) for owner in files.OWNERS
            for row in conn.execute(f'SELECT configuration_filename FROM {owner}')}


def impression(conn, request, version, provenance):
    return conn.execute('INSERT INTO rec_served_impressions('
        'request_id,surface,lane,identity_key,rank,model_version,provenance,served_at) '
        "VALUES (?,'home','main','unresolved-fixture',1,?,?,4)",
        (request, version, json.dumps(provenance))).lastrowid


def test_compact_shared_configuration_and_external_versions_are_preserved(predecessor):
    conn = predecessor
    before = {version: policy_storage.configuration(conn, version) for version in ('one', 'two', 'shared')}
    apply(conn)
    assert {version: configuration(conn, version) for version in before} == before
    assert conn.execute('SELECT count(*) FROM recommendation_policy_bodies').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM recommendation_policies').fetchone()[0] == 3
    for row in conn.execute("SELECT * FROM recommendation_policies WHERE base_version IS NOT NULL"):
        assert set(read_record(row)) == {'_policy_base', 'ranker'}
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert not any(conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone()
                   for name in migration.REPLACED)


@pytest.mark.parametrize('problem', ['malformed', 'nonfinite', 'unknown_base', 'wrong_link'])
def test_invalid_originals_fail_before_files_or_owner_replacement(predecessor, problem):
    conn = predecessor
    bad = {'malformed': 'original malformed document',
           'nonfinite': '{"value":NaN}',
           'unknown_base': '{"_policy_base":"unknown"}',
           'wrong_link': '{"standalone":true}'}[problem]
    with conn:
        conn.execute("UPDATE rec_serving_policies SET configuration=? WHERE model_version='one'", (bad,))
    before = list(conn.iterdump())
    with pytest.raises(MigrationError), conn:
        conn.execute('BEGIN IMMEDIATE')
        migration.apply(conn, Context(4))
    assert list(conn.iterdump()) == before
    assert not files.directory(conn).exists()


def test_an_orphan_original_link_cannot_be_silently_discarded(predecessor):
    conn = predecessor
    conn.execute('PRAGMA foreign_keys=OFF')
    with conn:
        conn.execute("INSERT INTO rec_policy_variants(model_version,base_version) VALUES ('orphan','shared')")
    conn.execute('PRAGMA foreign_keys=ON')
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match='Unresolved shared recommendation policy link'), conn:
        conn.execute('BEGIN IMMEDIATE')
        migration.apply(conn, Context(4))
    assert list(conn.iterdump()) == before
    assert not files.directory(conn).exists()


def test_interrupted_publication_rolls_back_owners_and_can_restart(predecessor, monkeypatch):
    conn = predecessor
    before = list(conn.iterdump())
    publish = files._publish_encoded
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('controlled interruption')
        return publish(*args, **kwargs)
    monkeypatch.setattr(files, '_publish_encoded', interrupted)
    with pytest.raises(OSError, match='controlled interruption'), conn:
        conn.execute('BEGIN IMMEDIATE')
        migration.apply(conn, Context(4))
    assert list(conn.iterdump()) == before
    orphaned = list(files.directory(conn).glob('*/*.json'))
    assert len(orphaned) == 1
    monkeypatch.setattr(files, '_publish_encoded', publish)
    apply(conn)
    assert configuration(conn, 'one')['ranker'] == 'primary'
    assert files.collect_orphans(conn) == 0


def test_database_rejects_broken_bases_and_content_mutation(predecessor):
    conn = predecessor
    apply(conn)
    for sql in ("UPDATE recommendation_policies SET content_digest=(CASE substr(content_digest,1,1) "
                "WHEN 'a' THEN 'b' ELSE 'a' END)||substr(content_digest,2) WHERE version='one'",
                "UPDATE recommendation_policy_bodies SET version='new' WHERE version='shared'",
                "DELETE FROM recommendation_policy_bodies WHERE version='shared'",
                "UPDATE recommendation_policies SET byte_size=-1 WHERE version='one'"):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(sql)
    with conn:
        conn.execute("UPDATE recommendation_policies SET served=1 WHERE version='one'")


def test_backup_restore_all_bases_and_variants_with_same_text_versions(predecessor, tmp_path):
    conn = predecessor
    apply(conn)
    before = {version: configuration(conn, version) for version in ('one', 'two', 'shared')}
    with files.retained_reader(files.directory(conn)), conn:
        conn.execute('BEGIN')
        manifest = files.export(conn, tmp_path / 'backup')
    originals = {owner: [tuple(r) for r in conn.execute(
        f'SELECT version,content_digest,byte_size FROM {owner} ORDER BY version')]
        for owner in files.OWNERS}
    for filename in owned_files(conn):
        filename.unlink()
    files.restore(conn, manifest)
    assert {version: configuration(conn, version) for version in before} == before
    for owner, rows in originals.items():
        assert [tuple(r) for r in conn.execute(
            f'SELECT version,content_digest,byte_size FROM {owner} ORDER BY version')] == rows
        for row in conn.execute(f'SELECT * FROM {owner}'):
            read_record(row)
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_thousands_of_compact_variants_do_not_allocate_a_file_per_policy(predecessor, tmp_path):
    conn = predecessor
    with conn:
        conn.executemany('INSERT INTO rec_serving_policies VALUES (?,?,?)',
                         [(f'compact-{i:04}', json.dumps({'ranker': f'model-{i}'}), 4) for i in range(2500)])
    apply(conn)
    paths = owned_files(conn)
    assert len(paths) < 5
    assert all(path.stat().st_size <= files.PACK_BYTES for path in paths)
    allocated = sum(getattr(path.stat(), 'st_blocks', (path.stat().st_size + 511) // 512) * 512
                    for path in paths)
    assert allocated < 1024 * 1024
    for i in (0, 127, 1023, 2048, 2499):
        assert files.configuration(conn, f'compact-{i:04}') == {'ranker': f'model-{i}'}
    with files.retained_reader(files.directory(conn)), conn:
        conn.execute('BEGIN')
        manifest = files.export(conn, tmp_path / 'backup')
    for path in paths:
        path.unlink()
    files.restore(conn, manifest)
    for i in (0, 127, 1023, 2048, 2499):
        assert files.configuration(conn, f'compact-{i:04}') == {'ranker': f'model-{i}'}
    assert len(owned_files(conn)) < 5
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_pack_records_are_individually_bounded_and_verified(tmp_path):
    expected = [{'ranker': 'first'}, {'ranker': 'second'}]
    records = list(files.publish_many(tmp_path, expected))
    assert records[0][0] == records[1][0]
    for (filename, digest, size, offset), value in zip(records, expected):
        assert files.read(filename, digest, offset=offset, size=size) == value
    filename, digest, size, offset = records[0]
    for wrong_offset, wrong_size in ((offset + 1, size), (offset, size + 1), (0, size), (offset, True)):
        with pytest.raises(ValueError):
            files.read(filename, digest, offset=wrong_offset, size=wrong_size)
    with pytest.raises(ValueError, match='digest'):
        files.read(filename, records[1][1], offset=offset, size=size)
    encoded = bytearray(fao.read_bytes(filename))
    encoded[offset] = ord('!')
    fao.write_bytes(filename, bytes(encoded))
    with pytest.raises(ValueError, match='digest'):
        files.read(filename, digest, offset=offset, size=size)
    second_file, second_digest, second_size, second_offset = records[1]
    assert files.read(second_file, second_digest, offset=second_offset, size=second_size) == expected[1]


def test_bulk_reader_pins_each_pack_once_and_verifies_each_record(tmp_path,monkeypatch):
    from contextlib import contextmanager
    expected = [{'original':i,'configuration':'x'*1000} for i in range(128)]
    references = list(files.publish_many(tmp_path,expected))
    assert len({row[0] for row in references})==1
    opened,original = [],fao.open_reader
    @contextmanager
    def counted(path,**kwargs):
        opened.append(path)
        with original(path,**kwargs) as stream:
            yield stream
    monkeypatch.setattr(fao,'open_reader',counted)
    values = list(files.read_many((filename,digest,offset,size) for filename,digest,size,offset in references))
    assert values==expected
    assert len(opened)==1
    filename,digest,size,offset = references[1]
    with pytest.raises(ValueError,match='digest'):
        list(files.read_many([(filename,references[0][1],offset,size)]))


def test_individual_large_policy_has_its_own_pack_without_splitting_the_record(tmp_path):
    large = {'large': 'x' * files.PACK_BYTES}
    values = [{'small': 1}, large, {'small': 2}]
    records = list(files.publish_many(tmp_path, values))
    assert len({row[0] for row in records}) == 3
    assert records[1][2] > files.PACK_BYTES
    for (filename, digest, size, offset), value in zip(records, values):
        assert files.read(filename, digest, offset=offset, size=size) == value


def test_collection_retains_a_pack_until_its_last_policy_owner_is_removed(predecessor):
    conn = predecessor
    apply(conn)
    leaves = {row[0] for row in conn.execute('SELECT configuration_filename FROM recommendation_policies')}
    assert len(leaves) == 1
    leaf = Path(next(iter(leaves)))
    with conn:
        conn.execute("DELETE FROM recommendation_policies WHERE version IN ('one','two')")
    assert files.collect_orphans(conn) == 0
    assert leaf.exists()
    assert files.configuration(conn, 'shared') == {'standalone': True}
    with conn:
        conn.execute("DELETE FROM recommendation_policies WHERE version='shared'")
    assert files.collect_orphans(conn) == 1
    assert not leaf.exists()
    assert read_record(conn.execute('SELECT * FROM recommendation_policy_bodies').fetchone())['unicode'] == 'é'


def test_interrupted_restore_never_publishes_partial_database_ranges(predecessor, tmp_path, monkeypatch):
    conn = predecessor
    apply(conn)
    with files.retained_reader(files.directory(conn)), conn:
        conn.execute('BEGIN')
        manifest = files.export(conn, tmp_path / 'backup')
    original = list(conn.iterdump())
    for path in owned_files(conn):
        path.unlink()
    publish = files._publish_encoded
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('controlled restore interruption')
        return publish(*args, **kwargs)
    monkeypatch.setattr(files, 'PACK_BYTES', 100)
    monkeypatch.setattr(files, '_publish_encoded', interrupted)
    with pytest.raises(OSError, match='controlled restore interruption'):
        files.restore(conn, manifest)
    assert list(conn.iterdump()) == original
    monkeypatch.setattr(files, '_publish_encoded', publish)
    files.restore(conn, manifest)
    assert files.configuration(conn, 'one')['ranker'] == 'primary'
    assert files.configuration(conn, 'two')['ranker'] == 'alternative'
    assert files.configuration(conn, 'shared') == {'standalone': True}


@pytest.mark.parametrize('problem', ['offset', 'byte_size', 'digest', 'path', 'duplicate', 'unhashable', 'extra'])
def test_invalid_backup_ranges_leave_all_database_owners_unchanged(predecessor, tmp_path, problem):
    conn = predecessor
    apply(conn)
    root = tmp_path / 'backup'
    with files.retained_reader(files.directory(conn)), conn:
        conn.execute('BEGIN')
        manifest = files.export(conn, root)
    document = files.read(manifest, Path(manifest).stem)
    chunk_digest = document['chunks'][0]
    chunk = files.read(root / chunk_digest[:2] / (chunk_digest + '.json'), chunk_digest)
    entry = chunk['policies'][0]
    if problem == 'offset':
        entry['offset'] += 1
    elif problem == 'byte_size':
        entry['byte_size'] = True
    elif problem == 'digest':
        entry['digest'] = '0' * 64
    elif problem == 'path':
        entry['relative_filename'] = '../outside.json'
    elif problem == 'duplicate':
        chunk['policies'].append(dict(entry))
    elif problem == 'unhashable':
        entry['version'] = []
    else:
        entry['unexpected'] = 1
    document['chunks'][0] = files.publish(root, chunk)[1]
    bad_manifest = files.publish(root, document)[0]
    original = list(conn.iterdump())
    with pytest.raises(ValueError):
        files.restore(conn, bad_manifest)
    assert list(conn.iterdump()) == original
    assert files.configuration(conn, 'one')['ranker'] == 'primary'


def test_cleanup_cannot_remove_uncommitted_policy_files(predecessor):
    conn = predecessor
    apply(conn)
    other = sqlite3.connect(conn.execute('PRAGMA database_list').fetchone()[2], timeout=0)
    try:
        conn.execute('BEGIN IMMEDIATE')
        orphan, _digest, _size = files.publish(files.directory(conn), {'new': 'not referenced yet'})
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            files.collect_orphans(other)
        assert Path(orphan).exists()
        conn.rollback()
        assert files.collect_orphans(other) == 1
        assert not Path(orphan).exists()
    finally:
        conn.rollback()
        other.close()


def test_corrupt_existing_file_is_not_accepted_as_durable_content(tmp_path):
    configuration = {'shared': ['meaning']}
    filename, digest, _size = files.publish(tmp_path, configuration)
    Path(filename).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='digest'):
        files.publish(tmp_path, configuration)
    with pytest.raises(ValueError, match='digest'):
        files.read(filename, digest)


def test_empty_store_reader_retains_a_first_publication_until_it_exits(predecessor):
    conn = predecessor
    with conn:
        conn.execute('DELETE FROM rec_policy_variants')
        conn.execute('DELETE FROM rec_serving_policies')
        conn.execute('DELETE FROM rec_policy_bases')
    apply(conn)
    root = files.directory(conn)
    assert not root.exists()
    with files.retained_reader(root):
        filename, _digest, _size = files.publish(root, {'first': 'publication'})
        assert files.collect_orphans(conn) == 0
        assert Path(filename).exists()
    assert files.collect_orphans(conn) == 1
    assert not Path(filename).exists()


def test_reader_locks_allow_nested_and_cross_process_reads_but_exclude_collection(tmp_path):
    root = tmp_path / 'reader-locks'
    child = '''
from pathlib import Path
import sys
from yt_playlist.core.policy_documents import _try_file_lock
with Path(sys.argv[1]).open('a+b') as stream:
    unlock = _try_file_lock(stream, exclusive=sys.argv[2] == 'exclusive')
    print('acquired' if unlock is not None else 'busy')
# Closing the handle also releases a claim after interruption/process exit.
'''
    def probe(kind):
        return subprocess.run([sys.executable, '-c', child, str(root / '.readers.lock'), kind],
                              check=True, capture_output=True, text=True, timeout=10).stdout.strip()

    with files.retained_reader(root):
        with files.retained_reader(root):
            assert probe('shared') == 'acquired'
            assert probe('exclusive') == 'busy'
        assert probe('exclusive') == 'busy'
    assert probe('exclusive') == 'acquired'
    with files.retained_reader(root):
        assert probe('shared') == 'acquired'


@pytest.mark.parametrize('reader_kind', ['read_snapshot', 'read_view'])
def test_pruning_keeps_files_needed_by_older_database_readers(predecessor, reader_kind):
    conn = predecessor
    conn.execute('PRAGMA journal_mode=WAL')
    apply(conn)
    original_files = owned_files(conn)
    store = Store(conn.execute('PRAGMA database_list').fetchone()[2])
    store._bind_repositories()
    store._snapshot_revisions = Revisions(store.conn)
    try:
        with getattr(store, reader_kind)() as pinned:
            reader = pinned.store if reader_kind == 'read_snapshot' else pinned
            assert configuration(reader.conn, 'one')['ranker'] == 'primary'
            with conn:
                conn.execute("DELETE FROM recommendation_policies WHERE version IN ('one','two')")
                conn.execute("DELETE FROM recommendation_policy_bodies WHERE version='shared'")
            # The current head no longer owns these files, but the WAL reader does.
            assert files.collect_orphans(conn) == 0
            assert configuration(reader.conn, 'one')['ranker'] == 'primary'
            assert configuration(reader.conn, 'two')['ranker'] == 'alternative'
        assert files.collect_orphans(conn) == len(original_files - owned_files(conn))
        assert configuration(conn, 'shared') == {'standalone': True}
        assert files.collect_orphans(conn) == 0
    finally:
        store.conn.close()


def test_new_policy_publication_reuses_bodies_and_rejects_version_reinterpretation(predecessor):
    conn = predecessor
    apply(conn)
    body = json.dumps({'shared': ['new inputs']})
    rows = [('new-one', '{"_policy_base":"new-base","ranker":"first"}', 4),
            ('new-two', '{"_policy_base":"new-base","ranker":"second"}', 4)]
    bases = [('new-base', body, len(body.encode()), 4)]
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        files.install(conn, rows, bases)
    assert files.configuration(conn, 'new-one') == {'shared': ['new inputs'], 'ranker': 'first'}
    assert files.configuration(conn, 'new-two') == {'shared': ['new inputs'], 'ranker': 'second'}
    assert conn.execute('SELECT count(*) FROM recommendation_policy_bodies').fetchone()[0] == 2
    before = list(conn.iterdump())
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        files.install(conn, rows, bases)
    assert list(conn.iterdump()) == before
    with pytest.raises(ValueError, match='immutable'), conn:
        conn.execute('BEGIN IMMEDIATE')
        files.install(conn, [('new-one', '{"ranker":"different meaning"}', 5)])
    assert list(conn.iterdump()) == before


def test_detached_offer_policies_can_be_republished_after_current_rows_are_collected(predecessor):
    conn = predecessor
    apply(conn)
    expected = files.configuration(conn, 'one')
    rows, bases = files.captured(conn, ['one', 'two'])
    original_files = owned_files(conn)
    with conn:
        conn.execute("DELETE FROM recommendation_policies WHERE version IN ('one','two')")
        conn.execute("DELETE FROM recommendation_policy_bodies WHERE version='shared'")
    assert files.collect_orphans(conn) == len(original_files - owned_files(conn))
    assert files.configuration(conn, 'one') is None
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        files.install(conn, rows, bases)
    assert files.configuration(conn, 'one') == expected
    assert files.configuration(conn, 'two')['ranker'] == 'alternative'


@pytest.mark.parametrize('rows', [
    [('new', '{"_policy_base":"missing"}', 4)],
    [('new', '{"ranker":"first"}', 4), ('new', '{"ranker":"conflict"}', 4)],
    [('new', '{"value":NaN}', 4)],
])
def test_invalid_new_batch_does_not_publish_files_or_partial_rows(predecessor, rows):
    conn = predecessor
    apply(conn)
    original_files = set(files.directory(conn).glob('*/*.json'))
    before = list(conn.iterdump())
    with pytest.raises(ValueError), conn:
        conn.execute('BEGIN IMMEDIATE')
        files.install(conn, rows)
    assert list(conn.iterdump()) == before
    assert set(files.directory(conn).glob('*/*.json')) == original_files


def test_impressions_backfill_both_known_policy_paths_and_preserve_unresolved_evidence(predecessor):
    conn = predecessor
    with conn:
        first = impression(conn, 'first', 'one', {'pool_policy': 'two'})
        missing = impression(conn, 'missing', 'absent', {'pool_policy': True})
        numeric = impression(conn, 'numeric', 'shared', {'pool_policy': 17})
        no_pool = impression(conn, 'no-pool', 'one', {'pool_policy': None})
    originals = list(map(tuple, conn.execute('SELECT * FROM rec_served_impressions ORDER BY id')))
    apply(conn)
    assert list(map(tuple, conn.execute('SELECT * FROM rec_served_impressions ORDER BY id'))) == originals
    assert list(map(tuple, conn.execute('SELECT * FROM recommendation_impression_policies ORDER BY impression_id,kind'))) == [
        (first, 'pool', 'two', 'two', None), (first, 'primary', 'one', 'one', None),
        (missing, 'pool', '1', None, 'invalid_type'), (missing, 'primary', 'absent', None, 'missing_policy'),
        (numeric, 'pool', '17', None, 'missing_policy'), (numeric, 'primary', 'shared', 'shared', None),
        (no_pool, 'primary', 'one', 'one', None)]
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    plan = ' '.join(row[3] for row in conn.execute('EXPLAIN QUERY PLAN SELECT impression_id '
        'FROM recommendation_impression_policies WHERE policy_version=?', ('two',)))
    assert 'ix_recommendation_impression_policy' in plan and 'SCAN ' not in plan
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("DELETE FROM recommendation_policies WHERE version='two'")


def test_impression_retention_and_late_policy_admission_use_exact_relational_references(predecessor):
    conn = predecessor
    with conn:
        pending = impression(conn, 'pending', 'future', {'pool_policy': 'two'})
        invalid = impression(conn, 'invalid-type', 'absent', {'pool_policy': True})
    apply(conn)
    assert conn.execute("SELECT served FROM recommendation_policies WHERE version='two'").fetchone()[0] == 1
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        files.install(conn, [('future', '{"ranker":"later exact evidence"}', 5), ('1', '{}', 5)])
    assert tuple(conn.execute('SELECT policy_version,resolution_reason FROM recommendation_impression_policies '
        "WHERE impression_id=? AND kind='primary'", (pending,)).fetchone()) == ('future', None)
    assert tuple(conn.execute('SELECT policy_version,resolution_reason FROM recommendation_impression_policies '
        "WHERE impression_id=? AND kind='pool'", (invalid,)).fetchone()) == (None, 'invalid_type')
    assert conn.execute("SELECT served FROM recommendation_policies WHERE version='future'").fetchone()[0] == 1
    assert conn.execute("SELECT served FROM recommendation_policies WHERE version='1'").fetchone()[0] == 0
    with conn:
        conn.execute('DELETE FROM rec_served_impressions WHERE id=?', (pending,))
    assert not conn.execute('SELECT 1 FROM recommendation_impression_policies WHERE impression_id=?', (pending,)).fetchone()
    assert conn.execute("SELECT served FROM recommendation_policies WHERE version='future'").fetchone()[0] == 0
    assert conn.execute("SELECT served FROM recommendation_policies WHERE version='two'").fetchone()[0] == 0
    assert conn.execute("SELECT served FROM recommendation_policy_bodies WHERE version='shared'").fetchone()[0] == 0
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_new_impressions_capture_immutable_policy_ownership(predecessor):
    conn = predecessor
    apply(conn)
    with conn:
        owner = impression(conn, 'new', 'one', {'pool_policy': 'two'})
    assert conn.execute('SELECT count(*) FROM recommendation_impression_policies WHERE impression_id=?',
                         (owner,)).fetchone()[0] == 2
    for sql, values in (
            ("UPDATE rec_served_impressions SET model_version='two' WHERE id=?", (owner,)),
            ("UPDATE rec_served_impressions SET provenance='{}' WHERE id=?", (owner,)),
            ("UPDATE recommendation_impression_policies SET raw_version='two',policy_version='two' "
             "WHERE impression_id=? AND kind='primary'", (owner,)),
            ("UPDATE recommendation_impression_policies SET policy_version=NULL,resolution_reason='missing_policy' "
             "WHERE impression_id=? AND kind='primary'", (owner,)),
            ('DELETE FROM recommendation_impression_policies WHERE impression_id=?', (owner,))):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(sql, values)


@pytest.mark.parametrize('problem', ['nontext_version', 'nonobject_provenance'])
def test_invalid_impression_evidence_fails_before_files_or_schema_replacement(predecessor, problem):
    conn = predecessor
    with conn:
        impression(conn, 'invalid', b'original bytes' if problem == 'nontext_version' else 'one',
                   [] if problem == 'nonobject_provenance' else {})
    before = list(conn.iterdump())
    with pytest.raises(MigrationError, match='Invalid served policy evidence'), conn:
        conn.execute('BEGIN IMMEDIATE')
        migration.apply(conn, Context(4))
    assert list(conn.iterdump()) == before
    assert not files.directory(conn).exists()


@pytest.mark.parametrize('version,provenance', [(b'original bytes', '{}'), ('one', '[]'),
                                               ('one', 'original malformed document')])
def test_new_impressions_require_the_same_policy_evidence_shape(predecessor, version, provenance):
    conn = predecessor
    apply(conn)
    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='Invalid served policy evidence'), conn:
        conn.execute('INSERT INTO rec_served_impressions('
            'request_id,surface,lane,identity_key,rank,model_version,provenance,served_at) '
            "VALUES ('invalid','home','main','unresolved-fixture',1,?,?,4)", (version, provenance))
    assert list(conn.iterdump()) == before

