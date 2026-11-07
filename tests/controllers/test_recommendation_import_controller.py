"""Legacy import admission and ordering without opening SQLite or files."""
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.recommendation_import import RecommendationImport
from yt_playlist.core import recommendation_outputs, recommendation_plan, recommendation_pools
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.repos.recommendation_import import RecommendationImportRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def importer():
    repository = create_autospec(RecommendationImportRepo, instance=True, spec_set=True)
    repository.in_transaction.return_value = True
    repository.generation_exists.return_value = False
    repository.policy_families.return_value = {'policy': 'policy'}
    repository.pool_owner.return_value = None
    repository.known_recordings.side_effect = set
    repository.pool_matches.return_value = True
    repository.create_pool.return_value = 9
    files = create_autospec(RecommendationFiles, instance=True, spec_set=True)
    return RecommendationImport(repository, files)


def plan():
    pool = recommendation_pools.prepare('fresh', 'policy', None, '1' * 64, 'fresh',
        [recommendation_pools.Candidate(index + 1, .5) for index in range(129)])
    return recommendation_plan.Plan('legacy', 'policy', None, 0, 100,
        slots=(recommendation_plan.Slot('home', 'all', 'fresh', pool),),
        outputs=(recommendation_plan.Output('home', 'chunk', recommendation_outputs.encode('home', 'chunk', {})),))


def test_all_canonical_owners_are_resolved_before_import_starts(importer):
    importer.repository.known_recordings.side_effect = [set(range(1, 129)), set()]
    with pytest.raises(ValueError, match='unresolved canonical'):
        importer.install(plan(), 100)
    importer.repository.create.assert_not_called()
    importer.files.publish.assert_not_called()


def test_import_file_precedes_owner_then_metadata_precedes_head(importer):
    prepared = plan()
    build = recommendation_plan.Build(7, 'worker', recommendation_plan.validate(prepared))
    importer.repository.create.return_value = build
    events = []
    importer.files.publish.side_effect = lambda encoded: events.append('file') or ('path', 'digest', 10)
    importer.repository.record_output.side_effect = lambda *args: events.append('owner')
    importer.repository.ready.side_effect = lambda *args: events.append('ready')
    importer.repository.finish.side_effect = lambda *args: events.append('head')
    assert importer.install(prepared, 100, before_publish=lambda identity: events.append(('metadata', identity))) == 7
    assert events == ['file', 'owner', 'ready', ('metadata', 7), 'head']
    assert [len(call.args[1]) for call in importer.repository.append_pool.call_args_list] == [128, 1]


def test_failed_metadata_keeps_head_unpublished(importer):
    def fail(identity):
        raise RuntimeError('metadata failed')
    with pytest.raises(RuntimeError, match='metadata failed'):
        importer.install(plan(), 100, before_publish=fail)
    importer.repository.finish.assert_not_called()


def test_failed_file_does_not_create_owner_or_publish_head(importer):
    importer.files.publish.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        importer.install(plan(), 100)
    importer.repository.record_output.assert_not_called()
    importer.repository.finish.assert_not_called()


def test_retiring_pool_is_rejected_before_file_work(importer):
    importer.repository.pool_owner.return_value = {'id': 9, 'complete': 1, 'retiring': 1}
    with pytest.raises(ValueError, match='unfinished or retiring'):
        importer.install(plan(), 100)
    importer.repository.create.assert_not_called()
    importer.files.publish.assert_not_called()


def test_policy_mismatch_is_rejected_before_owner_work(importer):
    importer.repository.policy_families.return_value = {}
    with pytest.raises(ValueError, match='recorded policy'):
        importer.install(plan(), 100)
    importer.repository.create.assert_not_called()
    importer.files.publish.assert_not_called()
