from contextlib import nullcontext
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.genres import Genres
from yt_playlist.repos.genres import GenreRepo
from yt_playlist.util import genre_catalog

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def genres(monkeypatch):
    defaults = ['Rock', 'Jazz', 'New']
    records = {name.lower(): {'key': name.lower(), 'name': name} for name in defaults}
    monkeypatch.setattr(genre_catalog, 'catalog', lambda: (records, {}))
    monkeypatch.setattr(genre_catalog, 'builtin_names', lambda: defaults)
    monkeypatch.setattr(genre_catalog, 'legacy_names', lambda: ['Rock', 'Jazz'])
    monkeypatch.setattr(genre_catalog, 'legacy_entries', lambda: [('Rock', []), ('Jazz', ['Jazz alias'])])
    monkeypatch.setattr(genre_catalog, 'family_labels', lambda: {})
    monkeypatch.setattr(genre_catalog, 'canonical_name',
        lambda name: next((value for value in defaults if value.casefold() == name.casefold()), name))
    repository = create_autospec(GenreRepo, instance=True, spec_set=True)
    repository.transaction.side_effect = nullcontext
    repository.uses_keyed_preferences.return_value = True
    repository.label_matches.side_effect = lambda label, normalized: {normalized}
    repository.axis_matches.side_effect = lambda labels: {
        label: {normalized: None} for label, normalized in labels}
    repository.genre_name.return_value = None
    repository.get_genre_whitelist.return_value = ['Jazz', 'Custom']
    repository.configuration_settings.return_value = {'genres_seeded': '1', 'genre_catalog_revision': 'old',
                                                     'genre_defaults': json.dumps(['Rock', 'Jazz'])}
    return Genres(repository)


def test_custom_addition_resolves_before_admitting_a_new_identity(genres):
    genres.repository.label_matches.side_effect = None
    genres.repository.label_matches.return_value = set()
    genres.add_genre('Custom Style')
    genres.repository.insert_custom.assert_called_once_with('custom style', 'Custom Style')
    genres.repository.save_preference.assert_called_once_with('custom style', enabled=True, origin='user')


@pytest.mark.parametrize('name,reason,existing,error', [
    ('Ambiguous', 'ambiguous_genre', None, 'Ambiguous'),
    ('...', 'unknown_genre', None, 'nonempty identity'),
    ('Collision', 'unknown_genre', 'Another display name', 'collides'),
])
def test_invalid_custom_identity_cannot_change_preferences(genres, name, reason, existing, error):
    genres.repository.label_matches.side_effect = None
    genres.repository.label_matches.return_value = {'first', 'second'} if reason == 'ambiguous_genre' else set()
    genres.repository.genre_name.return_value = existing
    with pytest.raises(ValueError, match=error):
        genres.add_genre(name)
    genres.repository.insert_custom.assert_not_called()
    genres.repository.save_preference.assert_not_called()


def test_missing_removal_is_harmless_but_ambiguous_removal_rejects(genres):
    genres.repository.label_matches.side_effect = [set(), {'first', 'second'}]
    genres.remove_genre('Missing')
    with pytest.raises(ValueError, match='Ambiguous'):
        genres.remove_genre('Ambiguous')
    genres.repository.save_preference.assert_not_called()


def test_catalog_upgrade_preserves_removals_and_custom_additions(genres):
    genres.configure_catalog()
    enabled, default_keys = genres.repository.configure_preferences.call_args.args
    assert set(enabled) == {('jazz', 'default'), ('new', 'default'), ('custom', 'custom')}
    assert set(default_keys) == {'rock', 'jazz', 'new'}
    genres.repository.seed_catalog.assert_called_once()


def test_reset_restores_defaults_and_drops_custom_membership(genres):
    genres.configure_catalog(reset=True)
    enabled, _default_keys = genres.repository.configure_preferences.call_args.args
    assert set(enabled) == {('rock', 'default'), ('jazz', 'default'), ('new', 'default')}


def test_current_catalog_does_not_resurrect_removed_defaults(genres):
    genres.repository.configuration_settings.return_value['genre_catalog_revision'] = genre_catalog.REVISION
    genres.configure_catalog()
    enabled, _default_keys = genres.repository.configure_preferences.call_args.args
    assert set(enabled) == {('jazz', 'default'), ('custom', 'custom')}
    genres.repository.seed_catalog.assert_not_called()


def test_explicit_empty_legacy_choice_survives_upgrade(genres):
    genres.repository.get_genre_whitelist.return_value = []
    genres.configure_catalog()
    assert genres.repository.configure_preferences.call_args.args[0] == []


def test_catalog_configuration_batches_known_labels_and_still_admits_custom_names(genres):
    genres.repository.axis_matches.side_effect = None
    genres.repository.axis_matches.return_value = {
        'Jazz': {'jazz': None}, 'New': {'new': None}, 'Custom': {}}
    genres.repository.label_matches.side_effect = lambda *_args: set()
    genres.configure_catalog()
    assert set(genres.repository.axis_matches.call_args.args[0]) == {
        ('Jazz', 'jazz'), ('New', 'new'), ('Custom', 'custom')}
    genres.repository.label_matches.assert_called_once_with('Custom', 'custom')
    genres.repository.insert_custom.assert_called_once_with('custom', 'Custom')
    enabled, _defaults = genres.repository.configure_preferences.call_args.args
    assert set(enabled) == {('jazz', 'default'), ('new', 'default'), ('custom', 'custom')}


def test_ambiguous_catalog_label_cannot_publish_preferences(genres):
    genres.repository.axis_matches.side_effect = None
    genres.repository.axis_matches.return_value = {
        'Jazz': {'jazz': None, 'other': None}, 'New': {'new': None}, 'Custom': {'custom': None}}
    genres.repository.label_matches.side_effect = lambda *_args: {'jazz', 'other'}
    with pytest.raises(ValueError, match='Ambiguous genre label'):
        genres.configure_catalog()
    genres.repository.configure_preferences.assert_not_called()
    genres.repository.save_configuration.assert_not_called()


def test_matcher_excludes_disabled_and_ambiguous_labels_and_follows_redirects(genres):
    genres.repository.matcher_facts.return_value = {
        'enabled': {'rock': 'Renamed Rock', 'jazz': 'Jazz'}, 'redirects': {'old-rock': 'rock'},
        'labels': [('Rock Alias', 'old-rock'), ('Rock Alias', 'rock'), ('Ambiguous', 'rock'),
                   ('Ambiguous', 'jazz'), ('Disabled', 'disabled'), ('Jazz', 'jazz')],
    }
    assert genres.enabled_matcher() == {'rock alias': 'Renamed Rock', 'jazz': 'Jazz'}


def test_missing_preference_format_is_distinct_from_an_explicit_empty_matcher(genres):
    genres.repository.matcher_facts.side_effect = [None, {'enabled': {}, 'redirects': {}, 'labels': [('Rock', 'rock')]}]
    assert genres.enabled_matcher() is None
    assert genres.enabled_matcher() == {}


def test_taxonomy_hides_retired_concepts_and_spelling_aliases(genres):
    genres.repository.taxonomy_facts.return_value = {
        'families': {'rock': 'Rock'}, 'redirects': {'retired': 'rock'},
        'genres': [('rock', 'Rock', 'rock'), ('retired', 'Retired', None), ('custom', 'Custom', None)],
        'aliases': [('rock', 'Rock', 'custom'), ('rock', 'ROCK', 'spelling'),
                    ('retired', 'Old Rock', 'custom'), ('rock', 'Old Rock', 'custom')],
    }
    assert genres.taxonomy() == {'families': {'rock': 'Rock'}, 'genres': [
        {'key': 'rock', 'name': 'Rock', 'family': 'rock', 'aliases': ['Old Rock']},
        {'key': 'custom', 'name': 'Custom', 'family': 'other:custom', 'aliases': []},
    ]}


def test_model_axes_reject_ambiguity_and_use_catalog_family_changes(genres, monkeypatch):
    from yt_playlist.util import genre_family_data
    fallback = Mock(return_value='rock-classic')
    monkeypatch.setattr(genre_family_data, 'family', fallback)
    genres.repository.axis_matches.side_effect = None
    genres.repository.axis_matches.return_value = {
        'Alias': {'renamed': 'jazz'}, 'Ambiguous': {'rock': 'rock', 'jazz': 'jazz'},
        'Custom': {'custom': None}, 'Rock': {},
    }
    assert genres.feature_axes(['Alias', 'Ambiguous', 'Custom', 'Rock']) == {
        'Alias': ('jazz', 'renamed'), 'Ambiguous': ('other:ambiguous', None),
        'Custom': ('other:custom', 'custom'), 'Rock': ('rock-classic', None),
    }
    fallback.assert_called_once_with('Rock')
    genres.repository.axis_matches.assert_called_once_with(
        [('Alias', 'alias'), ('Ambiguous', 'ambiguous'), ('Custom', 'custom'), ('Rock', 'rock')])
