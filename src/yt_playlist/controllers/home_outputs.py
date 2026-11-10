"""Home display validation, reference preparation, and file publication."""
from dataclasses import dataclass
import json

from yt_playlist.core import home_outputs


@dataclass(frozen=True)
class Publication:
    prepared: home_outputs.Prepared
    filenames: tuple
    policies: frozenset
    roots: tuple | None


class HomeOutputs:
    def __init__(self, repository, references, files, read_manifest):
        self.repository = repository
        self.references = references
        self.files = files
        self.read_manifest = read_manifest

    def prepare(self, surface, raw, now, *, cache=False):
        prepared = raw if isinstance(raw, home_outputs.Prepared) else home_outputs.prepare(surface, raw, now)
        if prepared.surface != surface or prepared.built_at != now:
            raise ValueError('Prepared Home surface or observation time does not match')
        roots = self.references.prepare_references(surface, prepared.value, now)
        filenames = []
        if self.repository.native():
            write = self.files.write_cache if cache else self.files.publish
            filenames = [(surface, str(position), *write(encoded)) for position, encoded in enumerate(prepared.parts)]
        return Publication(prepared, tuple(filenames), frozenset(home_outputs.policy_references(prepared.value)), roots)

    def publish(self, publication, *, input_revision=None):
        return self.repository.publish(publication.prepared, publication.filenames, publication.policies,
                                       publication.roots, input_revision=input_revision)

    def put(self, surface, raw, now, *, input_revision=None, validate=True):
        with self.files.retained():
            if not self.repository.native() and not validate:
                roots = self.references.prepare_references(surface, json.loads(raw), now)
                return self.repository.publish_legacy(surface, raw, now, roots)
            publication = self.prepare(surface, raw, now)
            return self.publish(publication, input_revision=input_revision)

    def document(self, surface):
        return self.repository.document(surface)

    def encoded(self, surface):
        with self.files.retained():
            raw, manifest = self.document(surface)
            return self.read_manifest(surface, *manifest) if manifest is not None else raw

    def present(self, surface):
        return self.repository.present(surface)
