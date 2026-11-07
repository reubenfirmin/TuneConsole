"""Taste decisions and publication ordering use mocked domain dependencies."""
from contextlib import nullcontext
import json
import struct
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.embeddings import Embeddings
from yt_playlist.controllers.genres import Genres
from yt_playlist.controllers.player_events import PlayerEvents
from yt_playlist.controllers.rec_model import RecommendationModel
from yt_playlist.core import collection_recovery, embeddings
from yt_playlist.core.rating_feedback import Rating
from yt_playlist.core.rec_model_policy import VectorChanges
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.settings import SettingsRepo

pytestmark = pytest.mark.usefixtures('no_database')
DAY = 86400
VECTOR = struct.pack('f', 1)
MODEL = json.dumps({'ncat': 1, 'cont': []})


@pytest.fixture
def model():
    repo = create_autospec(RecModelRepo, instance=True, spec_set=True)
    settings = create_autospec(SettingsRepo, instance=True, spec_set=True)
    events = create_autospec(PlayerEvents, instance=True, spec_set=True)
    genres = create_autospec(Genres, instance=True, spec_set=True)
    vectors = create_autospec(Embeddings, instance=True, spec_set=True)
    recovery = create_autospec(collection_recovery.RecoveryFiles, instance=True, spec_set=True)
    vectors.retained.side_effect = recovery.retained.side_effect = nullcontext
    settings.get_setting.return_value = None
    repo.weight_records.return_value = {}
    return RecommendationModel(repo, settings, events, genres, vectors, recovery, nullcontext)


@pytest.mark.parametrize('polarity', ['LIKE', 'DISLIKE'])
@pytest.mark.parametrize('association', [None, 20])
def test_association_keeps_earliest_feedback_expiry_and_graduation_guard(model, polarity, association):
    kind = polarity.lower()
    surface = 'like' if kind == 'like' else 'sync'
    current = dict(surface=surface, item_key='target', kind=kind, scope='', reason='sync', created_at=30, until=90)
    donor = {**current, 'item_key': 'old', 'created_at': 10, 'until': 50}
    guard = {**donor, 'surface': 'like_grad', 'kind': 'like_grad', 'reason': 'learned'}
    model.repository.rating_rows.return_value = [current]
    model.reassociate_rating_feedback({'target': Rating(polarity, 100, 'action')}, {'target': [donor, guard]}, association=association)
    model.repository.publish_association_ratings.assert_called_once_with(
        [('target', kind)], [('target',)] if kind == 'like' else [], [],
        [(10, 50, surface, 'target')], [('target', 'learned', 10)], association=association)
    model.repository.publish_ratings.assert_not_called()


def test_association_does_not_manufacture_feedback_for_pending_ratings(model):
    model.repository.rating_rows.return_value = []
    model.reassociate_rating_feedback({'target': Rating('DISLIKE', 100)}, {'target': []})
    model.repository.publish_association_ratings.assert_called_once_with([('target', 'dislike')], [], [], [], [], association=None)


def test_weight_reads_revert_by_age_preserving_mutes_and_unknown_age(model):
    model.repository.weight_records.return_value = {
        'lane:rotation': {'weight': 2., 'updated_at': 0},
        'genre:jazz': {'weight': 0., 'updated_at': 0},
        'genre:rock': {'weight': 2.5, 'updated_at': None}}
    assert model.get_weights(now=60 * DAY) == {'lane:rotation': 1.5, 'genre:jazz': 0., 'genre:rock': 2.5}
    model.repository.put_weight.assert_not_called()


@pytest.mark.parametrize('factor,expected', [(2., 3.), (.1, .2), (1., 1.5)])
def test_nudge_uses_reverted_value_and_clamps_before_write(model, factor, expected):
    model.repository.weight_record.return_value = {'weight': 2., 'updated_at': 0}
    assert model.nudge_weight('lane:rotation', factor, now=60 * DAY) == expected
    model.repository.put_weight.assert_called_once_with('lane:rotation', expected, 60 * DAY)


def test_nudge_never_rearms_explicit_genre_mute(model):
    model.repository.weight_record.return_value = {'weight': 0., 'updated_at': 0}
    assert model.nudge_weight('genre:jazz', 2., now=1000 * DAY) == 0.
    model.repository.put_weight.assert_not_called()


def test_suppression_combines_feedback_skips_and_genre_family(model):
    model.repository.feedback_suppression.return_value = {'dismissed'}
    model.player_events.skip_blackouts.return_value = {'skipped'}
    model.repository.weight_records.return_value = {'genre:electronic': {'weight': 0.}}
    model.repository.genre_rows.return_value = [('house-track', 'House'), ('rock-track', 'Rock')]
    model.genres.feature_axes.return_value = {'House': ('house', 'electronic'), 'Rock': ('rock', None)}
    keys = ['dismissed', 'skipped', 'house-track', 'rock-track']
    assert model.suppressed_keys('home', 12, keys=keys + keys) == {'dismissed', 'skipped', 'house-track'}
    model.player_events.skip_blackouts.assert_called_once_with(12, keys=keys)
    model.repository.genre_rows.assert_called_once_with(keys)


def test_empty_suppression_cohort_never_scans(model):
    assert model.suppressed_keys('home', 12, keys=[]) == set()
    assert not model.repository.mock_calls and not model.player_events.mock_calls


def test_rating_reobservations_preserve_age_expiry_and_graduation(model):
    model.repository.rating_rows.return_value = [
        {'surface': 'like', 'item_key': 'liked', 'kind': 'like', 'scope': '', 'created_at': 1, 'until': None},
        {'surface': 'sync', 'item_key': 'banned', 'kind': 'dislike', 'scope': '', 'created_at': 2, 'until': 10}]
    states = {'liked': Rating('LIKE', 100, 'action'), 'banned': Rating('DISLIKE', 100),
              'neutral': Rating('INDIFFERENT', 100), 'new': Rating('LIKE', 100)}
    assert model.apply_rating_feedback(states, dislike_seconds=50) == {'new': states['new']}
    model.repository.publish_ratings.assert_called_once_with(
        [('liked', 'like'), ('banned', 'dislike'), ('neutral', 'indifferent'), ('new', 'like')],
        [('liked',)], [('like', 'new', 'like', 'sync', None, 100)])


def test_unban_covers_retained_api_dislike_after_clock_moves_backwards(model):
    model.repository.unban_facts.return_value = ('canonical', 42, 200)
    model.unban('old-key', 100)
    model.repository.publish_unban.assert_called_once_with('canonical', 42, 200)


def test_checkpoint_writes_file_before_selecting_sql_reference(model):
    model.repository.checkpoint_facts.return_value = {'typed': True, 'header': None, 'weights': []}
    model.repository.parameter_values.return_value = {'rec_param:x': '.5'}
    model.recovery.publish.return_value = ('file', 'digest', 12)
    order = Mock()
    order.attach_mock(model.recovery.publish, 'file')
    order.attach_mock(model.repository.publish_checkpoint, 'sql')
    model.save_taste_settings('taste_settings_checkpoint_v1', ['rec_param:x'], 12)
    assert [call[0] for call in order.mock_calls] == ['file', 'sql']
    model.recovery.publish.assert_called_once_with('taste-checkpoint', 1, {'rec_param:x': '.5'})


def test_checkpoint_file_failure_leaves_existing_sql_reference(model):
    model.repository.checkpoint_facts.return_value = {'typed': True, 'header': None, 'weights': []}
    model.recovery.publish.side_effect = OSError('storage unavailable')
    with pytest.raises(OSError):
        model.save_taste_settings('taste_settings_checkpoint_v1', [], 12)
    model.repository.publish_checkpoint.assert_not_called()


def test_restore_rejects_merged_axes_before_changing_settings_or_weights(model):
    model.repository.checkpoint_facts.return_value = {'typed': True,
        'header': {'recovery_filename': 'file', 'recovery_digest': 'hash', 'recovery_bytes': 12},
        'weights': [{'position': 0, 'captured_axis': 'genre:first'}, {'position': 1, 'captured_axis': 'genre:second'}]}
    model.recovery.read.return_value = {}
    model.repository.checkpoint_axes.return_value = [(7, .3, 1), (7, .4, 2)]
    with pytest.raises(ValueError, match='Merged checkpoint axes'):
        model.restore_taste_settings('taste_settings_checkpoint_v1', [])
    model.settings.replace_keys.assert_not_called()
    model.repository.replace_weights.assert_not_called()


def test_training_delta_omits_unchanged_rows_and_preserves_discovery_order(model):
    model.vectors.head.return_value = ('basis', 1)
    model.vectors.rows.return_value = [('a', VECTOR), ('c', VECTOR)]
    model.vectors.prepare.return_value = object()
    assert model.prepare_training_changes({'rec_vectors': [('a', VECTOR), ('c', VECTOR)]}) == {}
    changes = model.prepare_training_changes({'rec_discovered_content_vectors': [('a', VECTOR), ('b', VECTOR), ('c', VECTOR)]})
    assert changes['rec_discovered_content_vectors'].replace
    assert changes['rec_discovered_content_vectors'].upserts == (('a', VECTOR), ('b', VECTOR), ('c', VECTOR))


def test_training_publication_validates_all_owner_names_before_preparing_files(model):
    with pytest.raises(ValueError, match='model table'):
        model.publish_training_generation({'rec_vectors': VectorChanges((('a', VECTOR),), ()),
                                           'settings': VectorChanges((), ())})
    model.vectors.prepare.assert_not_called()
    model.vectors.publish.assert_not_called()


def test_content_publication_prepares_both_pools_then_sql_then_generation(model):
    model.settings.get_setting.return_value = 'old'
    model.vectors.prepare.side_effect = ['library', 'discovered']
    order = Mock()
    order.attach_mock(model.vectors.prepare, 'file')
    order.attach_mock(model.vectors.publish, 'sql')
    order.attach_mock(model.settings.publish_values, 'head')
    assert model.publish_content_generation([('a', VECTOR)], [], MODEL, 'rev', 'inputs', 'old')
    assert [call[0] for call in order.mock_calls] == ['file', 'file', 'sql', 'head']
    assert model.settings.publish_values.call_args.args[0]['rec_content_generation'] != 'old'


def test_stale_content_publication_never_selects_its_files(model):
    model.settings.get_setting.return_value = 'newer'
    assert not model.publish_content_generation([('a', VECTOR)], [], MODEL, 'rev', '', 'old')
    model.vectors.publish.assert_not_called()
    model.settings.publish_values.assert_not_called()


def test_second_content_file_failure_never_changes_sql_or_head(model):
    model.vectors.prepare.side_effect = ['library', OSError('full')]
    with pytest.raises(OSError):
        model.publish_content_generation([('a', VECTOR)], [], MODEL, 'rev', '', '')
    model.vectors.publish.assert_not_called()
    model.settings.publish_values.assert_not_called()


def test_content_reads_hide_vectors_from_a_different_encoder(model):
    model.settings.get_setting.side_effect = lambda key: MODEL if key == 'rec_content_model' else None
    basis = embeddings.version('rec_content_vectors', {'rec_content_model': MODEL})
    model.vectors.head.side_effect = [(basis, 1), ('obsolete', 1)]
    model.vectors.rows.side_effect = [[('a', VECTOR)], [('b', VECTOR)]]
    result = model.content_generation()
    assert result['library'] == [('a', VECTOR)] and result['discovered'] == []
    assert not result['coherent']
