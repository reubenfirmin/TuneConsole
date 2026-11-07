"""Prediction policy and ordered persistence with explicit mocked owners."""
from contextlib import nullcontext
import hashlib
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.genres import Genres
from yt_playlist.controllers.prepared_content import PreparedContent
from yt_playlist.controllers.recommendation_refresh import RecommendationRefresh
from yt_playlist.controllers.vibe_predictions import VibePredictions
from yt_playlist.controllers.vibe_models import VibeModels
from yt_playlist.core import vibe_model_files, vibe_publication
from yt_playlist.core.audio_metadata_files import AudioMetadataFiles
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.sample_files import SampleFiles
from yt_playlist.core.vibe_provenance import ProvenanceFiles
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.repos.vibe_predictions import VibePredictionsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def predictions():
    service = VibePredictions(
        create_autospec(VibePredictionsRepo, instance=True, spec_set=True),
        create_autospec(ProvenanceFiles, instance=True, spec_set=True),
        create_autospec(SampleFiles, instance=True, spec_set=True),
        create_autospec(AudioMetadataFiles, instance=True, spec_set=True),
        create_autospec(VibeModels, instance=True, spec_set=True),
        create_autospec(SettingsRepo, instance=True, spec_set=True),
        create_autospec(RecommendationRefresh, instance=True, spec_set=True),
        create_autospec(PreparedContent, instance=True, spec_set=True),
        create_autospec(Genres, instance=True, spec_set=True))
    service.metadata.retained.side_effect = nullcontext
    service.genres.feature_axes.return_value = {'techno': ('electronic', 'techno')}
    service.provenance.stage_many.return_value = [], [], []
    service.repository.publication_facts.return_value = facts()
    return service


def facts():
    return {'ids': {'song': 3}, 'next_measurement': 7, 'next_prediction': 10,
            'previous': {}, 'measurements': {}, 'originals': {1: ('relax', 1), 2: ('relax', 2)},
            'artists': {4}, 'provenance': {}}


def track():
    return {'evidence': None, 'artist': '[4]', 'genre': 'techno', 'version': 'v'}


def fit(**changes):
    return {'source': 'artist', 'score': .8, 'support': .6, 'inputs': {'one': 1, 'two': 2}, **changes}


def previous(pid=9, version='v'):
    return dict(recording_id=3, vibe='relax', score=.8, support=.6, source='artist', measurement_id=None,
                prediction_id=pid, inputs_version=version, artist_key='[4]', subgenre_key='techno',
                recordings=None, variance=None)


def test_prediction_admission_and_file_preparation_use_detached_sql_facts(predictions):
    plan = predictions.prepare_publication({'song': track()}, {'song': {'relax': fit()}}, {'cohort'}, 20)
    assert plan.predictions == [(10, 3, 'relax', .8, .6, None, 'artist', 'v', 20)]
    assert plan.provenance == [(10, {1, 2}, [4])]
    predictions.repository.publication_facts.assert_called_once_with(['song'], {'v'}, [], {1, 2}, {4})
    predictions.provenance.stage_many.assert_called_once_with(plan.provenance)


@pytest.mark.parametrize('originals', [{1: ('relax', 3), 2: ('relax', 2)},
    {1: ('relax', 1), 2: ('relax', 1)}, {1: ('energize', 1), 2: ('relax', 2)}, {1: ('relax', 1)}])
def test_self_repeated_wrong_vibe_and_missing_contributors_fail_before_files(predictions, originals):
    predictions.repository.publication_facts.return_value['originals'] = originals
    with pytest.raises(ValueError, match='distinct original'):
        predictions.prepare_publication({'song': track()}, {'song': {'relax': fit()}}, [], 20)
    predictions.provenance.stage_many.assert_not_called()


@pytest.mark.parametrize('change', ['artist', 'genre'])
def test_unknown_conditioning_fails_before_file_preparation(predictions, change):
    if change == 'artist':
        predictions.repository.publication_facts.return_value['artists'] = set()
    else:
        predictions.genres.feature_axes.return_value = {'techno': ('electronic', None)}
    with pytest.raises(ValueError, match='conditioning|canonical'):
        predictions.prepare_publication({'song': track()}, {'song': {'relax': fit()}}, [], 20)
    predictions.provenance.stage_many.assert_not_called()


def test_identical_prediction_reuses_calculation_without_reading_provenance_files(predictions):
    saved = predictions.repository.publication_facts.return_value
    saved['previous'] = {3: {'relax': previous()}}
    saved['provenance'] = {9: (hashlib.sha256(canonical([1, 2])).digest(), 'ready', None, [])}
    plan = predictions.prepare_publication({'song': track()}, {'song': {'relax': fit()}}, [], 20)
    assert not plan.predictions and not plan.publications and not plan.provenance
    predictions.provenance.stage_many.assert_called_once_with([])


def test_changed_joint_conditioning_allocates_a_new_receipt(predictions):
    generation = 'a' * 64
    receipt = vibe_model_files.receipt(generation, [4], .01, .1, conditioning={'genre': 'techno', 'album': 'old'})
    saved = predictions.repository.publication_facts.return_value
    saved['previous'] = {3: {'relax': previous(version='v/model')}}
    saved['provenance'] = {9: (hashlib.sha256(canonical(receipt)).digest(), 'ready', generation, [])}
    resolved = fit(generation=generation, model_version='model', mean_variance=.01, new_track_variance=.1,
                   conditioning={'genre': 'techno', 'album': 'new'})
    plan = predictions.prepare_publication({'song': track()}, {'song': {'relax': resolved}}, [], 20)
    assert plan.predictions[0][0] == 10
    assert plan.provenance[0][1]['conditioning']['album'] == 'new'


def test_original_measurement_identity_cannot_change_its_score(predictions):
    saved = predictions.repository.publication_facts.return_value
    saved['measurements'] = {(3, 'relax'): (7, .1, .6, 'provider_measurement')}
    value = {**track(), 'evidence': {'source': 'provider_measurement', 'input_digest': 'digest',
                                    'fits': {'relax': {'score': .8, 'support': .6}}}}
    with pytest.raises(ValueError, match='changed its score'):
        predictions.prepare_publication({'song': value}, {'song': {}}, [], 20)
    predictions.provenance.stage_many.assert_not_called()


def test_file_publication_precedes_sql_and_separate_settings_and_notification(predictions):
    events = []
    file = Mock()
    file.publish.side_effect = lambda: events.append('file')
    plan = vibe_publication.PreparedVibes([], [], [], [('score',)], [], {'ordinary', '!joint:relax'}, staged=[file])
    predictions.repository.publish_prepared.side_effect = lambda *args, **kwargs: events.append('sql')
    predictions.settings.publish_values.side_effect = lambda values: events.append('settings')
    predictions.refresh.publish_notification.side_effect = lambda: events.append('notify')
    predictions.publish_prepared(plan)
    assert events == ['file', 'sql', 'settings', 'notify']
    predictions.repository.publish_prepared.assert_called_once_with(plan, cohorts=['ordinary'])
    predictions.settings.publish_values.assert_called_once_with({vibe_model_files.STATE_PREFIX + 'relax': ''})


def test_failed_file_does_not_write_sql_or_acknowledge_predictions(predictions):
    file = Mock()
    file.publish.side_effect = OSError('full')
    plan = vibe_publication.PreparedVibes([], [], [], [], [], set(), staged=[file])
    with pytest.raises(OSError, match='full'):
        predictions.publish_prepared(plan)
    predictions.repository.publish_prepared.assert_not_called()
    predictions.settings.publish_values.assert_not_called()
    predictions.refresh.publish_notification.assert_not_called()


def test_version_notification_is_queued_before_the_settings_marker(predictions):
    events = []
    predictions.settings.get_setting.return_value = 'old'
    predictions.repository.queue.side_effect = lambda keys: events.append('queue')
    predictions.settings.publish_values.side_effect = lambda values: events.append('settings')
    assert predictions.ensure_version('new')
    assert events == ['queue', 'settings']
    predictions.settings.get_setting.return_value = 'new'
    assert not predictions.ensure_version('new')
    assert events == ['queue', 'settings']


def test_broken_sample_run_falls_back_to_the_next_complete_playback(predictions):
    source = {'metadata': {'genre': 'Techno'}, 'choices': {}, 'sources': []}
    predictions.repository.input_facts.return_value = {'song': {
        'old': ([], []), 'artists': [4], 'recording': 3,
        'runs': [([{'id': 'bad'}], source), ([{'id': str(i)} for i in range(3)], source)]}}
    predictions.samples.read.side_effect = [OSError('missing'), {'id': '0'}, {'id': '1'}, {'id': '2'}]
    value = predictions.inputs(['song'])['song']
    assert value['samples'] == [{'id': '0'}, {'id': '1'}, {'id': '2'}]
    assert value['metadata']['genre'] == 'Techno'
    assert value['artist'] == '[4]'


def test_joint_profile_capture_is_explicitly_coordinated_with_content_service(predictions):
    predictions.repository.joint_profile_keys.return_value = {3: 'song'}
    predictions.prepared_content.capture.return_value = ['raw']
    assert predictions.joint_profile_rows([3]) == ({3: 'song'}, ['raw'])
    predictions.prepared_content.capture.assert_called_once_with([('song', 0)], mask_numeric=True)


def test_joint_model_uses_the_validated_model_service(predictions):
    predictions.models.read.return_value = {'vibe': 'relax', 'inputs': []}
    assert predictions.joint_model('generation') == {'vibe': 'relax', 'inputs': []}
    predictions.models.read.assert_called_once_with('generation')


def test_joint_head_is_published_after_model_file_and_measurement_roots(predictions):
    events, staged = [], Mock()
    staged.publish.side_effect = lambda: events.append('file')
    predictions.repository.root_joint_model.side_effect = lambda *args: events.append('roots')
    predictions.settings.publish_values.side_effect = lambda values: events.append('head')
    predictions.publish_joint_model('relax', 'generation', {'outcome': 'ready'}, [7], staged, True)
    assert events == ['file', 'roots', 'head']


def test_cohort_statistics_accumulate_weighted_originals_across_pages(predictions):
    rows = [(i, str(i), i, 'relax', .8, .5) for i in range(1, 129)]
    predictions.repository.cohort_rows.side_effect = [rows, [(129, '129', 129, 'relax', .2, 1.)]]
    value = predictions.groups(['cohort'])['cohort']
    assert value['stats']['relax'] == pytest.approx([129, 65, 51.4, 41.0])
    assert len(value['inputs']['relax']) == 129
    assert predictions.repository.cohort_rows.call_args_list[1].args == ('cohort', (128, 'relax'), 128)
