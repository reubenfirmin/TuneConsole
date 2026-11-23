"""Document orchestration and projections against mock SQL and file owners."""
from contextlib import nullcontext
import hashlib
import json
from itertools import count
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.listening_analytics import ListeningAnalytics
from yt_playlist.controllers.home_outputs import HomeOutputs
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.album_notes import AlbumNotesRepo
from yt_playlist.repos.recommendation_reports import RecommendationReportsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def proposals():
    repository = create_autospec(RecProposalsRepo, instance=True, spec_set=True)
    reports = create_autospec(RecommendationReportsRepo, instance=True, spec_set=True)
    files = create_autospec(RecommendationFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    files.describe.side_effect = lambda raw: (hashlib.sha256(raw).hexdigest(), len(raw))
    reports.reserve_file.side_effect = count(1)
    publications = create_autospec(RecommendationPublications, instance=True, spec_set=True)
    notes = create_autospec(AlbumNotesRepo, instance=True, spec_set=True)
    return RecommendationProposals(repository, reports, notes, files,
                                   create_autospec(ListeningAnalytics, instance=True, spec_set=True),
                                   create_autospec(HomeOutputs, instance=True, spec_set=True), lambda: publications)


def manifest(raw):
    return {'content_digest': hashlib.sha256(raw.encode()).hexdigest(), 'total_bytes': len(raw.encode()),
            'part_count': 1, 'parts': [
                {'chunk_key': '0', 'filename': 'report', 'content_digest': 'part', 'byte_size': 42}]}


def test_native_clicked_pool_bypasses_repository_dispatcher(proposals):
    publications = proposals.publications()
    publications.clicked_pool.return_value = {'parameters': {'lane': 'comfort'}, 'tracks': []}
    publications.clicked_pool_present.return_value = True
    proposals.put_encoded('home_pool:comfort', '{"tracks": []}', 10)
    publications.put_clicked_pool.assert_called_once_with('home_pool:comfort', '{"tracks": []}', 10)
    assert proposals.get_proposals('home_pool:comfort') == publications.clicked_pool.return_value
    assert proposals.has_proposals('home_pool:comfort')
    proposals.repository.put_encoded.assert_not_called()
    proposals.repository.document.assert_not_called()
    proposals.repository.has_proposals.assert_not_called()


def test_report_files_are_durable_before_sql_publication(proposals):
    calls = []
    proposals.files.publish_reserved.side_effect = lambda identity, encoded, **kwargs: (calls.append('file') or ('report', *kwargs['expected']))
    proposals.report_repository.publish.side_effect = lambda *args, **kwargs: calls.append('sql')
    proposals.put_proposals('listening_choices', {'at': 10, 'detail': 'é' * 70000}, 11)
    assert len(calls) > 2
    assert calls[-1] == 'sql' and set(calls[:-1]) == {'file'}
    assert proposals.report_repository.publish.call_args.kwargs['file_ids'] == list(range(1, len(calls)))
    proposals.repository.put_encoded.assert_not_called()


def test_failed_report_file_never_changes_sql_head(proposals):
    proposals.files.publish_reserved.side_effect = OSError('disk full')
    with pytest.raises(OSError):
        proposals.put_proposals('model_health', {'version': 1}, 10)
    proposals.report_repository.publish.assert_not_called()


def test_invalid_report_is_rejected_before_file_publication(proposals):
    with pytest.raises(ValueError):
        proposals.put_proposals('listening_choices', {'home_format': {'scores': {'invalid': 5}}}, 10)
    proposals.files.publish_reserved.assert_not_called()
    proposals.report_repository.publish.assert_not_called()


def test_report_read_captures_references_before_file_access(proposals):
    raw = '{ "detail": "é" }'
    proposals.report_repository.manifest.return_value = manifest(raw)
    proposals.files.read.return_value = raw
    assert proposals.get_proposals('model_health') == {'detail': 'é'}
    proposals.files.read.assert_called_once_with(manifest(raw)['parts'][0], surface='model_health', chunk_key='0')
    proposals.repository.document.assert_not_called()


@pytest.mark.parametrize('error', [OSError('lost file'), ValueError('bad envelope')])
def test_unreadable_report_returns_no_document(proposals, error):
    proposals.report_repository.manifest.return_value = manifest('{}')
    proposals.files.read.side_effect = error
    assert proposals.get_proposals('model_health') is None


@pytest.mark.parametrize('damage', ['missing', 'reordered', 'digest', 'size'])
def test_report_checks_exact_part_sequence_and_original_digest(proposals, damage):
    raw = '{}'
    reference = manifest(raw)
    if damage == 'missing':
        reference['parts'] = []
    elif damage == 'reordered':
        reference['parts'][0]['chunk_key'] = '1'
    elif damage == 'digest':
        reference['content_digest'] = 'wrong'
    else:
        reference['total_bytes'] += 1
    proposals.report_repository.manifest.return_value = reference
    proposals.files.read.return_value = raw
    with pytest.raises(ValueError):
        proposals.get_proposals('model_health')


def test_report_serving_fields_never_open_diagnostic_files(proposals):
    proposals.report_repository.present.return_value = True
    proposals.report_repository.usage.return_value = {'scores': {'radio': 9}}
    proposals.report_repository.checkpoint.return_value = {'at': 10}
    assert proposals.has_proposals('model_health')
    assert proposals.choice_usage() == {'scores': {'radio': 9}}
    assert proposals.report_checkpoint('model_health') == {'at': 10}
    proposals.files.read.assert_not_called()
    proposals.report_repository.manifest.assert_not_called()


def test_missing_report_has_no_document_or_serving_fields(proposals):
    proposals.report_repository.usage.return_value = {}
    proposals.report_repository.checkpoint.return_value = None
    proposals.report_repository.manifest.return_value = None
    assert proposals.get_proposals('listening_choices') is None
    assert proposals.choice_usage() == {}
    assert proposals.report_checkpoint('listening_choices') is None
    proposals.files.read.assert_not_called()


def test_unknown_report_checkpoint_is_rejected_before_sql(proposals):
    with pytest.raises(ValueError):
        proposals.report_checkpoint('home_prepared')
    proposals.report_repository.checkpoint.assert_not_called()


@pytest.mark.parametrize('surface', ['model_pair:2:incumbent:mixes', 'model_live_report', 'model_album_assignment'])
def test_retired_surfaces_do_not_read_or_write_any_owner(proposals, surface):
    assert proposals.get_proposals(surface) is None
    assert not proposals.has_proposals(surface)
    with pytest.raises(ValueError, match='Retired'):
        proposals.put_encoded(surface, '{}', 10)
    with pytest.raises(ValueError, match='Retired'):
        proposals.publish_prepared_home('{}', 10, surface=surface)
    assert not proposals.repository.mock_calls and not proposals.report_repository.mock_calls


def test_missing_tv_pool_returns_no_document(proposals):
    proposals.repository.document.return_value = None
    assert proposals.get_proposals('tv_pool') is None
    proposals.files.read.assert_not_called()


def test_listening_documents_use_the_listening_workflow(proposals):
    proposals.listening.encoded.return_value = '{"text":"Ready"}'
    proposals.listening.present.return_value = True
    proposals.put_encoded('listening_edition', '{"text":"Ready"}', 10)
    proposals.listening.publish.assert_called_once_with('{"text":"Ready"}', 10, 'listening_edition')
    assert proposals.get_proposals('listening_edition') == {'text': 'Ready'}
    assert proposals.has_proposals('listening_edition')
    proposals.repository.put_encoded.assert_not_called()
    proposals.repository.document.assert_not_called()
    proposals.repository.has_proposals.assert_not_called()


def test_home_documents_use_the_home_workflow(proposals):
    proposals.home.encoded.return_value = '{"title":"Ready"}'
    proposals.home.present.return_value = True
    proposals.put_encoded('home_prepared', '{"title":"Ready"}', 10)
    proposals.home.put.assert_called_once_with('home_prepared', '{"title":"Ready"}', 10)
    assert proposals.get_proposals('home_prepared') == {'title': 'Ready'}
    assert proposals.has_proposals('home_prepared')
    proposals.repository.put_encoded.assert_not_called()
    proposals.repository.document.assert_not_called()
    proposals.repository.has_proposals.assert_not_called()


def test_album_notes_are_encoded_before_replacing_sql_outputs(proposals):
    proposals.put_proposals('album_notes', {'release': {'title': 'Ready'}, 'second': {'title': 'Later'}}, 10)
    proposals.note_repository.replace_outputs.assert_called_once_with(
        [('release', '{"title":"Ready"}'), ('second', '{"title":"Later"}')])
    proposals.files.publish_reserved.assert_not_called()
    proposals.repository.put_encoded.assert_not_called()


@pytest.mark.parametrize('value', [[], {'release': 'invalid'}, {'': {}}, {str(index): {} for index in range(129)}])
def test_invalid_album_note_map_is_rejected_before_sql_replacement(proposals, value):
    with pytest.raises(ValueError):
        proposals.put_proposals('album_notes', value, 10)
    proposals.files.publish_reserved.assert_not_called()
    proposals.note_repository.replace_outputs.assert_not_called()


def test_album_notes_read_current_sql_outputs(proposals):
    proposals.note_repository.output_document.return_value = {'release': {'title': 'Ready'}}
    assert proposals.get_proposals('album_notes') == {'release': {'title': 'Ready'}}
    proposals.files.read.assert_not_called()
    proposals.note_repository.output_document.return_value = {}
    assert proposals.get_proposals('album_notes') is None


def test_album_note_presence_never_opens_a_file(proposals):
    proposals.note_repository.outputs_present.return_value = True
    assert proposals.has_proposals('album_notes')
    proposals.files.read.assert_not_called()
    proposals.note_repository.output_document.assert_not_called()
