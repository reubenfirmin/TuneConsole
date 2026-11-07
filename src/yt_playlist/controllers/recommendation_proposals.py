"""Prepared recommendation documents and their serving projections."""
import hashlib
import json

from yt_playlist.core import album_note_outputs, clicked_pool_policy, home_outputs, json_cache, listening_outputs, recommendation_reports as reports
from yt_playlist.core.proposal_codec import encode_proposals
from yt_playlist.core.recommendation_work import finite
from yt_playlist.core.recommendation_outputs import encode


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
        if surface in clicked_pool_policy.SURFACES and self.publications().native_clicked_pools():
            return self.publications().put_clicked_pool(surface, encoded, now)
        if surface == 'album_notes' and self.note_repository.native_outputs():
            value = json.loads(encoded)
            if now is not None:
                finite(now)
            if not isinstance(value, dict) or len(value) > album_note_outputs.MAX_NOTES:
                raise ValueError('Album notes must be a bounded release map')
            if any(not isinstance(browse, str) or not browse or not isinstance(note, dict) for browse, note in value.items()):
                raise ValueError('Album note output requires a release ID and an object')
            prepared = [(browse, encode(surface, browse, note)) for browse, note in value.items()]
            with self.files.retained():
                durable = [(browse, self.files.publish(part)) for browse, part in prepared]
                return self.note_repository.replace_outputs(durable, now)
        if surface in home_outputs.SURFACES:
            return self.home.put(surface, encoded, now, validate=False)
        if listening_outputs.owns(surface):
            with self.files.retained():
                return self.listening.publish(encoded, now, surface)
        if surface in reports.SURFACES and self.report_repository.native():
            prepared = reports.prepare(surface, encoded, now)
            with self.files.retained():
                durable = [(surface, str(position), *self.files.publish(part))
                           for position, part in enumerate(prepared.parts)]
                self.report_repository.publish(prepared, durable)
            return
        self.repository.put_encoded(surface, encoded, now)

    def _proposal_json(self, surface):
        if surface in clicked_pool_policy.SURFACES and self.publications().native_clicked_pools():
            value = self.publications().clicked_pool(surface)
            return json.dumps(value) if value is not None else None
        if surface == 'album_notes':
            with self.files.retained():
                raw, references = self.note_repository.output_document()
                if references is None:
                    return raw
                value = {row['browse_id']: self.files.read(row, surface=surface, chunk_key=row['browse_id']) for row in references}
                return json.dumps(value) if value else None
        if surface in home_outputs.SURFACES:
            return self.home.encoded(surface)
        if listening_outputs.owns(surface):
            with self.files.retained():
                return self.listening.encoded(surface)
        if surface not in reports.SURFACES:
            return self.repository._proposal_json(surface)
        with self.files.retained():
            raw, manifest = self.report_repository.document(surface)
            if manifest is None:
                return raw
            parts = manifest['parts']
            if [part['chunk_key'] for part in parts] != list(map(str, range(manifest['part_count']))):
                if manifest['prepared']:
                    return None
                raise ValueError('Incomplete recommendation report')
            try:
                raw = ''.join(self.files.read(part, surface=surface, chunk_key=part['chunk_key']) for part in parts)
            except (OSError, ValueError):
                return None
            if (len(raw.encode()) != manifest['total_bytes'] or
                    hashlib.sha256(raw.encode()).hexdigest() != manifest['content_digest']):
                if manifest['prepared']:
                    return None
                raise ValueError('Report differs from its publication digest')
            return raw

    def get_proposals(self, surface):
        payload = self._proposal_json(surface)
        return json_cache.decode(payload) if payload is not None else None

    def proposal_fields(self, surface, fields):
        payload = self._proposal_json(surface)
        return json_cache.decode(payload, keys=set(fields)) if payload is not None else None

    def has_proposals(self, surface):
        if surface in clicked_pool_policy.SURFACES and self.publications().native_clicked_pools():
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
        owned, usage = self.report_repository.usage()
        return usage if owned else (self.get_proposals('listening_choices') or {}).get('home_format') or {}

    def report_checkpoint(self, surface):
        if surface not in reports.SURFACES:
            raise ValueError('Unknown recommendation report')
        owned, checkpoint = self.report_repository.checkpoint(surface)
        if owned:
            return checkpoint
        value = self.get_proposals(surface)
        if value is None:
            return None
        refresh = value.get('refresh') or {}
        return {'at': value.get('at', value.get('built_at')), 'schema': value.get('schema', value.get('version')),
                'watermark': value.get('watermark'), 'report_id': value.get('report_id'),
                'generation': refresh.get('generation'), 'inputs': refresh.get('inputs', value.get('inputs')),
                'next_at': refresh.get('next_at')}

    def fresh_candidates(self):
        def select(value):
            return {mode: {'fresh': [{name: item[name] for name in
                    ('key', 'video_id', 'title', 'artist', 'album', 'thumbnail', 'genre', 'score') if name in item}
                    for item in bucket.get('fresh', [])]}
                for mode, bucket in (value or {}).items() if not mode.startswith('_')}
        payload = self._proposal_json('mode_bundles')
        return json_cache.decode(payload, project=select) if payload is not None else {}

    def group_pools_json(self):
        row = self.repository._group_pools_json()
        return json_cache.decode_groups(*row) if row is not None else None

    def choice_refresh_inputs(self):
        values = self.repository.choice_refresh_inputs()
        return values if values is not None else tuple(self.get_proposals(surface) or {}
                                                      for surface in ('choice_interests', 'choice_bandit'))

    def group_checkpoint(self):
        return self.repository.group_checkpoint()

    def cache_rows(self):
        return self.repository.cache_rows()

    def stage_pruned_cache(self, prepared):
        return self.repository.stage_pruned_cache(prepared)

    def publish_pruned_cache(self, prepared):
        return self.repository.publish_pruned_cache(prepared)

    def discard_learned_outputs(self):
        return self.repository.discard_learned_outputs()

    def stage_prepared_bundles(self, bundles_json, fresh_json):
        return self.repository.stage_prepared_bundles(bundles_json, fresh_json)

    def publish_prepared_bundles(self, bundles_json, fresh_json, now):
        return self.repository.publish_prepared_bundles(bundles_json, fresh_json, now)

    def publish_prepared_home(self, payload, now, *, surface='home_prepared', input_revision=None):
        if surface in clicked_pool_policy.SURFACES and self.publications().native_clicked_pools():
            return self.publications().put_clicked_pool(surface, payload, now, input_revision=input_revision)
        if surface in home_outputs.SURFACES:
            return self.home.put(surface, payload, now, input_revision=input_revision)
        if surface in reports.SURFACES or listening_outputs.owns(surface) or surface == 'album_notes':
            return self.put_encoded(surface, payload, now)
        return self.repository.publish_prepared_home(payload, now, surface=surface, input_revision=input_revision)
