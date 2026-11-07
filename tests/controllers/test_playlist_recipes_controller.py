"""Saved recipe and append policy use mocked file and SQL owners."""
from contextlib import nullcontext
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.playlist_recipes import PlaylistRecipes
from yt_playlist.core.playlist_files import PlaylistFiles
from yt_playlist.repos.playlist_recipes import PlaylistRecipesRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def recipes():
    repo = create_autospec(PlaylistRecipesRepo, instance=True, spec_set=True)
    files = create_autospec(PlaylistFiles, instance=True, spec_set=True)
    files.retained.side_effect = nullcontext
    repo.document_owner.return_value = {'native': True, 'scope': 'TV', 'id': 8}
    files.publish.return_value = ('file', 'digest', 12)
    return PlaylistRecipes(repo, files, nullcontext)


def test_catalog_rekey_changes_declared_keys_and_preserves_labels_media_and_created_time(recipes):
    recipes.repository.document_created_ats.side_effect = [{'scope': 10}, {'canvas': 20}]
    original = {'keys': ['old'], 'label': 'old', 'video_id': 'old'}
    recipes.get_recipe = Mock(return_value=original)
    recipes.get_cluster_canvas = Mock(return_value=json.dumps({'nodes': [{'key': 'old', 'title': 'old'}]}))
    recipes.set_recipe, recipes.set_cluster_canvas = Mock(), Mock()
    recipes.rekey_recordings({'old': 'new'})
    args = recipes.set_recipe.call_args.args
    assert args[0] == 'scope' and args[2] == 10
    assert args[1]['keys'] == ['new'] and args[1]['label'] == args[1]['video_id'] == 'old'
    assert original['keys'] == ['old']
    scope, raw, created = recipes.set_cluster_canvas.call_args.args
    assert (scope, created) == ('canvas', 20)
    assert json.loads(raw)['nodes'] == [{'key': 'new', 'title': 'old', 'catalog_identity_version': 2}]


def test_catalog_rekey_preserves_future_document_formats(recipes):
    recipes.repository.document_created_ats.side_effect = [{'scope': 10}, {}]
    recipes.get_recipe = Mock(return_value={'catalog_state_version': 999, 'keys': ['old']})
    recipes.set_recipe = Mock()
    recipes.rekey_recordings({'old': 'new'})
    recipes.set_recipe.assert_not_called()


def test_recipe_and_append_files_exist_before_sql_publication(recipes):
    recipes.repository.append_owner.return_value = {'id': 9, 'prior': None}
    checkpoint = {'request_id': 'request', 'state': 'pending', 'at': 10,
                  'before': ['one', 'one'], 'additions': [{'video_id': 'two'}], 'items': []}
    value = {'media': 'video', 'facets': {'genres': ['house', 'house'], 'eras': ['2010']},
             'tv_stream': 'stream', 'tv_refill': checkpoint}
    order = Mock()
    order.attach_mock(recipes.files.publish, 'file')
    order.attach_mock(recipes.repository.publish_recipe, 'sql')
    recipes.set_recipe('TV', value, 10)
    assert [call[0] for call in order.mock_calls] == ['file', 'file', 'sql']
    args = recipes.repository.publish_recipe.call_args.args
    assert args[3] == [('genre', 0, 'genre:house', 'house'), ('genre', 1, 'genre:house', 'house'),
                       ('era', 0, 'era:2010', '2010')]
    assert args[5]['roles'] == {'before': ['one', 'one'], 'additions': ['two'], 'items': []}


@pytest.mark.parametrize('value', [None, {'facets': []}, {'facets': {'genres': 'house'}}, {'facets': {'genres': ['']}}])
def test_invalid_recipe_does_not_write_files_or_select_sql_references(recipes, value):
    with pytest.raises(ValueError):
        recipes.set_recipe('TV', value, 10)
    recipes.files.publish.assert_not_called()
    recipes.repository.publish_recipe.assert_not_called()


def test_completed_append_cannot_become_pending(recipes):
    recipes.repository.append_owner.return_value = {'id': 9, 'prior': {'state': 'complete', 'started_at': 1}}
    with pytest.raises(ValueError, match='completed TV append'):
        recipes.set_recipe('TV', {'tv_stream': 'stream', 'tv_refill': {'request_id': 'request', 'state': 'pending'}})
    recipes.files.publish.assert_not_called()
    recipes.repository.publish_recipe.assert_not_called()


def test_second_file_failure_leaves_original_recipe_and_append_selected(recipes):
    recipes.repository.append_owner.return_value = {'id': 9, 'prior': None}
    recipes.files.publish.side_effect = [('file', 'digest', 12), OSError('full')]
    with pytest.raises(OSError):
        recipes.set_recipe('TV', {'tv_stream': 'stream', 'tv_refill': {'request_id': 'request', 'state': 'pending'}})
    recipes.repository.publish_recipe.assert_not_called()


def test_recipe_projects_current_facets_and_preserves_opaque_captured_fields(recipes):
    header = {'id': 8, 'media': 'video', 'model': 'current', 'recovery_filename': 'file',
              'recovery_digest': 'digest', 'recovery_bytes': 12}
    recipes.repository.document_facts.return_value = {'scope': 'TV', 'native': True, 'header': header,
                                                      'facets': [('genre', 'new-key'), ('genre', 'new-key')]}
    recipes.files.read.return_value = {'media': 'audio', 'model': 'old', 'facets': {'genres': ['old-key', 'old-key'],
        'eras': None}, 'opaque': {'original': [2, 1]}}
    recipes.repository.append_facts.return_value = None
    assert recipes.get_recipe('TV') == {'media': 'video', 'model': 'current',
        'facets': {'genres': ['new-key', 'new-key'], 'eras': None}, 'opaque': {'original': [2, 1]}}


def test_native_append_projection_preserves_duplicate_order_and_metadata(recipes):
    recipes.repository.document_facts.return_value = {'scope': 'TV', 'native': False, 'prepared': False,
        'raw': '{"tv_stream":"stream","tv_refill":{"request_id":"request"}}'}
    recipes.repository.append_facts.return_value = {'native': True, 'header': {'id': 9, 'request_id': 'request',
        'state': 'complete', 'started_at': 1, 'stream_token': 'stream', 'recovery_filename': 'file',
        'recovery_digest': 'digest', 'recovery_bytes': 12}, 'roles': {'before': ['one', 'one'], 'items': ['two', 'two']}}
    recipes.files.read.return_value = {'at': 99, 'before': ['obsolete'], 'items': [{'video_id': 'two', 'title': 'Saved'}]}
    checkpoint = recipes.get_recipe('TV')['tv_refill']
    assert checkpoint['at'] == 1 and checkpoint['state'] == 'complete'
    assert checkpoint['before'] == ['one', 'one']
    assert checkpoint['items'] == [{'video_id': 'two', 'title': 'Saved'}] * 2


def test_missing_append_intent_is_not_reported_as_a_complete_recipe(recipes):
    recipes.repository.document_facts.return_value = {'scope': 'TV', 'native': False, 'prepared': False, 'raw': '{}'}
    recipes.repository.append_facts.return_value = {'native': True, 'header': None, 'roles': {}}
    with pytest.raises(ValueError, match='no retained intent'):
        recipes.get_recipe('TV')


def test_canvas_file_failure_leaves_original_sql_reference(recipes):
    recipes.files.publish.side_effect = OSError('full')
    with pytest.raises(OSError):
        recipes.set_cluster_canvas('TV', 'verbatim canvas')
    recipes.repository.publish_canvas.assert_not_called()


def test_prepared_original_canvas_is_read_without_reencoding(recipes):
    recipes.repository.document_facts.return_value = {'scope': 'TV', 'native': False, 'prepared': True,
                                                      'header': {'source': 'TV'}, 'parts': ['parts']}
    recipes.files.read_original.return_value = 'not JSON; keep this text\n'
    assert recipes.get_cluster_canvas('TV') == 'not JSON; keep this text\n'
    recipes.files.read_original.assert_called_once_with({'source': 'TV'}, ['parts'])
