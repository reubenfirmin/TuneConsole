"""Sample and annotation workflows use repository and file-object mocks."""
from contextlib import contextmanager, nullcontext
from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.audio_vibes import AudioVibes
from yt_playlist.controllers.prepared_content import PreparedContent
from yt_playlist.core.audio_annotation_files import AnnotationFiles
from yt_playlist.core.audio_metadata_files import AudioMetadataFiles
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core.sample_files import SampleFiles
from yt_playlist.repos.audio_vibes import AudioVibesRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def audio():
    repository = create_autospec(AudioVibesRepo, instance=True, spec_set=True)
    samples = create_autospec(SampleFiles, instance=True, spec_set=True)
    annotations = create_autospec(AnnotationFiles, instance=True, spec_set=True)
    prepared = create_autospec(PreparedContent, instance=True, spec_set=True)
    metadata = create_autospec(AudioMetadataFiles, instance=True, spec_set=True)
    metadata.retained.side_effect = nullcontext
    repository.playback.return_value = dict(video_id='video', title='Song', artist='Artist')
    repository.sample_conflicts.return_value = []
    repository.sample_facts.return_value = []
    repository.sample_owners.return_value = {'sample': 'run'}
    repository.annotation.return_value = None
    repository.taxonomy.return_value = True, {'cruise', 'relax'}
    samples.write.return_value = 'sample-file'
    samples.matches.return_value = True
    leased = False

    @contextmanager
    def write(document):
        nonlocal leased
        leased = True
        try:
            yield 'annotation-file', 'digest'
        finally:
            leased = False

    def publish(*args, **kwargs):
        assert leased
        return 7

    annotations.write.side_effect = write
    repository.publish_annotation.side_effect = publish
    return AudioVibes(repository, samples, annotations, prepared, metadata)


def sample(**values):
    return dict(id='sample', run_id='run', video_id='video', point='thirty', title='Song', artist='Artist', **values)


def previous(**values):
    return dict(id=7, run_id='run', legacy_run_key=None, kind='session', taxonomy_version=TAXONOMY_VERSION,
                revision=2, state='labeled', created_at=10, sample_ids=['sample'], vibes=['cruise'], **values)


def test_unreadable_samples_are_omitted_while_other_evidence_remains_usable(audio):
    references = [dict(id=str(index)) for index in range(3)]
    audio.repository.sample_facts.return_value = references
    audio.sample_files.read.side_effect = [OSError('missing'), ValueError('corrupt'), sample()]
    assert audio.samples('run') == [sample()]
    assert audio.sample_files.read.call_count == 3


def test_legacy_playback_fallback_uses_readable_sample_evidence(audio):
    audio.repository.playback.return_value = None
    audio.repository.sample_facts.return_value = [dict(id='sample')]
    audio.sample_files.read.return_value = sample()
    assert audio.playback('run') == dict(video_id='video', title='Song', artist='Artist')
    audio.repository.save_playback.assert_not_called()


def test_native_playback_avoids_loading_sample_payloads(audio):
    assert audio.playback('run')['video_id'] == 'video'
    audio.sample_files.read.assert_not_called()


def test_sample_publication_passes_a_file_reference_to_sql(audio):
    audio.save_sample(sample(), 10)
    audio.sample_files.write.assert_called_once_with(sample())
    audio.repository.save_sample.assert_called_once_with(sample(), 'sample-file', 10, expected=[])


@pytest.mark.parametrize('operation', ['playback', 'sample'])
def test_an_existing_run_cannot_be_reassigned_to_another_video(audio, operation):
    with pytest.raises(ValueError, match='different song'):
        if operation == 'sample':
            value = {**sample(), 'video_id': 'another-video'}
            audio.save_sample(value, 10)
        else:
            audio.save_playback('run', 'another-video', 'Song', 'Artist', 10)
    audio.repository.save_sample.assert_not_called()
    audio.repository.save_playback.assert_not_called()
    audio.sample_files.write.assert_not_called()


@pytest.mark.parametrize('changed', [{'id': 'different'}, {'run_id': 'another-run'}, {'video_id': 'another-video'}])
def test_sampling_point_and_sample_id_bindings_are_immutable(audio, changed):
    audio.repository.sample_conflicts.return_value = [{**sample(), 'recording_id': 3, 'filename': 'sample-file', **changed}]
    with pytest.raises(ValueError, match='already been saved|different song'):
        audio.save_sample(sample(), 10)
    audio.sample_files.write.assert_not_called()
    audio.sample_files.repair.assert_not_called()


def test_same_identity_with_changed_sample_bytes_is_rejected(audio):
    audio.repository.sample_conflicts.return_value = [{**sample(), 'recording_id': 3, 'filename': 'sample-file'}]
    audio.sample_files.matches.return_value = False
    with pytest.raises(ValueError, match='already been saved'):
        audio.save_sample(sample(), 10)
    audio.sample_files.repair.assert_not_called()


def test_identical_retry_repairs_evidence_and_retries_a_failed_queue_notification(audio):
    audio.repository.sample_conflicts.return_value = [{**sample(), 'recording_id': 3, 'filename': 'sample-file'}]
    audio.prepared_content.requeue_recording.side_effect = [RuntimeError('queue interrupted'), None]
    with pytest.raises(RuntimeError, match='queue interrupted'):
        audio.save_sample(sample(), 10)
    audio.save_sample(sample(), 10)
    assert audio.sample_files.repair.call_count == 2
    assert audio.prepared_content.requeue_recording.call_count == 2
    audio.repository.save_sample.assert_not_called()


@pytest.mark.parametrize('old_state,labels,state', [(None, [], 'empty'), (None, ['cruise'], 'labeled'),
    ('labeled', [], 'withdrawn'), ('withdrawn', [], 'withdrawn'), ('empty', [], 'empty')])
def test_annotation_state_preserves_empty_and_withdrawn_meanings(audio, old_state, labels, state):
    if old_state is not None:
        value = previous()
        value['state'] = old_state
        audio.repository.annotation.return_value = value
        audio.annotation_files.read.return_value = {'prediction': {'older': True}}
    audio.save_feedback('run', ['sample'], labels, TAXONOMY_VERSION, None, 20)
    kwargs = audio.repository.publish_annotation.call_args.kwargs
    assert kwargs['state'] == state
    assert kwargs['expected'] == (7 if old_state is not None else None)
    assert kwargs['revision'] == (3 if old_state is not None else 1)
    assert kwargs['reference'] == ('annotation-file', 'digest')


@pytest.mark.parametrize('state,labels', [('labeled', ['cruise']), ('withdrawn', []), ('empty', [])])
def test_an_identical_annotation_retry_does_not_create_a_revision(audio, state, labels):
    value = previous()
    value.update(state=state, vibes=labels)
    audio.repository.annotation.return_value = value
    audio.annotation_files.read.return_value = {'prediction': None}
    audio.save_feedback('run', ['sample'], labels, TAXONOMY_VERSION, None, 20)
    audio.annotation_files.write.assert_not_called()
    audio.repository.publish_annotation.assert_not_called()


@pytest.mark.parametrize('invalid', ['run', 'taxonomy', 'labels', 'samples'])
def test_invalid_annotation_membership_is_rejected_before_file_publication(audio, invalid):
    if invalid == 'run':
        audio.repository.playback.return_value = None
    elif invalid == 'taxonomy':
        audio.repository.taxonomy.return_value = False, set()
    elif invalid == 'labels':
        audio.repository.taxonomy.return_value = True, {'relax'}
    else:
        audio.repository.sample_owners.return_value = {'sample': 'another-run'}
    with pytest.raises(ValueError):
        audio.save_feedback('run', ['sample'], ['cruise'], TAXONOMY_VERSION, None, 20)
    audio.annotation_files.write.assert_not_called()
    audio.repository.publish_annotation.assert_not_called()


def test_feedback_displays_preserved_legacy_sample_ids_without_admitting_memberships(audio):
    audio.repository.annotation.return_value = previous()
    audio.annotation_files.read.return_value = dict(prediction=None, legacy={'sample_ids': '["unresolved"]'})
    assert audio.feedback('run') == dict(taxonomy_version=TAXONOMY_VERSION, vibes=['cruise'],
        prediction=None, updated_at=10, sample_ids=['unresolved'])
    audio.repository.publish_annotation.assert_not_called()


@pytest.mark.parametrize('values,choices,expected', [
    ([.8, .8], {}, .8), ([.8, .2], {}, None), ([True, .8], {}, .8),
    ([1.2, float('nan')], {}, None), ([.8], {'mood_aggressive': 'null'}, None),
    ([.8], {'mood_aggressive': '0.4'}, .4),
])
def test_metadata_requires_unambiguous_provider_values_and_preserves_explicit_nulls(audio, values, choices, expected):
    sources = [dict(key=index) for index in range(len(values))]
    audio.repository.base_metadata_inputs.return_value = dict(
        metadata={'entry_id': 4, 'identity_key': 'song', 'bpm': 120}, choices=choices, sources=sources)
    audio.metadata_files.source_values.side_effect = [{'mood_aggressive': {'value': value}} for value in values]
    result = audio.base_metadata('video')
    assert result == dict(entry_id=4, identity_key='song', bpm=120, mood_aggressive=expected, mood_party=None)


def test_an_unknown_video_does_not_load_unrelated_metadata_evidence(audio):
    audio.repository.base_metadata_inputs.return_value = dict(metadata={}, choices={}, sources=[])
    assert audio.base_metadata('unknown-video') == {}
    audio.metadata_files.source_values.assert_not_called()
