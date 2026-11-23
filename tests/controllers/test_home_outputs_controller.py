"""Home publication prepares domain references and files before SQL writes."""
from contextlib import nullcontext
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.group_evidence import GroupEvidence
from yt_playlist.controllers.home_outputs import HomeOutputs
from yt_playlist.core import home_outputs
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.repos.home_cache import HomeCacheRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def home():
    repository = create_autospec(HomeCacheRepo, instance=True, spec_set=True)
    repository.unchanged.return_value = False
    repository.document.return_value = None
    repository.reserve_file.return_value = 41
    references = create_autospec(GroupEvidence, instance=True, spec_set=True)
    references.prepare_references.return_value = ('prepared', 'home_prepared', (), ())
    files = create_autospec(RecommendationFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    files.describe.return_value = ('digest', 123)
    files.publish_reserved.return_value = ('file', 'digest', 123)
    return HomeOutputs(repository, references, files, Mock())


def test_publication_resolves_references_and_writes_files_before_sql(home):
    order = []
    roots = ('prepared', 'home_prepared', (), ())
    home.references.prepare_references.side_effect = lambda *args: (order.append('references') or roots)
    home.repository.reserve_file.side_effect = lambda *args: (order.append('reserve') or 41)
    home.files.publish_reserved.side_effect = lambda *args, **kwargs: (order.append('file') or ('file', 'digest', 123))
    home.repository.publish.side_effect = lambda *args, **kwargs: order.append('sql')
    home.put('home_prepared', '{"context":{"artifact":"retained"}}', 10, input_revision=42)
    assert order == ['references', 'reserve', 'file', 'sql']
    prepared, filenames, policies, supplied_roots = home.repository.publish.call_args.args
    assert prepared.value == {'context': {'artifact': 'retained'}}
    assert filenames == (('home_prepared', '0', 'file', 'digest', 123),)
    assert policies == frozenset()
    assert supplied_roots == roots
    assert home.repository.publish.call_args.kwargs == {'input_revision': 42, 'file_ids': (41,)}
    home.files.publish_reserved.assert_called_once_with(41, prepared.parts[0], expected=('digest', 123))
    order.clear()
    home.repository.unchanged.return_value = True
    home.repository.document.return_value = home_outputs.Manifest(1, 'digest', 10, [('0', 'file', 'digest', 123)])
    home.read_manifest.return_value = '{"context":{"artifact":"retained"}}'
    home.put('home_prepared', '{"context":{"artifact":"retained"}}', 11, input_revision=43)
    assert order == []
    home.repository.publish.assert_called_once()


def test_missing_context_is_rejected_before_file_or_sql_writes(home):
    home.references.prepare_references.side_effect = ValueError('missing context')
    with pytest.raises(ValueError, match='missing context'):
        home.put('home_prepared', '{"context":{"artifact":"gone"}}', 10)
    home.files.publish_reserved.assert_not_called()
    home.repository.reserve_file.assert_not_called()
    home.repository.publish.assert_not_called()


def test_file_failure_preserves_the_sql_head(home):
    home.files.publish_reserved.side_effect = OSError('write failed')
    with pytest.raises(OSError):
        home.put('home_prepared', '{"title":"Ready"}', 10)
    home.repository.publish.assert_not_called()


def test_disposable_cache_reserves_a_file_without_replacing_the_sql_head(home):
    publication = home.prepare('home_tuned', '{"title":"Ready"}', 10, cache=True)
    assert publication.filenames == (('home_tuned', '0', 'file', 'digest', 123),)
    assert publication.file_ids == (41,)
    home.repository.reserve_file.assert_called_once_with('home_tuned', '0', ('digest', 123))
    home.repository.publish.assert_not_called()
    home.publish(publication)
    home.repository.publish.assert_called_once()


def test_healthy_identical_guarded_preparation_reuses_files_without_new_reservations(home):
    raw = '{"title":"Ready"}'
    home.repository.unchanged.return_value = True
    home.repository.document.return_value = home_outputs.Manifest(1, 'digest', 10, [('0', 'file', 'digest', 123)])
    home.read_manifest.return_value = raw
    prepared = home.prepare('home_prepared', raw, 10)
    assert prepared.file_ids == () and prepared.filenames == ()
    home.repository.reserve_file.assert_not_called()
    home.files.publish_reserved.assert_not_called()
    home.references.prepare_references.assert_not_called()


@pytest.mark.parametrize('surface,now', [('home_choice', 10), ('home_prepared', 11)])
def test_prepared_surface_and_time_must_match_before_any_work(home, surface, now):
    prepared = home_outputs.prepare('home_prepared', '{}', 10)
    with pytest.raises(ValueError, match='does not match'):
        home.prepare(surface, prepared, now)
    home.references.prepare_references.assert_not_called()
    home.files.publish_reserved.assert_not_called()


def test_read_uses_detached_manifest_when_present(home):
    home.repository.document.return_value = home_outputs.Manifest(1, 'digest', 10, [('0', 'file', 'digest', 123)])
    home.read_manifest.return_value = '{ "title":"Ready" }'
    assert home.encoded('home_prepared') == '{ "title":"Ready" }'
    home.read_manifest.assert_called_once_with('home_prepared', 'digest', 10, [('0', 'file', 'digest', 123)])
    home.repository.document.return_value = None
    assert home.encoded('home_prepared') is None
    assert home.read_manifest.call_count == 1


def test_presence_never_reads_display_files(home):
    home.repository.present.return_value = True
    assert home.present('home_prepared')
    home.read_manifest.assert_not_called()
    home.repository.document.assert_not_called()
