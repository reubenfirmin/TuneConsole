"""Saved playlist recipes and canvases coordinate SQL references and file access."""
import json
import uuid

from yt_playlist.core import append_work, playlist_recipe_policy as policy
from yt_playlist.core.catalog_state import rekey_state


class PlaylistRecipes:
    def __init__(self, repository, files, guard):
        self.repository, self.files, self.guard = repository, files, guard

    def set_recipe(self, scope, recipe, now=None):
        with self.guard(), self.files.retained():
            facets = policy.facets(recipe)
            owner = self.repository.document_owner('recipes', scope)
            recipe, append = append_work.prepare(recipe)
            if append is not None:
                self.repository.check_append(owner['scope'], append)
            integrity = self.files.describe('playlist-recipe', owner['id'], recipe)
            file_id = self.repository.reserve_file('playlist-recipe', owner['id'], integrity)
            reference = self.files.publish_reserved(file_id, 'playlist-recipe', owner['id'], recipe,
                                                    expected=integrity)
            self.repository.publish_recipe(owner, uuid.uuid4().hex, recipe, facets, reference, append, now,
                                           file_id=file_id)

    def get_recipe(self, scope):
        with self.files.retained():
            facts = self.repository.document_facts('recipes', scope)
            if facts is None:
                return None
            header = facts['header']
            value = self.files.read(self._reference(header), kind='playlist-recipe', owner=header['id'])
            policy.project(value, header, facts['facets'])
            return append_work.project(value, facts['appends'])

    def set_cluster_canvas(self, scope, state, now=None):
        with self.guard(), self.files.retained():
            if state is not None and not isinstance(state, str):
                raise ValueError('A saved canvas requires its serialized text')
            owner = self.repository.document_owner('canvases', scope)
            integrity = self.files.describe('playlist-canvas', owner['id'], state)
            file_id = self.repository.reserve_file('playlist-canvas', owner['id'], integrity)
            reference = self.files.publish_reserved(file_id, 'playlist-canvas', owner['id'], state,
                                                    expected=integrity)
            self.repository.publish_canvas(owner, uuid.uuid4().hex, reference, now, file_id=file_id)

    def get_cluster_canvas(self, scope):
        with self.files.retained():
            facts = self.repository.document_facts('canvases', scope)
            if facts is None:
                return None
            return self.files.read(self._reference(facts['header']), kind='playlist-canvas', owner=facts['header']['id'])

    @staticmethod
    def _reference(header):
        return tuple(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))

    def delete_cluster_canvas(self, scope):
        return self.repository.delete_cluster_canvas(scope)

    def recent_video_playlist_refs(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('Video recipe lookup requires a limit from 1 to 20')
        return self.repository.recent_video_playlist_refs(limit)

    def get_recipe_created_ats(self):
        return self.repository.get_recipe_created_ats()

    def rekey_recordings(self, keymap):
        with self.guard():
            self._rekey_recordings(keymap)

    def _rekey_recordings(self, keymap):
        for name in ('recipes', 'canvases'):
            for scope, created in self.repository.document_created_ats(name).items():
                original = self.get_recipe(scope) if name == 'recipes' else self.get_cluster_canvas(scope)
                try:
                    value = original if name == 'recipes' else json.loads(original)
                    revised = rekey_state(value, keymap)
                except (ValueError, TypeError):
                    continue
                if revised == value:
                    continue
                if name == 'recipes':
                    self.set_recipe(scope, revised, created)
                else:
                    self.set_cluster_canvas(scope, json.dumps(revised), created)
