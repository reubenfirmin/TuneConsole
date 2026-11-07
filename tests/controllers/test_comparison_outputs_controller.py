"""Paired display workflows use mock SQL, files and publication collaborators."""
from contextlib import nullcontext
import hashlib
import json
import struct
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.comparison_outputs import ComparisonOutputs
from yt_playlist.controllers.group_evidence import GroupEvidence
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.repos.comparison_outputs import ComparisonOutputsRepo

pytestmark = pytest.mark.usefixtures('no_database')
SURFACE = 'model_pair:2:incumbent:mixes'


@pytest.fixture
def outputs():
    repository = create_autospec(ComparisonOutputsRepo, instance=True, spec_set=True)
    repository.native.return_value = True
    files = create_autospec(RecommendationFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    groups = create_autospec(GroupEvidence, instance=True, spec_set=True)
    publications = create_autospec(RecommendationPublications, instance=True, spec_set=True)
    return ComparisonOutputs(repository, files, groups, lambda: publications)


def document(raw, **changes):
    return {'part_count': 1, 'output_digest': hashlib.sha256(raw.encode()).hexdigest(),
            'parts': [{'filename': 'output', 'content_digest': 'part', 'byte_size': 100, 'chunk_key': '0'}],
            'modes': [], 'generation_id': None, **changes}


def test_files_and_roots_are_prepared_before_sql_head(outputs):
    events = []
    outputs.groups.prepare_references.side_effect = lambda *args: events.append('roots') or ('prepared',)
    outputs.files.publish.side_effect = lambda part: events.append('file') or ('output', 'digest', len(part))
    outputs.repository.publish.side_effect = lambda *args: events.append('sql')
    outputs.put(SURFACE, json.dumps({'run_id': 2, 'data': {'tuning': 'original'}}), 100)
    assert events == ['roots', 'file', 'sql']
    prepared, durable, generation, roots = outputs.repository.publish.call_args.args
    assert prepared.value == {'tuning': 'original'}
    assert durable == [('output', 'digest', len(prepared.parts[0]))]
    assert generation is None and roots == ('prepared',)
    outputs.publications().claim.assert_not_called()


def test_file_failure_preserves_sql_head_and_does_not_publish_ranked_generation(outputs):
    outputs.files.publish.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        outputs.put(SURFACE, json.dumps({'run_id': 2, 'data': {'tuning': 'new'}}), 100)
    outputs.repository.publish.assert_not_called()
    outputs.publications().claim.assert_not_called()


def test_installed_format_prepares_roots_before_its_sql_write(outputs):
    outputs.repository.native.return_value = False
    events = []
    outputs.groups.prepare_references.side_effect = lambda *args: events.append('roots') or ('prepared',)
    outputs.repository.publish_legacy.side_effect = lambda *args: events.append('sql')
    raw = '{ "run_id": 2, "data": {"tuning": "original"} }'
    outputs.put(SURFACE, raw, 100)
    assert events == ['roots', 'sql']
    outputs.groups.prepare_references.assert_called_once_with(SURFACE, json.loads(raw), 100)
    outputs.repository.publish_legacy.assert_called_once_with(SURFACE, raw, 100, ('prepared',))
    outputs.files.publish.assert_not_called()
    outputs.repository.publish.assert_not_called()


def test_invalid_owner_is_rejected_before_files_or_sql(outputs):
    with pytest.raises(ValueError, match='does not match'):
        outputs.put(SURFACE, json.dumps({'run_id': 3, 'data': {}}), 100)
    outputs.files.publish.assert_not_called()
    outputs.repository.publish.assert_not_called()


def test_radio_generation_is_published_before_comparison_reference(outputs):
    events = []
    publications = outputs.publications()
    publications.prepare_bundle.return_value = Mock(created_at=100)
    publications.claim.return_value = Mock(generation_id=7)
    publications.stage.side_effect = lambda *args: events.append('stage')
    publications.publish.side_effect = lambda *args: events.append('ranked')
    outputs.repository.publish.side_effect = lambda *args: events.append('comparison')
    value = {'policy_version': 'original', 'bundles': {'all': {'fresh': []}}}
    outputs.put('model_pair:2:incumbent:radio', json.dumps({'run_id': 2, 'data': value}), 100, input_revision=4)
    assert events == ['stage', 'ranked', 'comparison']
    assert publications.prepare_bundle.call_args.kwargs['input_revision'] == 4
    assert outputs.repository.publish.call_args.args[2] == 7


def test_radio_failure_does_not_publish_comparison_head(outputs):
    outputs.publications().stage.side_effect = RuntimeError('interrupted')
    value = {'policy_version': 'original', 'bundles': {'all': {'fresh': []}}}
    with pytest.raises(RuntimeError, match='interrupted'):
        outputs.put('model_pair:2:incumbent:radio', json.dumps({'run_id': 2, 'data': value}), 100)
    outputs.repository.publish.assert_not_called()
    outputs.publications().publish.assert_not_called()


@pytest.mark.parametrize('failure', ['missing', 'incomplete', 'file', 'digest', 'json'])
def test_unusable_display_returns_no_output(outputs, failure):
    raw = '{"tuning":"original"}'
    manifest = document(raw)
    outputs.files.read.return_value = raw
    if failure == 'missing':
        manifest = None
    elif failure == 'incomplete':
        manifest['part_count'] = 2
    elif failure == 'file':
        outputs.files.read.side_effect = FileNotFoundError('gone')
    elif failure == 'digest':
        manifest['output_digest'] = 'different'
    elif failure == 'json':
        raw = 'invalid json'
        manifest = document(raw)
        outputs.files.read.return_value = raw
    outputs.repository.document.return_value = manifest
    assert outputs.encoded(SURFACE) is None


def test_radio_display_restores_captured_modes_and_generation(outputs):
    raw = '{"modes":[{"label":"Original mode"}]}'
    outputs.repository.document.return_value = document(raw, generation_id=7,
        modes=[{'mode_id': 4, 'space': 'captured', 'centroid': struct.pack('<dd', 1., 0.)}])
    outputs.files.read.return_value = raw
    value = json.loads(outputs.encoded('model_pair:2:incumbent:radio'))
    assert value == {'run_id': 2, 'data': {
        'modes': [{'label': 'Original mode', 'mode_id': 4, 'space': 'captured', 'centroid': [1., 0.]}],
        '_ranked_generation': 7, '_ranked_prefix': 'model_pair:2:incumbent:radio:'}}


def test_presence_is_only_a_sql_head_lookup(outputs):
    outputs.repository.present.return_value = True
    assert outputs.present(SURFACE)
    outputs.repository.present.assert_called_once_with(2, 'incumbent', 'mixes')
    outputs.files.read.assert_not_called()
    outputs.repository.document.assert_not_called()
