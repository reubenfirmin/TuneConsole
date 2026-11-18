"""Exact profile packing and expiry with database access disabled."""
from dataclasses import replace
import hashlib
import json
import time

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao, profile_files
from yt_playlist.core.profile_paths import record_path

pytestmark = pytest.mark.usefixtures('no_database')


def values(count):
    raw = '{ "dimensions": {}, "evidence": "preserve whitespace" }'
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return [(str(i), i + 1, raw, 'v1', digest) for i in range(count)]


@pytest.mark.parametrize('kind', ['recording', 'source'])
def test_bounded_native_profile_packs_preserve_exact_bodies_and_independent_identity(tmp_path, kind):
    files = profile_files.ProfileFiles(tmp_path)
    records = files.describe(kind, values(129))
    assert [len(record['rows']) for record in records] == [128, 1]
    for identity, record in zip((257, 258), records, strict=True):
        record['file_id'] = identity
    heads = files.write_reserved(records)
    rows = [(key, '', version, digest, owner, (publication, identity), size)
            for key, owner, publication, size, version, digest, identity, physical_size in heads]
    assert files.read_page(kind, rows[::-1]) == {key: json.loads(raw) for key, owner, raw, version, digest in values(129)}
    path = tmp_path / record_path(kind, 257)
    assert fao.read(path).bytes == records[0]['raw']
    files.write_reserved([records[0] | {'file_id': 513}])
    assert fao.read(tmp_path / record_path(kind, 513)).bytes == fao.read(path).bytes
    fao.write(path, b'{}')
    with pytest.raises(fao.CorruptFile, match='publication'):
        files.read_page(kind, rows[:1])


def test_mutated_profile_description_is_rejected_before_writing(tmp_path):
    files = profile_files.ProfileFiles(tmp_path)
    record, = files.describe('recording', values(1))
    with pytest.raises(ValueError, match='changed after reservation'):
        files.write_reserved([record | {'file_id': 1, 'raw': b'{}'}])
    assert not list(tmp_path.rglob('*.json'))


def test_profile_byte_limit_counts_utf8_records_and_the_complete_envelope(monkeypatch):
    raw = '{ "evidence": "caf\u00e9" }'
    digest = hashlib.sha256(raw.encode()).hexdigest()
    one = [('one', 1, raw, 'v1', digest)]
    first, = profile_files.ProfileFiles.describe('recording', one)
    monkeypatch.setattr(profile_files, 'MAX_BYTES', len(first['raw']))
    records = profile_files.ProfileFiles.describe('recording', one + [('two', 2, raw, 'v1', digest)])
    assert [record['size'] for record in records] == [len(first['raw'])] * 2
    monkeypatch.setattr(profile_files, 'MAX_BYTES', len(first['raw']) - 1)
    with pytest.raises(fao.FileBudgetExceeded, match='byte budget'):
        profile_files.ProfileFiles.describe('recording', one)


def test_mixed_hash_native_and_inline_heads_remain_readable(tmp_path):
    files = profile_files.ProfileFiles(tmp_path)
    record, = files.describe('recording', values(1))
    fao.write(tmp_path / profile_files.relative('recording', record['digest']), record['raw'])
    head, = files.write_reserved([record | {'file_id': 1}])
    key, owner, publication, size, version, digest, identity, physical_size = head
    rows = [(key, '', version, digest, owner, (publication, identity), size),
            ('legacy', '', version, digest, owner, publication, size),
            ('inline', '{}', version, None, None, None, None)]
    assert files.read_page('recording', rows) == {key: json.loads(values(1)[0][2]),
                                               'legacy': json.loads(values(1)[0][2]), 'inline': {}}


@pytest.mark.parametrize('legacy_cursor', [None, 441])
def test_profile_collection_obeys_reader_fences_age_and_candidate_budget(tmp_path, legacy_cursor):
    files = profile_files.ProfileFiles(tmp_path)
    if legacy_cursor is not None:
        fao.write(tmp_path / '.collection.json', json.dumps({'shard': legacy_cursor}).encode())
    records = files.describe('recording', values(1))
    records[0]['file_id'] = 1
    files.write_reserved(records)
    path = tmp_path / record_path('recording', 1)
    with files.garbage(time.time() - 30*86400, limit=256) as candidates:
        assert list(candidates) == []
    content = fao.read(path)
    fao.write(path, fao.Content(content.bytes,
        metadata=replace(content.info, ctime_ns=content.info.ctime_ns - 31*86400*10**9)))
    with files.retained(), files.garbage(time.time() - 30*86400, limit=256) as candidates:
        assert list(candidates) == []
    found = []
    for _ in range(8):
        with files.garbage(time.time() - 30*86400, limit=256) as candidates:
            page = list(candidates)
            assert len(page) <= 256
            found.extend(page)
        if found:
            break
    assert {(kind, identity, candidate) for kind, identity, candidate, info in found} == {('recording', 1, path)}
    assert files.remove(path, found[0][3])
    assert (tmp_path / '.collection-v2.json').is_file()
    if legacy_cursor is not None:
        assert json.loads(fao.read(tmp_path / '.collection.json').bytes) == {'shard': legacy_cursor}
