"""Document orchestration and projections against mock SQL and file owners."""
from contextlib import nullcontext
import hashlib
import json
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.listening_analytics import ListeningAnalytics
from yt_playlist.controllers.home_outputs import HomeOutputs
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.controllers.comparison_outputs import ComparisonOutputs
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.core.schema_contract import MigrationError
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
    publications = create_autospec(RecommendationPublications, instance=True, spec_set=True)
    return RecommendationProposals(repository, reports, create_autospec(AlbumNotesRepo, instance=True, spec_set=True), files,
                                   create_autospec(ListeningAnalytics, instance=True, spec_set=True),
                                   create_autospec(HomeOutputs, instance=True, spec_set=True), lambda: publications,
                                   create_autospec(ComparisonOutputs, instance=True, spec_set=True))


def manifest(raw, *, prepared=False):
    return {'content_digest': hashlib.sha256(raw.encode()).hexdigest(), 'total_bytes': len(raw.encode()),
            'part_count': 1, 'prepared': prepared, 'parts': [
                {'chunk_key': '0', 'filename': 'report', 'content_digest': 'part', 'byte_size': 42}]}


def test_native_clicked_pool_bypasses_repository_dispatcher(proposals):
    publications = proposals.publications()
    publications.native_clicked_pools.return_value = True
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
    proposals.report_repository.native.return_value = True
    proposals.files.publish.side_effect = lambda encoded: (calls.append('file') or ('report', 'digest', len(encoded)))
    proposals.report_repository.publish.side_effect = lambda *args: calls.append('sql')
    proposals.put_proposals('listening_choices', {'at': 10, 'detail': 'é' * 70000}, 11)
    assert len(calls) > 2
    assert calls[-1] == 'sql' and set(calls[:-1]) == {'file'}
    proposals.repository.put_encoded.assert_not_called()


def test_failed_report_file_never_changes_sql_head(proposals):
    proposals.report_repository.native.return_value = True
    proposals.files.publish.side_effect = OSError('disk full')
    with pytest.raises(OSError):
        proposals.put_proposals('model_health', {'version': 1}, 10)
    proposals.report_repository.publish.assert_not_called()


def test_invalid_report_is_rejected_before_file_publication(proposals):
    proposals.report_repository.native.return_value = True
    with pytest.raises(ValueError):
        proposals.put_proposals('listening_choices', {'home_format': {'scores': {'invalid': 5}}}, 10)
    proposals.files.publish.assert_not_called()
    proposals.report_repository.publish.assert_not_called()


def test_supported_legacy_report_keeps_its_installed_writer(proposals):
    proposals.report_repository.native.return_value = False
    proposals.put_encoded('model_health', '{ "version": 1 }', 10)
    proposals.repository.put_encoded.assert_called_once_with('model_health', '{ "version": 1 }', 10)
    proposals.files.publish.assert_not_called()


def test_report_read_captures_references_before_file_access(proposals):
    raw = '{ "detail": "é" }'
    proposals.report_repository.document.return_value = None, manifest(raw)
    proposals.files.read.return_value = raw
    assert proposals.get_proposals('model_health') == {'detail': 'é'}
    proposals.files.read.assert_called_once_with(manifest(raw)['parts'][0], surface='model_health', chunk_key='0')
    proposals.repository.document.assert_not_called()


@pytest.mark.parametrize('error', [OSError('lost file'), ValueError('bad envelope')])
def test_unreadable_report_returns_no_document(proposals, error):
    proposals.report_repository.document.return_value = None, manifest('{}')
    proposals.files.read.side_effect = error
    assert proposals.get_proposals('model_health') is None


@pytest.mark.parametrize('prepared', [False, True])
@pytest.mark.parametrize('damage', ['missing', 'reordered', 'digest', 'size'])
def test_report_checks_exact_part_sequence_and_original_digest(proposals, prepared, damage):
    raw = '{}'
    reference = manifest(raw, prepared=prepared)
    if damage == 'missing':
        reference['parts'] = []
    elif damage == 'reordered':
        reference['parts'][0]['chunk_key'] = '1'
    elif damage == 'digest':
        reference['content_digest'] = 'wrong'
    else:
        reference['total_bytes'] += 1
    proposals.report_repository.document.return_value = None, reference
    proposals.files.read.return_value = raw
    if prepared:
        assert proposals.get_proposals('model_health') is None
    else:
        with pytest.raises(ValueError):
            proposals.get_proposals('model_health')


def test_report_serving_fields_never_open_diagnostic_files(proposals):
    proposals.report_repository.present.return_value = True
    proposals.report_repository.usage.return_value = True, {'scores': {'radio': 9}}
    proposals.report_repository.checkpoint.return_value = True, {'at': 10}
    assert proposals.has_proposals('model_health')
    assert proposals.choice_usage() == {'scores': {'radio': 9}}
    assert proposals.report_checkpoint('model_health') == {'at': 10}
    proposals.files.read.assert_not_called()
    proposals.report_repository.document.assert_not_called()


def test_legacy_report_serving_fields_come_from_installed_document(proposals):
    proposals.report_repository.usage.return_value = False, None
    proposals.report_repository.checkpoint.return_value = False, None
    proposals.report_repository.document.return_value = json.dumps({
        'built_at': 10, 'version': 2, 'inputs': 'old', 'home_format': {'scores': {'albums': 3}},
        'refresh': {'generation': 'new', 'inputs': 'current', 'next_at': 20}}), None
    assert proposals.choice_usage() == {'scores': {'albums': 3}}
    assert proposals.report_checkpoint('listening_choices') == {
        'at': 10, 'schema': 2, 'watermark': None, 'report_id': None,
        'generation': 'new', 'inputs': 'current', 'next_at': 20}
    proposals.files.read.assert_not_called()


def test_unknown_report_checkpoint_is_rejected_before_sql(proposals):
    with pytest.raises(ValueError):
        proposals.report_checkpoint('home_prepared')
    proposals.report_repository.checkpoint.assert_not_called()


def test_fresh_projection_omits_private_profiles_and_metadata(proposals):
    proposals.repository.document.return_value = json.dumps({
        '_meta': {'private': True}, 'all': {'fresh': [{'key': 'song', 'title': 'Title', 'score': .5,
                                                     'profile': {'audio': 'large'}}]}}), None
    assert proposals.fresh_candidates() == {'all': {'fresh': [{'key': 'song', 'title': 'Title', 'score': .5}]}}


@pytest.mark.parametrize('surface,prefix', [('model_pair:2:incumbent:mixes', 'original:'),
                                         ('home_pool:comfort', 'original:'), ('tv_pool', '')])
def test_prepared_original_is_read_outside_repository(proposals, surface, prefix):
    proposals.comparisons.native.return_value = False
    proposals.publications().native_clicked_pools.return_value = False
    raw = '{ "original": "é" }'
    reference = {**manifest(raw), 'prefix': prefix}
    proposals.repository.document.return_value = None, reference
    proposals.files.read.return_value = raw
    assert proposals.get_proposals(surface) == {'original': 'é'}
    proposals.files.read.assert_called_once_with(reference['parts'][0], surface=surface, chunk_key=prefix + '0')


@pytest.mark.parametrize('damage', ['missing', 'reordered', 'digest', 'size'])
def test_prepared_original_keeps_integrity_checks(proposals, damage):
    raw = '{}'
    reference = {**manifest(raw), 'prefix': ''}
    if damage == 'missing':
        reference['parts'] = []
    elif damage == 'reordered':
        reference['parts'][0]['chunk_key'] = '1'
    elif damage == 'digest':
        reference['content_digest'] = 'wrong'
    else:
        reference['total_bytes'] += 1
    proposals.repository.document.return_value = None, reference
    proposals.files.read.return_value = raw
    with pytest.raises(MigrationError):
        proposals.get_proposals('tv_pool')


def test_selected_empty_original_never_falls_back(proposals):
    proposals.repository.document.return_value = None, None
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
    proposals.home.put.assert_called_once_with('home_prepared', '{"title":"Ready"}', 10, validate=False)
    assert proposals.get_proposals('home_prepared') == {'title': 'Ready'}
    assert proposals.has_proposals('home_prepared')
    proposals.repository.put_encoded.assert_not_called()
    proposals.repository.document.assert_not_called()
    proposals.repository.has_proposals.assert_not_called()


def test_album_notes_are_written_before_replacing_sql_references(proposals):
    order = []
    proposals.note_repository.native_outputs.return_value = True
    proposals.files.publish.side_effect = lambda *args: (order.append('file') or ('note', 'digest', 123))
    proposals.note_repository.replace_outputs.side_effect = lambda *args: order.append('sql')
    proposals.put_proposals('album_notes', {'release': {'title': 'Ready'}, 'second': {'title': 'Later'}}, 10)
    assert order == ['file', 'file', 'sql']
    proposals.note_repository.replace_outputs.assert_called_once_with(
        [('release', ('note', 'digest', 123)), ('second', ('note', 'digest', 123))], 10)
    proposals.repository.put_encoded.assert_not_called()


@pytest.mark.parametrize('value', [[], {'release': 'invalid'}, {'': {}}, {str(index): {} for index in range(129)}])
def test_invalid_album_note_map_is_rejected_before_any_files(proposals, value):
    proposals.note_repository.native_outputs.return_value = True
    with pytest.raises(ValueError):
        proposals.put_proposals('album_notes', value, 10)
    proposals.files.publish.assert_not_called()
    proposals.note_repository.replace_outputs.assert_not_called()


def test_album_note_file_failure_preserves_previous_sql_references(proposals):
    proposals.note_repository.native_outputs.return_value = True
    proposals.files.publish.side_effect = OSError('volume unavailable')
    with pytest.raises(OSError):
        proposals.put_proposals('album_notes', {'release': {'title': 'Ready'}}, 10)
    proposals.note_repository.replace_outputs.assert_not_called()


def test_native_or_prepared_album_notes_read_detached_file_references(proposals):
    row = {'browse_id': 'release', 'filename': 'note', 'content_digest': 'digest', 'byte_size': 123}
    proposals.note_repository.output_document.return_value = None, [row]
    proposals.files.read.return_value = {'title': 'Ready'}
    assert proposals.get_proposals('album_notes') == {'release': {'title': 'Ready'}}
    proposals.files.read.assert_called_once_with(row, surface='album_notes', chunk_key='release')
    proposals.note_repository.output_document.return_value = None, []
    assert proposals.get_proposals('album_notes') is None


def test_album_note_presence_never_opens_a_file(proposals):
    proposals.note_repository.outputs_present.return_value = True
    assert proposals.has_proposals('album_notes')
    proposals.files.read.assert_not_called()
    proposals.note_repository.output_document.assert_not_called()
