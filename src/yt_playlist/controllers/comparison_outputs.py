"""Prepare paired displays and publish their files before their SQL heads."""
import hashlib
import json
import struct

from yt_playlist.core import model_projections, recommendation_outputs


class ComparisonOutputs:
    def __init__(self, repository, files, groups, publications):
        self.repository, self.files = repository, files
        self.groups, self.publications = groups, publications

    def native(self):
        return self.repository.native()

    def put(self, surface, raw, at, *, input_revision=None):
        prepared, inputs = model_projections.prepare_output(surface, json.loads(raw), at, input_revision=input_revision)
        with self.files.retained():
            roots = self.groups.prepare_references(surface, prepared.value, at)
            durable = [self.files.publish(part) for part in prepared.parts]
            generation = None
            if inputs is not None:
                bundle, options = inputs
                publications = self.publications()
                plan = publications.prepare_bundle(bundle, None, **options)
                build = publications.claim(plan)
                publications.stage(plan, build)
                publications.publish(build, plan.created_at)
                generation = build.generation_id
            return self.repository.publish(prepared, durable, generation, roots)

    def present(self, surface):
        return self.repository.present(*model_projections.address(surface))

    def encoded(self, surface):
        run, arm, format = model_projections.address(surface)
        with self.files.retained():
            document = self.repository.document(run, arm, format)
            if document is None or len(document['parts']) != document['part_count']:
                return None
            try:
                raw = ''.join(self.files.read(part, surface=surface, chunk_key=part['chunk_key'])
                              for part in document['parts'])
                if hashlib.sha256(raw.encode()).hexdigest() != document['output_digest']:
                    return None
                value = recommendation_outputs.decode_json(raw)
                if format == 'radio':
                    modes = document['modes']
                    if modes:
                        value['modes'] = [{**display, 'mode_id': mode['mode_id'], 'space': mode['space'],
                            'centroid': list(struct.unpack('<' + 'd' * (len(mode['centroid']) // 8), mode['centroid']))}
                            for display, mode in zip(value['modes'], modes, strict=True)]
                    if document['generation_id'] is not None:
                        value['_ranked_generation'] = document['generation_id']
                        value['_ranked_prefix'] = surface + ':'
            except (OSError, ValueError, TypeError):
                return None
            return json.dumps({'run_id': run, 'data': value})
