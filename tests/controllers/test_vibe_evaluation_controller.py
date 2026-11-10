"""Held-out targets must never become their own conditioning evidence."""
import json
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.genres import Genres
from yt_playlist.controllers.prepared_content import PreparedContent
from yt_playlist.controllers.vibe_evaluation import VibeEvaluation
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.controllers.musical_profiles import MusicalProfiles
from yt_playlist.repos.vibe_evaluation import VibeEvaluationRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def evaluation():
    repository = create_autospec(VibeEvaluationRepo, instance=True, spec_set=True)
    sources = create_autospec(MusicalProfiles, instance=True, spec_set=True)
    content = create_autospec(PreparedContent, instance=True, spec_set=True)
    genres = create_autospec(Genres, instance=True, spec_set=True)
    return VibeEvaluation(repository, sources, content, genres)


@pytest.mark.parametrize('videos,identities,recording', [
    (['a'], {'a': 7}, 7),
    (['b', 'a', 'a'], {'a': 7, 'b': 7}, 7),
    (['a', 'b'], {'a': 7, 'b': 8}, None),
    (['a', 'missing'], {'a': 7}, None),
    ([], {}, None),
])
def test_annotations_require_all_uploads_to_identify_the_same_recording(evaluation, videos, identities, recording):
    row = {'id': 1, 'run_id': 'run', 'legacy_run_key': None, 'state': 'empty'}
    evidence = {'videos': videos, 'labels': [], 'sample_ids': [], 'evaluation': None}
    evaluation.repository.annotation_rows.return_value = [row]
    evaluation.repository.annotation_values.return_value = evidence
    evaluation.repository.video_recordings.return_value = identities
    result = evaluation.annotations(['historical'])
    assert result['revisions'] == [{**row, **evidence, 'videos': sorted(set(videos)), 'recording': recording}]
    assert result['revisions'][0]['labels'] == []
    evaluation.repository.video_recordings.assert_called_once_with(sorted(set(videos) | {'historical'}))


@pytest.mark.parametrize('failure', ['rows', 'historical', 'videos', 'bytes'])
def test_annotation_budgets_fail_before_identity_expansion(evaluation, failure):
    row = {'id': 1, 'run_id': None, 'legacy_run_key': 'old'}
    evaluation.repository.annotation_rows.return_value = [row, row] if failure == 'rows' else [row]
    evaluation.repository.annotation_values.return_value = {
        'videos': ['a', 'b'] if failure == 'videos' else ['a'],
        'labels': [], 'sample_ids': [], 'evaluation': None,
    }
    with pytest.raises(WorkBudgetExceeded):
        evaluation.annotations(['a', 'b'] if failure == 'historical' else (),
                               limit=1, max_bytes=1 if failure == 'bytes' else 10000)
    evaluation.repository.video_recordings.assert_not_called()


def test_conditioning_rejects_broad_genres_and_excessive_artist_credits(evaluation):
    evaluation.repository.conditioning_rows.return_value = {
        1: {'artists': list(range(16)), 'genre': 'Specific'},
        2: {'artists': list(range(17)), 'genre': 'Rock'},
        3: {'artists': [], 'genre': None},
    }
    evaluation.repository.album_facts.return_value = {1: ('release', 1), 2: ('ambiguous', 2)}
    evaluation.genres.feature_axes.return_value = {'Specific': ('rock', 'specific'), 'Rock': ('rock', 'rock')}
    assert evaluation.conditioning([3, 1, 2, 1]) == {
        '1': {'recording': 1, 'artists': list(range(16)), 'genre': 'specific', 'album': 'release'},
        '2': {'recording': 2, 'artists': [], 'genre': None, 'album': None},
        '3': {'recording': 3, 'artists': [], 'genre': None, 'album': None},
    }


def test_profiles_exclude_target_observations_numeric_overrides_and_predictions(evaluation):
    evaluation.repository.recording_keys.return_value = {'key': 7}
    evaluation.sources.recording_inputs.return_value = {'key': ([
        {'scope': 'recording', 'score': .9}, {'scope': 'artist', 'text': 'artist description'},
        {'scope': 'release', 'text': 'release description'},
    ], {'genre': 'Specific', 'energy': .9, 'year': 2024}, True)}
    evaluation.content.capture.return_value = [('key', 0, json.dumps({
        'library': {'genre': 'Specific', 'audio': {'energy': .9}}, 'discovered': None,
    }), 'digest', True)]
    assert evaluation.profile_inputs([7, 7]) == {'7': {
        'content': {'library': {'genre': 'Specific', 'audio': {}}, 'discovered': None},
        'sources': [{'scope': 'artist', 'text': 'artist description'},
                    {'scope': 'release', 'text': 'release description'}],
        'overrides': {'genre': 'Specific'}, 'truncated': True,
    }}
    evaluation.sources.recording_inputs.assert_called_once_with(['key'], source_scopes=('artist', 'release'))
    evaluation.content.capture.assert_called_once_with([('key', 0)], mask_numeric=True)


def test_missing_original_cannot_be_silently_dropped(evaluation):
    evaluation.repository.recording_keys.return_value = {'key': 7}
    with pytest.raises(ValueError, match='each original recording'):
        evaluation.profile_inputs([7, 8])
    evaluation.sources.recording_inputs.assert_not_called()
    evaluation.content.capture.assert_not_called()


def test_masked_profile_byte_budget_is_enforced(evaluation):
    evaluation.repository.recording_keys.return_value = {'key': 7}
    evaluation.sources.recording_inputs.return_value = {'key': ([], {}, False)}
    evaluation.content.capture.return_value = [('key', 0, '{"library":null,"discovered":null}', 'digest', True)]
    with pytest.raises(WorkBudgetExceeded, match='byte budget'):
        evaluation.profile_inputs([7], max_bytes=1)


def test_empty_profile_selection_does_not_capture_evidence(evaluation):
    assert evaluation.profile_inputs([]) == {}
    evaluation.repository.recording_keys.assert_not_called()
    evaluation.sources.recording_inputs.assert_not_called()
    evaluation.content.capture.assert_not_called()


def test_training_manifest_uses_unambiguous_albums_and_original_versions(evaluation):
    evaluation.repository.training_rows.return_value = [
        dict(recording_id=1, measurement_id=7, artist_key='[4]', subgenre_key='specific',
             score=.8, support=.6, source='provider', input_digest='digest', exact_version='v')]
    evaluation.repository.album_facts.return_value = {1: (23, 2)}
    assert evaluation.training_manifest('relax', limit=2) == [
        dict(recording=1, measurement=7, artists=[4], genre='specific', album=None,
             score=.8, support=.6, source='provider/v', input_digest='digest', version='v')]
    evaluation.repository.training_rows.assert_called_once_with('relax', 2)


@pytest.mark.parametrize('failure', ['rows', 'bytes'])
def test_training_manifest_fails_closed_on_budgets(evaluation, failure):
    evaluation.repository.training_rows.return_value = [
        dict(recording_id=1, measurement_id=7, artist_key='[]', subgenre_key=None,
             score=.8, support=.6, source='provider', input_digest='digest', exact_version='v')]
    evaluation.repository.album_facts.return_value = {}
    with pytest.raises(WorkBudgetExceeded):
        evaluation.training_manifest('relax', limit=0 if failure == 'rows' else 2,
                                     max_bytes=1 if failure == 'bytes' else 10000)
    if failure == 'rows':
        evaluation.repository.album_facts.assert_not_called()
