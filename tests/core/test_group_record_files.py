"""SQL-independent publication and verification of group document ranges."""
import hashlib

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao, group_documents

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('kind', group_documents.KINDS)
def test_group_files_use_the_pack_digest_and_keep_their_document_kind(tmp_path, kind):
    files = group_documents.GroupFiles(tmp_path, tmp_path / 'policies')
    reference, = files.publish([(kind, {'original': True})])
    digest = hashlib.sha256(fao.read(reference[0]).bytes).hexdigest()
    assert reference[0] == str(tmp_path / digest[:2] / (digest + '.json'))
    assert group_documents.read(*reference, kind) == {'original': True}
    assert files.publish([(kind, {'original': True})]) == [reference]
    with pytest.raises(ValueError, match='Unsupported'):
        group_documents.read(*reference, next(value for value in group_documents.KINDS if value != kind))


def test_multiple_group_documents_share_a_pack_with_separate_verified_ranges(tmp_path):
    references = list(group_documents.publish_many(tmp_path, [('generation', {'old': True}),
                                                            ('lineage', {'compatible': True})]))
    assert group_documents.read(*references[0], 'generation') == {'old': True}
    assert group_documents.read(*references[1], 'lineage') == {'compatible': True}
    assert references[0][0] == references[1][0]
    assert references[1][-1] > references[0][-1]
    fao.write(references[0][0], b'corrupt')
    with pytest.raises(ValueError):
        group_documents.read(*references[0], 'generation')
