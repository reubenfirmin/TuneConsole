"""Genre matching and enabled vocabulary over one taxonomy repository."""
import json

from yt_playlist.core.genre_identity import axes_from_matches
from yt_playlist.library import genre_policy
from yt_playlist.util import genre_catalog


class Genres:
    def __init__(self, repository):
        self.repository = repository

    def taxonomy(self):
        return genre_policy.taxonomy(self.repository.taxonomy_facts())

    def resolve(self, label):
        if label is None or label == '':
            return None, None
        if not isinstance(label, str):
            raise ValueError('A genre label must be text or null')
        matches = self.repository.label_matches(label, genre_catalog.normalize(label))
        if len(matches) == 1:
            return next(iter(matches)), None
        return None, 'ambiguous_genre' if matches else 'unknown_genre'

    def enabled_matcher(self):
        return genre_policy.matcher(self.repository.matcher_facts())

    def _admit(self, name):
        genre_policy.validate_name(name, custom=True)
        resolved, reason = self.resolve(name)
        key = genre_policy.custom_key(name, resolved, reason)
        if resolved is None:
            if self.repository.genre_name(key) is not None:
                raise ValueError('Custom genre key collides with an existing identity: ' + name)
            self.repository.insert_custom(key, name)
        return key

    def configure_catalog(self, *, reset=False):
        records, labels = genre_catalog.catalog()
        families = genre_catalog.family_labels()
        with self.repository.transaction():
            current = self.repository.get_genre_whitelist()
            target, _, defaults, upgrading = genre_policy.configuration(
                current, self.repository.configuration_settings(), reset=reset)
            if upgrading:
                self.repository.seed_catalog(records, labels, families)
            matches = self.repository.axis_matches(
                [(name, genre_catalog.normalize(name)) for name in target])
            keys = set()
            for name in target:
                genre_policy.validate_name(name, custom=True)
                choices = matches[name]
                if len(choices) == 1:
                    keys.add(next(iter(choices)))
                else:
                    keys.add(self._admit(name))
            self.repository.configure_preferences(
                [(key, 'default' if key in records else 'custom') for key in keys], list(records))
            self.repository.save_configuration({
                'genres_seeded': '1', 'genre_catalog_revision': genre_catalog.REVISION,
                'genre_defaults': json.dumps(defaults),
            })

    def set_genres(self, names):
        names = list(names)
        with self.repository.transaction():
            self.repository.replace_preferences({self._admit(name) for name in names}, origin='user')

    def add_genre(self, name):
        with self.repository.transaction():
            self.repository.save_preference(self._admit(name), enabled=True, origin='user')

    def remove_genre(self, name):
        with self.repository.transaction():
            key, reason = self.resolve(name)
            if reason == 'ambiguous_genre':
                raise ValueError('Ambiguous genre label: ' + name)
            if key is not None:
                self.repository.save_preference(key, enabled=False, origin='user')

    def feature_axes(self, labels):
        labels = list(dict.fromkeys(label for label in labels if label))
        if any(not isinstance(label, str) for label in labels):
            raise ValueError('A genre label must be text or null')
        prepared = [(label, genre_catalog.normalize(label)) for label in labels]
        return axes_from_matches(self.repository.axis_matches(prepared))

    def family_members(self, families):
        return self.repository.family_members(families)

    def get_genre_whitelist(self):
        return self.repository.get_genre_whitelist()

    def all_genres(self):
        return self.repository.all_genres()
