"""Prepared recommendation documents and their serving projections."""
from contextlib import contextmanager
import hashlib
import json

from yt_playlist.core import album_note_outputs, clicked_pool_policy, home_outputs, json_cache, listening_outputs, recommendation_reports as reports
from yt_playlist.core.proposal_codec import encode_proposals
from yt_playlist.core.recommendation_work import finite


def _retired_surface(surface):
    return surface.startswith('model_pair:') or surface in {
        'model_live_comparison', 'model_listening_monitor', 'model_album_assignment', 'model_live_report'}


class RecommendationProposals:
    def __init__(self, repository, report_repository, note_repository, files, listening, home, publications):
        self.repository = repository
        self.report_repository = report_repository
        self.note_repository = note_repository
        self.files = files
        self.listening = listening
        self.home = home
        self.publications = publications

    def put_proposals(self, surface, data, now=None):
        self.put_encoded(surface, encode_proposals(surface, data), now)

    def put_encoded(self, surface, encoded, now):
        if _retired_surface(surface):
            raise ValueError('Retired recommendation surface')
        if surface in clicked_pool_policy.SURFACES:
            return self.publications().put_clicked_pool(surface, encoded, now)
        if surface == 'album_notes':
            value = json.loads(encoded)
            if now is not None:
                finite(now)
            if not isinstance(value, dict) or len(value) > album_note_outputs.MAX_NOTES:
                raise ValueError('Album notes must be a bounded release map')
            return self.note_repository.replace_outputs(
                [(browse, album_note_outputs.encode(browse, note)) for browse, note in value.items()])
        if surface in home_outputs.SURFACES:
            return self.home.put(surface, encoded, now)
        if listening_outputs.owns(surface):
            with self.files.retained():
                return self.listening.publish(encoded, now, surface)
        if surface in reports.SURFACES:
            prepared = reports.prepare(surface, encoded, now)
            with self.files.retained():
                durable, file_ids = [], []
                for position, part in enumerate(prepared.parts):
                    key = str(position)
                    integrity = self.files.describe(part)
                    identity = self.report_repository.reserve_file(surface, key, integrity)
                    reference = self.files.publish_reserved(identity, part, expected=integrity)
                    durable.append((surface, key, *reference))
                    file_ids.append(identity)
                self.report_repository.publish(prepared, durable, file_ids=file_ids)
            return
        self.repository.put_encoded(surface, encoded, now)

    def _proposal_json(self, surface):
        if _retired_surface(surface):
            return None
        if surface in clicked_pool_policy.SURFACES:
            value = self.publications().clicked_pool(surface)
            return json.dumps(value) if value is not None else None
        if surface == 'album_notes':
            value = self.note_repository.output_document()
            return json.dumps(value) if value else None
        if surface in home_outputs.SURFACES:
            return self.home.encoded(surface)
        if listening_outputs.owns(surface):
            with self.files.retained():
                return self.listening.encoded(surface)
        if surface not in reports.SURFACES:
            return self.repository.document(surface)
        with self.files.retained():
            manifest = self.report_repository.manifest(surface)
            if manifest is None:
                return None
            parts = manifest['parts']
            if [part['chunk_key'] for part in parts] != list(map(str, range(manifest['part_count']))):
                raise ValueError('Incomplete recommendation report')
            try:
                raw = ''.join(self.files.read(part, surface=surface, chunk_key=part['chunk_key']) for part in parts)
            except (OSError, ValueError):
                return None
            if (len(raw.encode()) != manifest['total_bytes'] or
                    hashlib.sha256(raw.encode()).hexdigest() != manifest['content_digest']):
                raise ValueError('Report differs from its publication digest')
            return raw

    def get_proposals(self, surface):
        try:
            payload = self._proposal_json(surface)
            return json_cache.decode(payload) if payload is not None else None
        except ValueError:
            if surface == 'adaptive_group_preview':
                return None
            raise

    def library_taste_summary(self):
        return self.repository.library_taste_summary()

    def publish_library_taste_summary(self, summary, now):
        self.repository.publish_library_taste_summary(summary, now)

    def revisit_remaining(self):
        return self.repository.revisit_remaining()

    def save_revisit_remaining(self, remaining):
        return self.repository.save_revisit_remaining(remaining)

    def vibe_request(self):
        return self.repository.vibe_request()

    def save_vibe_request(self, request):
        self.repository.save_vibe_request(request)

    def finish_vibe_request(self, request_id, phase):
        return self.repository.finish_vibe_request(request_id, phase)

    def proposal_fields(self, surface, fields):
        payload = self._proposal_json(surface)
        return json_cache.decode(payload, keys=set(fields)) if payload is not None else None

    def has_proposals(self, surface):
        if _retired_surface(surface):
            return False
        if surface in clicked_pool_policy.SURFACES:
            return self.publications().clicked_pool_present(surface)
        if surface == 'album_notes':
            return self.note_repository.outputs_present()
        if surface in home_outputs.SURFACES:
            return self.home.present(surface)
        if listening_outputs.owns(surface):
            return self.listening.present(surface)
        if surface in reports.SURFACES:
            return self.report_repository.present(surface)
        return self.repository.has_proposals(surface)

    def choice_usage(self):
        return self.report_repository.usage()

    def report_checkpoint(self, surface):
        if surface not in reports.SURFACES:
            raise ValueError('Unknown recommendation report')
        return self.report_repository.checkpoint(surface)

    def choice_refresh_inputs(self):
        return self.repository.choice_refresh_inputs()

    def group_checkpoint(self):
        return self.repository.group_checkpoint()

    def preview_recommendations(self, generation, schema):
        return self.repository.preview_recommendations(generation, schema)

    def cache_rows(self):
        return self.repository.cache_rows()

    def publish_pruned_cache(self, prepared):
        return self.repository.publish_pruned_cache(prepared)

    def discard_learned_outputs(self):
        return self.repository.discard_learned_outputs()

    def publish_prepared_home(self, payload, now, *, surface='home_prepared', input_revision=None):
        if _retired_surface(surface):
            raise ValueError('Retired recommendation surface')
        if surface in clicked_pool_policy.SURFACES:
            return self.publications().put_clicked_pool(surface, payload, now, input_revision=input_revision)
        if surface in home_outputs.SURFACES:
            return self.home.put(surface, payload, now, input_revision=input_revision)
        if surface in reports.SURFACES or listening_outputs.owns(surface) or surface == 'album_notes':
            return self.put_encoded(surface, payload, now)
        return self.repository.publish_prepared_home(payload, now, surface=surface, input_revision=input_revision)

    @contextmanager
    def prepare_home(self, payload, now, *, surface='home_prepared'):
        """Reserve and write display files before a guarded SQL publication."""
        with self.files.retained():
            yield self.home.prepare(surface, payload, now)

    def publish_home(self, publication, *, input_revision=None):
        return self.home.publish(publication, input_revision=input_revision)
