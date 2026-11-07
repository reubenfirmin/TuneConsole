"""Genre edits and vocabulary configuration over one taxonomy repository."""
import json
import time

from yt_playlist.core.genre_identity import axes_from_matches
from yt_playlist.library import genre_policy
from yt_playlist.util import genre_catalog


class Genres:
    def __init__(self, repository, discovery):
        self.repository = repository
        self.discovery = discovery

    def taxonomy(self):
        return genre_policy.taxonomy(self.repository.taxonomy_facts())

    def enabled_matcher(self):
        return genre_policy.matcher(self.repository.matcher_facts())

    def _admit(self, name):
        genre_policy.validate_name(name, custom=True)
        resolved, reason = self.repository.resolve(name)
        key = genre_policy.custom_key(name, resolved, reason)
        if resolved is None:
            if self.repository.genre_name(key) is not None:
                raise ValueError('Custom genre key collides with an existing identity: ' + name)
            self.repository.insert_custom(key, name)
        return key

    def rename_genre(self, key, name):
        genre_policy.validate_name(name)
        with self.repository.transaction():
            key = self.repository.current_key(key)
            old = self.repository.genre_name(key)
            if old is None:
                raise ValueError('Unknown genre identity')
            resolved, reason = self.repository.resolve(name)
            if reason == 'ambiguous_genre' or resolved not in (None, key):
                raise ValueError('Genre name conflicts with another identity')
            self.repository.rename(key, name, old_label_key=genre_catalog.normalize(old), old_label=old)
            self.repository.invalidate()

    def set_genre_alias(self, label, key):
        if not isinstance(label, str) or not genre_catalog.normalize(label):
            raise ValueError('An alias requires a nonblank label')
        with self.repository.transaction():
            key = self.repository.current_key(key)
            if self.repository.genre_name(key) is None:
                raise ValueError('Unknown genre identity')
            if self.repository.alias_conflicts(label, key):
                raise ValueError('Alias conflicts with a genre identity')
            self.repository.save_alias(genre_catalog.normalize(label), label, key)
            self.repository.invalidate()

    def set_genre_family(self, key, family_key):
        with self.repository.transaction():
            key = self.repository.current_key(key)
            if self.repository.genre_name(key) is None:
                raise ValueError('Unknown genre identity')
            self.repository.save_family(key, family_key)
            self.repository.invalidate()

    def configure_catalog(self, *, reset=False):
        records, labels = genre_catalog.catalog()
        families = genre_catalog.family_labels()
        with self.repository.transaction():
            current = self.repository.get_genre_whitelist()
            target, canonical, defaults, upgrading = genre_policy.configuration(
                current, self.repository.configuration_settings(), reset=reset)
            if upgrading:
                self.repository.seed_catalog(records, labels, families)
            if self.repository.uses_keyed_preferences():
                keys = {self._admit(name) for name in target}
                self.repository.configure_preferences(
                    [(key, 'default' if key in records else 'custom') for key in keys], list(records))
            else:
                for key, name in canonical.items():
                    if key not in records:
                        self.repository.insert_legacy_custom(key, name)
                if target != current:
                    self.repository.replace_legacy_whitelist(target)
            self.repository.save_configuration({
                'genres_seeded': '1', 'genre_catalog_revision': genre_catalog.REVISION,
                'genre_defaults': json.dumps(defaults),
            })

    def set_genres(self, names):
        names = list(names)
        with self.repository.transaction():
            if self.repository.uses_keyed_preferences():
                self.repository.replace_preferences({self._admit(name) for name in names}, origin='user')
            else:
                self.repository.replace_legacy_whitelist(names)

    def add_genre(self, name):
        with self.repository.transaction():
            if self.repository.uses_keyed_preferences():
                self.repository.save_preference(self._admit(name), enabled=True, origin='user')
            else:
                self.repository.add_legacy_genre(name)

    def remove_genre(self, name):
        with self.repository.transaction():
            if self.repository.uses_keyed_preferences():
                key, reason = self.repository.resolve(name)
                if reason == 'ambiguous_genre':
                    raise ValueError('Ambiguous genre label: ' + name)
                if key is not None:
                    self.repository.save_preference(key, enabled=False, origin='user')
            else:
                self.repository.remove_legacy_genre(name)

    def feature_axes(self, labels):
        return axes_from_matches(self.repository.axis_matches(labels))

    def family_members(self, families):
        return self.repository.family_members(families)

    def merge_genres(self, retired_key, key, *, now=None):
        source, target = self.repository.current_key(retired_key), self.repository.current_key(key)
        if source == target:
            return
        self._merge_plan(source, target)
        # Preserve originals and move discovery provenance before retiring the
        # taxonomy key. A failed taxonomy write leaves that key available to retry.
        self.discovery.merge_genre_origins(source, target)
        with self.repository.transaction():
            old_axis, new_axis, kind, preference = self._merge_plan(source, target)
            if old_axis is not None and new_axis is None:
                new_axis = self.repository.create_axis(kind, target)
            self.repository.apply_merge(source, target, preference, old_axis=old_axis, new_axis=new_axis,
                                        now=time.time() if now is None else now)
            self.repository.invalidate()

    def _merge_plan(self, source, target):
        if self.repository.genre_name(source) is None or self.repository.genre_name(target) is None:
            raise ValueError('Unknown genre identity')
        old_axis = self.repository.genre_axis(source)
        new_axis, kind = None, None
        if old_axis is not None:
            # Preserve the established broad-family meaning of genre:<family>.
            kind = 'family' if self.repository.family_exists(target) else 'genre'
            new_axis = self.repository.axis_id(kind, target)
            if new_axis is not None:
                for table, count in self.repository.axis_counts(old_axis, new_axis).items():
                    if table != 'home_hidden_facet' and count == 2:
                        raise ValueError('Conflicting current taste axes in ' + table)
        preference = genre_policy.merge_preference(self.repository.preferences(source, target))
        return old_axis, new_axis, kind, preference

    def get_genre_whitelist(self):
        return self.repository.get_genre_whitelist()

    def all_genres(self):
        return self.repository.all_genres()
