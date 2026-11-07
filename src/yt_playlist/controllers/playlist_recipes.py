"""Saved playlist recipes and canvases coordinate SQL references and file access."""
import json
import uuid

from yt_playlist.core import playlist_recipe_policy as policy


class PlaylistRecipes:
    def __init__(self, repository, files, guard):
        self.repository, self.files, self.guard = repository, files, guard

    def set_recipe(self, scope, recipe, now=None):
        with self.guard(), self.files.retained():
            owner = self.repository.document_owner('recipes', scope)
            if not owner['native']:
                self.repository.publish_legacy('recipes', owner['scope'], json.dumps(recipe), now)
                return
            facets = policy.facets(recipe)
            append = None
            if recipe.get('tv_refill') is not None and self.repository.append_available():
                checkpoint = recipe['tv_refill']
                if not isinstance(checkpoint, dict):
                    raise ValueError('Unsupported TV append recovery checkpoint')
                append = policy.append_plan(recipe, self.repository.append_owner(owner['scope'],
                    recipe.get('tv_stream'), checkpoint.get('request_id')))
            reference = self.files.publish('playlist-recipe', owner['id'], recipe)
            if append is not None:
                append['reference'] = self.files.publish('tv-append', append['id'], append['checkpoint'])
            self.repository.publish_recipe(owner, uuid.uuid4().hex, recipe, facets, reference, append, now)

    def get_recipe(self, scope):
        with self.files.retained():
            facts = self.repository.document_facts('recipes', scope)
            if facts is None:
                return None
            if facts['native']:
                header = facts['header']
                value = self.files.read(self._reference(header), kind='playlist-recipe', owner=header['id'])
                policy.project(value, header, facts['facets'])
                identity = header['id']
            else:
                raw = self.files.read_original(facts['header'], facts['parts']) if facts['prepared'] else facts['raw']
                value = json.loads(raw) if raw is not None else None
                identity = None
            if not isinstance(value, dict):
                return value
            append = self.repository.append_facts(facts['scope'], identity, value)
            if append is not None:
                header = append['header']
                if header is None:
                    raise ValueError('Saved TV append has no retained intent')
                checkpoint = self.files.read(self._reference(header), kind='tv-append',
                    owner=header['id'] if append['native'] else header['source'])
                policy.project_append(value, checkpoint, append)
            return value

    def set_cluster_canvas(self, scope, state, now=None):
        with self.guard(), self.files.retained():
            owner = self.repository.document_owner('canvases', scope)
            if not owner['native']:
                self.repository.publish_legacy('canvases', owner['scope'], state, now)
                return
            if state is not None and not isinstance(state, str):
                raise ValueError('A saved canvas requires its serialized text')
            reference = self.files.publish('playlist-canvas', owner['id'], state)
            self.repository.publish_canvas(owner, uuid.uuid4().hex, reference, now)

    def get_cluster_canvas(self, scope):
        with self.files.retained():
            facts = self.repository.document_facts('canvases', scope)
            if facts is None:
                return None
            if facts['native']:
                return self.files.read(self._reference(facts['header']), kind='playlist-canvas', owner=facts['header']['id'])
            return self.files.read_original(facts['header'], facts['parts']) if facts['prepared'] else facts['raw']

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
