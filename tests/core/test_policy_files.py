"""Current policy file publication needs only detached SQL facts."""
import hashlib
from dataclasses import replace
import time

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao, file_collection, file_map, policy_documents, policy_files, policy_history_files

pytestmark = pytest.mark.usefixtures('no_database')


def test_new_policy_file_reads_check_the_stored_digest_at_its_offset(tmp_path):
    files = policy_files.PolicyFiles(tmp_path, tmp_path / 'archive')
    (filename, digest, size, offset), = files.publish([{'value': {'weights': {'rock': 2}}}])
    assert files.read(dict(configuration_filename=filename, content_digest=digest,
                           byte_size=size, configuration_offset=offset)) == {'weights': {'rock': 2}}
    with pytest.raises(ValueError, match='digest'):
        policy_documents.read(filename, '0' * 64, size=size, offset=offset)


def test_shared_and_variant_values_have_separate_references_within_a_pack(tmp_path):
    files = policy_files.PolicyFiles(tmp_path, tmp_path / 'archive')
    first, second = files.publish([{'value': {}}, {'value': {}}])
    assert first[:3] == second[:3]
    assert first[3] != second[3]
    assert first == files.publish([{'value': {}}, {'value': {}}])[0]


def test_archiving_retains_the_shared_base_and_uses_its_reserved_sql_identity(tmp_path):
    files = policy_files.PolicyFiles(tmp_path / 'current', tmp_path / 'archive')
    references = files.publish([{'value': {'weights': {'rock': 2}}},
                                {'value': {'_policy_base': 'base', 'ranker': 'variant'}}])

    def facts(version, reference, base=None):
        filename, digest, size, offset = reference
        return dict(version=version, configuration_filename=filename, content_digest=digest,
                    byte_size=size, configuration_offset=offset, created_at=1, base_version=base)

    records = files.archive_records([facts('variant', references[1], 'base')],
                                    {'base': facts('base', references[0])})
    records[0]['file_id'] = 7
    with files.retained(), files.write_archive_reserved(records):
        with file_collection.collector(files.archive_root) as admitted:
            assert not admitted
        saved = files.archived('variant', dict(state='archived', file=dict(id=7,
            content_digest=records[0]['digest'], byte_size=records[0]['size'])))
        assert saved['configuration'] == {'_policy_base': 'base', 'ranker': 'variant'}
        assert saved['base'] == dict(version='base', created_at=1, configuration={'weights': {'rock': 2}})
        with pytest.raises(fao.CorruptFile, match='Invalid historical'):
            policy_history_files.read(files.archive_root, 'other', dict(id=7,
                content_digest=records[0]['digest'], byte_size=records[0]['size']))
    assert files.archived('variant', {'state': 'expired'}) is None


def test_new_archive_reads_its_digest_from_sql_instead_of_its_path(tmp_path):
    files = policy_files.PolicyFiles(tmp_path / 'current', tmp_path / 'archive')
    raw = policy_history_files.encode('v1', dict(state='archived', configuration={}, base=None))
    record = dict(file_id=2, version='v1', value=dict(state='archived', configuration={}, base=None),
                  digest=hashlib.sha256(raw).hexdigest(), size=len(raw))
    with files.write_archive_reserved([record]):
        with pytest.raises(fao.CorruptFile, match='committed configuration'):
            files.archived('v1', dict(state='archived', file=dict(id=2, content_digest='0' * 64, byte_size=len(raw))))


def test_installed_archives_remain_readable_without_conversion(tmp_path):
    files = policy_files.PolicyFiles(tmp_path / 'current', tmp_path / 'archive')
    saved = dict(state='archived', configuration={'original': True}, base=None)
    publication = file_map.update(files.archive_root, None, {'old': saved})
    assert files.archived('old', dict(state='archived', publication=publication)) == saved


def test_archive_collection_exposes_old_id_files_only_after_the_reader_is_released(tmp_path):
    files = policy_files.PolicyFiles(tmp_path / 'current', tmp_path / 'archive')
    value = dict(state='archived', configuration={}, base=None)
    raw = policy_history_files.encode('v1', value)
    record = dict(file_id=2, version='v1', value=value, digest=hashlib.sha256(raw).hexdigest(), size=len(raw))
    with files.write_archive_reserved([record]):
        pass
    path = files.archive_root / 'by-id' / '02' / '2.json'
    with fao.read(path) as stream:
        raw, info = stream.read(), stream.info
    fao.write(path, fao.Content(raw, metadata=replace(info, ctime_ns=info.ctime_ns - 31 * 86400 * 10**9)))
    before = time.time() - 30 * 86400
    with files.retained(), files.history_garbage(None, before, limit=16) as candidates:
        assert list(candidates) == []
        assert path.is_file()
    for _ in range(8):
        with files.history_garbage(None, before, limit=16) as candidates:
            candidate = next(candidates, None)
            if candidate is None:
                continue
            found_path, info, identity = candidate
            assert (found_path, identity) == (path, 2)
            assert files.remove_history(found_path, info)
            break
    else:
        pytest.fail('Released policy archive file was not discovered')
    assert not path.exists()
