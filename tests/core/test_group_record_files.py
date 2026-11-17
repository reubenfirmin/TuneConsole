"""SQL-independent group file publication and installed packed reads."""
import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao, group_documents

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('kind', group_documents.KINDS)
def test_group_files_use_the_reserved_id_and_keep_their_document_kind(tmp_path, kind):
    files = group_documents.GroupFiles(tmp_path, tmp_path / 'policies')
    record, = files.describe([(kind, {'original': True})])
    record['file_id'] = 513
    reference, = files.write_reserved([record])
    assert reference[0] == str(tmp_path / 'by-id/01/513.json')
    assert group_documents.read(*reference, kind) == {'original': True}
    duplicate, = files.write_reserved([record | {'file_id': 514}])
    assert duplicate[0] != reference[0] and duplicate[1:] == reference[1:]
    with pytest.raises(ValueError, match='Unsupported'):
        group_documents.read(*reference, next(value for value in group_documents.KINDS if value != kind))


def test_a_changed_document_is_rejected_before_its_file_is_created(tmp_path):
    files = group_documents.GroupFiles(tmp_path, tmp_path / 'policies')
    record, = files.describe([('generation', {'original': True})])
    with pytest.raises(ValueError, match='changed after reservation'):
        files.write_reserved([record | dict(file_id=1, value={'changed': True})])
    assert not list(tmp_path.rglob('*.json'))


def test_installed_packs_stay_readable_beside_native_group_documents(tmp_path):
    references = list(group_documents.publish_many(tmp_path, [('generation', {'old': True}),
                                                            ('lineage', {'compatible': True})]))
    assert group_documents.read(*references[0], 'generation') == {'old': True}
    assert group_documents.read(*references[1], 'lineage') == {'compatible': True}
    fao.write(references[0][0], b'corrupt')
    with pytest.raises(ValueError):
        group_documents.read(*references[0], 'generation')
