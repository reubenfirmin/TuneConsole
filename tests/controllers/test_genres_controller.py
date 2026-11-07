from contextlib import nullcontext
import json
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.genres import Genres
from yt_playlist.repos.genres import GenreRepo
from yt_playlist.repos.discovery import DiscoveryRepo
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
    repository.current_key.side_effect = lambda key: key
    repository.resolve.side_effect = lambda name: (genre_catalog.normalize(name), None)
    repository.genre_name.return_value = None
    repository.alias_conflicts.return_value = False
    repository.get_genre_whitelist.return_value = ['Jazz', 'Custom']
    repository.configuration_settings.return_value = {'genres_seeded': '1', 'genre_catalog_revision': 'old',
                                                     'genre_defaults': json.dumps(['Rock', 'Jazz'])}
    return Genres(repository, create_autospec(DiscoveryRepo, instance=True, spec_set=True))


@pytest.mark.parametrize('name', [None, '', '  ', 'é' * 513])
def test_invalid_rename_does_not_start_a_write(genres, name):
    with pytest.raises(ValueError, match='nonblank text'):
        genres.rename_genre('rock', name)
    genres.repository.transaction.assert_not_called()


def test_rename_preserves_original_alias_and_invalidates_after_write(genres):
    genres.repository.genre_name.return_value = 'Original Name'
    genres.repository.resolve.side_effect = None
    genres.repository.resolve.return_value = ('rock', None)
    genres.rename_genre('rock', 'New Name')
    genres.repository.rename.assert_called_once_with('rock', 'New Name', old_label_key='original name',
                                                     old_label='Original Name')
    genres.repository.invalidate.assert_called_once_with()


@pytest.mark.parametrize('resolution', [('another', None), (None, 'ambiguous_genre')])
def test_rename_cannot_capture_another_identity_or_ambiguous_name(genres, resolution):
    genres.repository.genre_name.return_value = 'Original'
    genres.repository.resolve.side_effect = None
    genres.repository.resolve.return_value = resolution
    with pytest.raises(ValueError, match='conflicts'):
        genres.rename_genre('rock', 'Taken')
    genres.repository.rename.assert_not_called()
    genres.repository.invalidate.assert_not_called()


def test_alias_conflict_cannot_change_identity_or_invalidate(genres):
    genres.repository.genre_name.return_value = 'Rock'
    genres.repository.alias_conflicts.return_value = True
    with pytest.raises(ValueError, match='Alias conflicts'):
        genres.set_genre_alias('Other', 'rock')
    genres.repository.save_alias.assert_not_called()
    genres.repository.invalidate.assert_not_called()


def test_custom_addition_resolves_before_admitting_a_new_identity(genres):
    genres.repository.resolve.side_effect = None
    genres.repository.resolve.return_value = (None, 'unknown_genre')
    genres.add_genre('Custom Style')
    genres.repository.insert_custom.assert_called_once_with('custom style', 'Custom Style')
    genres.repository.save_preference.assert_called_once_with('custom style', enabled=True, origin='user')


@pytest.mark.parametrize('name,reason,existing,error', [
    ('Ambiguous', 'ambiguous_genre', None, 'Ambiguous'),
    ('...', 'unknown_genre', None, 'nonempty identity'),
    ('Collision', 'unknown_genre', 'Another display name', 'collides'),
])
def test_invalid_custom_identity_cannot_change_preferences(genres, name, reason, existing, error):
    genres.repository.resolve.side_effect = None
    genres.repository.resolve.return_value = (None, reason)
    genres.repository.genre_name.return_value = existing
    with pytest.raises(ValueError, match=error):
        genres.add_genre(name)
    genres.repository.insert_custom.assert_not_called()
    genres.repository.save_preference.assert_not_called()


def test_missing_removal_is_harmless_but_ambiguous_removal_rejects(genres):
    genres.repository.resolve.side_effect = [(None, 'unknown_genre'), (None, 'ambiguous_genre')]
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
    genres.repository.axis_matches.return_value = {
        'Alias': {'renamed': 'jazz'}, 'Ambiguous': {'rock': 'rock', 'jazz': 'jazz'},
        'Custom': {'custom': None}, 'Rock': {},
    }
    assert genres.feature_axes(['Alias', 'Ambiguous', 'Custom', 'Rock']) == {
        'Alias': ('jazz', 'renamed'), 'Ambiguous': ('other:ambiguous', None),
        'Custom': ('other:custom', 'custom'), 'Rock': ('rock-classic', None),
    }
    fallback.assert_called_once_with('Rock')


@pytest.mark.parametrize('preferences,expected', [
    ([], None),
    ([{'enabled': 0, 'origin': 'default'}, {'enabled': 1, 'origin': 'user'}], (1, 'user')),
    ([{'enabled': 1, 'origin': 'user'}, {'enabled': 0, 'origin': 'custom'}], (0, 'user')),
    ([{'enabled': 1, 'origin': 'default'}, {'enabled': 0, 'origin': 'default'}], (0, 'default')),
])
def test_merge_preference_is_explicit_and_discovery_finishes_before_taxonomy_write(genres, preferences, expected):
    from contextlib import contextmanager
    writing = False
    calls = []

    @contextmanager
    def transaction():
        nonlocal writing
        assert not writing
        writing = True
        try:
            yield
        finally:
            writing = False

    def discovery(previous, target):
        assert not writing
        calls.append('discovery')

    def taxonomy(*args, **kwargs):
        assert writing
        calls.append('taxonomy')

    genres.repository.transaction.side_effect = transaction
    genres.repository.genre_name.return_value = 'Known'
    genres.repository.genre_axis.return_value = None
    genres.repository.preferences.return_value = preferences
    genres.repository.apply_merge.side_effect = taxonomy
    genres.discovery.merge_genre_origins.side_effect = discovery
    genres.merge_genres('previous', 'target', now=100)
    assert calls == ['discovery', 'taxonomy']
    genres.repository.apply_merge.assert_called_once_with('previous', 'target', expected,
        old_axis=None, new_axis=None, now=100)


def test_conflicting_axes_reject_before_any_discovery_or_taxonomy_write(genres):
    genres.repository.genre_name.return_value = 'Known'
    genres.repository.genre_axis.return_value = 1
    genres.repository.family_exists.return_value = False
    genres.repository.axis_id.return_value = 2
    genres.repository.axis_counts.return_value = {'rec_lean': 2}
    with pytest.raises(ValueError, match='Conflicting current taste axes in rec_lean'):
        genres.merge_genres('previous', 'target')
    genres.discovery.merge_genre_origins.assert_not_called()
    genres.repository.transaction.assert_not_called()
    genres.repository.apply_merge.assert_not_called()


def test_merge_creates_the_broad_family_axis_when_the_target_key_is_a_family(genres):
    genres.repository.genre_name.return_value = 'Known'
    genres.repository.genre_axis.return_value = 1
    genres.repository.family_exists.return_value = True
    genres.repository.axis_id.return_value = None
    genres.repository.create_axis.return_value = 2
    genres.repository.preferences.return_value = []
    genres.merge_genres('previous', 'jazz', now=100)
    genres.repository.create_axis.assert_called_once_with('family', 'jazz')
    genres.repository.apply_merge.assert_called_once_with('previous', 'jazz', None, old_axis=1, new_axis=2, now=100)
