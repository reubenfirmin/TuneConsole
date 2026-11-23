from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.recommendation_refresh import RecommendationRefresh, VERSION
from yt_playlist.repos.rec_refresh import RecRefreshRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('revision,version,pending', [
    (None, VERSION, False), (None, None, True), (None, 'old', True), (3, VERSION, True)])
def test_queue_and_model_version_independently_require_refresh(revision, version, pending):
    repository = create_autospec(RecRefreshRepo, instance=True, spec_set=True)
    repository.revision.return_value = revision
    repository.version.return_value = version
    assert RecommendationRefresh(repository).has_pending_changes() is pending
    repository.acknowledge.assert_not_called()
