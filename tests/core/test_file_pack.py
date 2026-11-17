"""Pack boundaries and range integrity, primarily with in-memory readers."""
import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import (fao, file_pack, group_documents, legacy_evidence,
                              metadata_delivery_files, policy_documents)

pytestmark = pytest.mark.usefixtures('no_database')


def reader(raw, *, header=b'header\n', **limits):
    buffer = BytesIO(raw)
    stream = Mock(spec=['info', 'read', 'seek'])
    stream.info = SimpleNamespace(logical_bytes=len(raw))
    stream.read.side_effect = buffer.read
    stream.seek.side_effect = buffer.seek
    return file_pack.PackReader(stream, header=header, **limits), stream


def test_count_byte_and_header_bounds_preserve_exact_record_bytes_lazily():
    consumed = []
    records = [b'{}', '"caf\u00e9"'.encode(), b'x' * 17, b'[]']
    def source():
        for raw in records:
            consumed.append(raw)
            yield raw
    packs = file_pack.encode(source(), header=b'H\n', target_bytes=12, max_bytes=20, max_rows=2)
    assert consumed == []
    first = next(packs)
    assert consumed == records[:3]
    actual = [first, *packs]
    assert [pack.raw for pack in actual] == [b'H\n' + b''.join(records[:2]), b'H\n' + records[2], b'H\n[]']
    recovered = []
    for pack in actual:
        assert len(pack.raw) <= 20 and len(pack.records) <= 2
        for reference in pack.records:
            raw = pack.raw[reference.offset:reference.offset + reference.size]
            assert hashlib.sha256(raw).hexdigest() == reference.digest
            recovered.append(raw)
    assert recovered == records
    assert list(file_pack.encode([], header=b'H', target_bytes=12)) == []
    with pytest.raises(fao.FileBudgetExceeded, match='byte budget'):
        list(file_pack.encode([b'x' * 19], header=b'H\n', target_bytes=12, max_bytes=20))


def test_ordered_ranges_check_the_header_once_and_hash_only_exact_records():
    raw = b'header\n' + b'first' + b'second'
    pack, stream = reader(raw)
    assert pack.read(7, 5, hashlib.sha256(b'first').hexdigest()) == b'first'
    assert pack.read(12, 6, hashlib.sha256(b'second').hexdigest()) == b'second'
    assert stream.read.call_args_list == [call(7), call(5), call(6)]
    with pytest.raises(fao.CorruptFile, match='digest'):
        pack.read(7, 5, hashlib.sha256(b'other').hexdigest())


@pytest.mark.parametrize('offset,size,digest', [
    (True, 1, 'a' * 64), (1, True, 'a' * 64), (7, None, 'a' * 64),
    (-1, 1, 'a' * 64), (7, 0, 'a' * 64), (7, 1, 'bad'),
    (3, 1, 'a' * 64), (7, 1000, 'a' * 64), (0, 1, 'a' * 64),
])
def test_invalid_ranges_fail_before_decompression(offset, size, digest):
    pack, stream = reader(b'header\nbody')
    with pytest.raises(ValueError):
        pack.read(offset, size, digest)
    stream.read.assert_not_called()
    stream.seek.assert_not_called()


def test_wrong_headers_and_excessive_logical_sizes_are_rejected():
    pack, _stream = reader(b'wrong!\nbody')
    with pytest.raises(fao.CorruptFile, match='header'):
        pack.read(7, 4, hashlib.sha256(b'body').hexdigest())
    pack, stream = reader(b'header\nbody', max_pack_bytes=10)
    with pytest.raises(fao.FileBudgetExceeded, match='byte budget'):
        pack.read(7, 4, hashlib.sha256(b'body').hexdigest())
    stream.read.assert_not_called()
    pack, stream = reader(b'body', max_bytes=3)
    with pytest.raises(fao.FileBudgetExceeded, match='byte budget'):
        pack.read(0, None, hashlib.sha256(b'body').hexdigest())
    stream.read.assert_not_called()


def test_ranges_work_on_compressed_fao_streams_and_keep_their_work_budget(tmp_path):
    raw = b'header\nfirstsecond'
    path = tmp_path / '257.json'
    fao.write(path, raw)
    with fao.read(path, max_bytes=len(raw), work_bytes=len(raw)) as stream:
        pack = file_pack.PackReader(stream, header=b'header\n')
        assert pack.read(7, 5, hashlib.sha256(b'first').hexdigest()) == b'first'
        assert pack.read(12, 6, hashlib.sha256(b'second').hexdigest()) == b'second'
        with pytest.raises(fao.FileBudgetExceeded, match='work budget'):
            pack.read(7, 5, hashlib.sha256(b'first').hexdigest())


@pytest.mark.parametrize('owner', ['policy', 'group', 'metadata', 'evidence'])
def test_document_owners_share_count_limits_and_keep_installed_encodings(tmp_path, owner):
    values = [{'number': index} for index in range(129)]
    if owner == 'policy':
        references = list(policy_documents.publish_many(tmp_path, values))
        load = lambda ref: policy_documents.read(ref[0], ref[1], size=ref[2], offset=ref[3])
    elif owner == 'group':
        references = list(group_documents.publish_many(tmp_path, (('definition', value) for value in values)))
        load = lambda ref: group_documents.read(*ref, 'definition')
    elif owner == 'metadata':
        references = list(metadata_delivery_files.publish_encoded(tmp_path, map(metadata_delivery_files.encode, values)))
        load = lambda ref: metadata_delivery_files.read(*ref)
    else:
        references = list(legacy_evidence._publish(tmp_path, 'workflow',
            (legacy_evidence._encode('workflow', str(value['number'])) for value in values)))
        load = lambda ref: {'number': int(legacy_evidence._read('workflow', *ref)[1])}
    assert len({ref[0] for ref in references[:128]}) == 1
    assert references[128][0] != references[0][0]
    assert [load(ref) for ref in references] == values
