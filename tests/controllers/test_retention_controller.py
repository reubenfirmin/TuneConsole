from contextlib import contextmanager, nullcontext
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.enrichment_state import EnrichmentState
from yt_playlist.controllers.embeddings import Embeddings
from yt_playlist.controllers.musical_profiles import MusicalProfiles
from yt_playlist.controllers.retention import Retention
from yt_playlist.controllers.managed_file_retention import ManagedFileRetention
from yt_playlist.controllers.collection_retention import CollectionRetention
from yt_playlist.controllers.vibe_models import VibeModels
from yt_playlist.controllers.vibe_predictions import VibePredictions
from yt_playlist.core.maintenance_context import MaintenanceDatabase

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def retention():
    database = MaintenanceDatabase('connection', nullcontext(), None, lambda: False)
    return Retention(database, create_autospec(EnrichmentState, instance=True, spec_set=True),
                     create_autospec(VibeModels, instance=True, spec_set=True),
                     create_autospec(Embeddings, instance=True, spec_set=True),
                     create_autospec(MusicalProfiles, instance=True, spec_set=True),
                     create_autospec(VibePredictions, instance=True, spec_set=True),
                     create_autospec(ManagedFileRetention, instance=True, spec_set=True),
                     create_autospec(CollectionRetention, instance=True, spec_set=True))


def test_managed_collection_expires_alternatives_before_scanning(retention):
    calls = []
    retention.enrichment.expire_provider_alternatives.side_effect = lambda now: calls.append(('expire', now))
    collect = retention.managed.maintain
    collect.side_effect = lambda now: calls.append(('collect', now)) or 3
    assert retention.maintain_managed_files(123) == 3
    assert calls == [('expire', 123), ('collect', 123)]

    retention.enrichment.expire_provider_alternatives.side_effect = OSError('interrupted expiry')
    with pytest.raises(OSError, match='interrupted expiry'):
        retention.maintain_managed_files(124)
    collect.assert_called_once_with(123)


def test_prediction_expiry_delegates_to_the_prediction_owner(retention):
    retention.predictions.maintain.return_value = 5
    assert retention.maintain_prediction_provenance(123) == 5
    retention.predictions.maintain.assert_called_once_with(123)


def test_shutdown_releases_both_disposable_iterators(retention):
    retention.close()
    retention.managed.close.assert_called_once_with()
    retention.collections.close.assert_called_once_with()


def test_database_context_reflects_current_pause_state():
    paused = [False]
    connection = object()
    database = MaintenanceDatabase(connection, nullcontext(), None, lambda: paused[0])
    assert database.conn is connection and not database.activity_paused
    paused[0] = True
    assert database.activity_paused
    with pytest.raises(AttributeError):
        database.settings
