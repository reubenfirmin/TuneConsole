"""Reserved response files retain their SQL identities without database access."""
import pytest

from tests.repository_mock_support import no_database
from yt_playlist.core import fao
from yt_playlist.core.metadata_delivery_files import MetadataDeliveryFiles

pytestmark = pytest.mark.usefixtures('no_database')


def test_native_response_checks_the_sql_digest_and_retains_distinct_file_ids(tmp_path):
    files = MetadataDeliveryFiles(tmp_path)
    encoded = files.encode({'value': 'é'})
    with files.write_reserved(257, encoded) as first, files.write_reserved(258, encoded) as second:
        assert first[0] == str(tmp_path / 'by-id/01/257.json')
        assert second[0] != first[0] and second[1:] == first[1:]
        assert files.read(first) == {'value': 'é'}
    fao.write(first[0], files.encode({'value': 'changed'}))
    with pytest.raises(ValueError):
        files.read(first)


def test_installed_response_packs_remain_readable_next_to_native_files(tmp_path):
    files = MetadataDeliveryFiles(tmp_path)
    installed = files.publish(files.encode({'value': 'installed'}))
    with files.write_reserved(1, files.encode({'value': 'new'})) as native:
        assert files.read(installed) == {'value': 'installed'}
        assert files.read(native) == {'value': 'new'}
