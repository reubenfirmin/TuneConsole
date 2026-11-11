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
    repository.native.return_value = True
    repository.unchanged.return_value = False
    repository.document.return_value = None, None
    references = create_autospec(GroupEvidence, instance=True, spec_set=True)
    references.prepare_references.return_value = ('prepared', 'home_prepared', (), ())
    files = create_autospec(RecommendationFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    files.publish.return_value = ('file', 'digest', 123)
    files.write_cache.return_value = ('cache', 'digest', 123)
    return HomeOutputs(repository, references, files, Mock())


def test_publication_resolves_references_and_writes_files_before_sql(home):
    order = []
    roots = ('prepared', 'home_prepared', (), ())
    home.references.prepare_references.side_effect = lambda *args: (order.append('references') or roots)
    home.files.publish.side_effect = lambda *args: (order.append('file') or ('file', 'digest', 123))
    home.repository.publish.side_effect = lambda *args, **kwargs: order.append('sql')
    home.put('home_prepared', '{"context":{"artifact":"retained"}}', 10, input_revision=42)
    assert order == ['references', 'file', 'sql']
    prepared, filenames, policies, supplied_roots = home.repository.publish.call_args.args
    assert prepared.value == {'context': {'artifact': 'retained'}}
    assert filenames == (('home_prepared', '0', 'file', 'digest', 123),)
    assert policies == frozenset()
    assert supplied_roots == roots
    assert home.repository.publish.call_args.kwargs == {'input_revision': 42}
    order.clear()
    home.repository.unchanged.return_value = True
    home.repository.document.return_value = '{"context":{"artifact":"retained"}}', None
    home.put('home_prepared', '{"context":{"artifact":"retained"}}', 11, input_revision=43)
    assert order == []
    home.repository.publish.assert_called_once()


def test_missing_context_is_rejected_before_file_or_sql_writes(home):
    home.references.prepare_references.side_effect = ValueError('missing context')
    with pytest.raises(ValueError, match='missing context'):
        home.put('home_prepared', '{"context":{"artifact":"gone"}}', 10)
    home.files.publish.assert_not_called()
    home.repository.publish.assert_not_called()


def test_file_failure_preserves_the_sql_head(home):
    home.files.publish.side_effect = OSError('write failed')
    with pytest.raises(OSError):
        home.put('home_prepared', '{"title":"Ready"}', 10)
    home.repository.publish.assert_not_called()


def test_disposable_cache_uses_the_cache_file_writer_and_prepares_without_sql_publication(home):
    publication = home.prepare('home_tuned', '{"title":"Ready"}', 10, cache=True)
    assert publication.filenames == (('home_tuned', '0', 'cache', 'digest', 123),)
    home.files.publish.assert_not_called()
    home.repository.publish.assert_not_called()
    home.publish(publication)
    home.repository.publish.assert_called_once()


@pytest.mark.parametrize('surface,now', [('home_choice', 10), ('home_prepared', 11)])
def test_prepared_surface_and_time_must_match_before_any_work(home, surface, now):
    prepared = home_outputs.prepare('home_prepared', '{}', 10)
    with pytest.raises(ValueError, match='does not match'):
        home.prepare(surface, prepared, now)
    home.references.prepare_references.assert_not_called()
    home.files.publish.assert_not_called()


def test_legacy_writer_keeps_exact_installed_bytes_and_prepared_references(home):
    home.repository.native.return_value = False
    raw = '{ "context": {"artifact":"retained"} }'
    home.put('home_prepared', raw, 10, validate=False)
    home.references.prepare_references.assert_called_once_with('home_prepared', {'context': {'artifact': 'retained'}}, 10)
    home.repository.publish_legacy.assert_called_once_with('home_prepared', raw, 10, ('prepared', 'home_prepared', (), ()))
    home.files.publish.assert_not_called()
    home.repository.publish.assert_not_called()
    home.repository.document.return_value = raw, None
    home.put('home_prepared', raw, 11, validate=False)
    home.repository.publish_legacy.assert_called_once()


def test_validated_legacy_preparation_creates_no_files(home):
    home.repository.native.return_value = False
    publication = home.prepare('home_prepared', '{"title":"Ready"}', 10)
    assert publication.filenames == ()
    home.files.publish.assert_not_called()
    home.files.write_cache.assert_not_called()


def test_read_uses_detached_manifest_and_legacy_fallback(home):
    home.repository.document.return_value = None, ('digest', 10, [('0', 'file', 'digest', 123)])
    home.read_manifest.return_value = '{ "title":"Ready" }'
    assert home.encoded('home_prepared') == '{ "title":"Ready" }'
    home.read_manifest.assert_called_once_with('home_prepared', 'digest', 10, [('0', 'file', 'digest', 123)])
    home.repository.document.return_value = '{ "installed":true }', None
    assert home.encoded('home_prepared') == '{ "installed":true }'
    assert home.read_manifest.call_count == 1


def test_presence_never_reads_display_files(home):
    home.repository.present.return_value = True
    assert home.present('home_prepared')
    home.read_manifest.assert_not_called()
    home.repository.document.assert_not_called()
