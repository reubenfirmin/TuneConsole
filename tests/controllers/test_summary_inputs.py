from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.summary_inputs import SummaryInputs, DEPENDENCIES
from yt_playlist.repos.summary_inputs import SummaryInputsRepo
from yt_playlist.controllers.settings import Settings

pytestmark = pytest.mark.usefixtures('no_database')


def test_each_summary_uses_only_its_dependencies_and_external_changes():
    repository = create_autospec(SummaryInputsRepo, instance=True, spec_set=True)
    settings = create_autospec(Settings, instance=True, spec_set=True)
    revisions = {('player_events', 'key:tick'): 2, ('cleanup_ignored', ''): 3}
    repository.capture.return_value = revisions, 7, {'listening_analytics'}
    settings.get_setting.return_value = 'America/Costa_Rica'
    facts, zone = SummaryInputs(repository, settings).capture(iter(['analytics', 'cleanup']))
    assert facts == {'analytics': ((('player_events', 2),), 7, True),
                     'cleanup': ((('cleanup_ignored', 3),), 7, False)}
    assert zone == 'America/Costa_Rica'
    repository.capture.assert_called_once_with(
        DEPENDENCIES['analytics'] | DEPENDENCIES['cleanup'], ('listening_analytics', 'cleanup'))


def test_non_analytics_capture_does_not_depend_on_timezone():
    repository = create_autospec(SummaryInputsRepo, instance=True, spec_set=True)
    settings = create_autospec(Settings, instance=True, spec_set=True)
    repository.capture.return_value = {}, 1, set()
    assert SummaryInputs(repository, settings).capture(['cleanup'])[1] is None
    settings.get_setting.assert_not_called()
