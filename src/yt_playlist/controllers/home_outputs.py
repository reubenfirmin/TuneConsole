"""Home display validation, reference preparation, and file publication."""
from dataclasses import dataclass

from yt_playlist.core import home_outputs


@dataclass(frozen=True)
class Publication:
    prepared: home_outputs.Prepared
    filenames: tuple
    policies: frozenset
    roots: tuple | None
    file_ids: tuple = ()


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
        if self.repository.unchanged(prepared):
            try:
                if self.encoded(surface) == prepared.raw:
                    return Publication(prepared, (), frozenset(), None)
            except (OSError, ValueError):
                pass  # A matching SQL digest cannot establish file health.
        roots = self.references.prepare_references(surface, prepared.value, now)
        filenames, file_ids = [], []
        for position, encoded in enumerate(prepared.parts):
            key = str(position)
            integrity = self.files.describe(encoded)
            identity = self.repository.reserve_file(surface, key, integrity)
            reference = self.files.publish_reserved(identity, encoded, expected=integrity)
            file_ids.append(identity)
            filenames.append((surface, key, *reference))
        return Publication(prepared, tuple(filenames), frozenset(home_outputs.policy_references(prepared.value)),
                           roots, tuple(file_ids))

    def publish(self, publication, *, input_revision=None):
        return self.repository.publish(publication.prepared, publication.filenames, publication.policies,
                                       publication.roots, input_revision=input_revision, file_ids=publication.file_ids)

    def put(self, surface, raw, now, *, input_revision=None):
        with self.files.retained():
            prepared = home_outputs.prepare(surface, raw, now)
            if self.repository.unchanged(prepared):
                try:
                    if self.encoded(surface) == raw:
                        return
                except (OSError, ValueError):
                    pass  # Recreate missing/damaged files before acknowledging.
            publication = self.prepare(surface, prepared, now)
            return self.publish(publication, input_revision=input_revision)

    def document(self, surface):
        return self.repository.document(surface)

    def publication_id(self, surface):
        return self.repository.publication_id(surface)

    def encoded(self, surface):
        with self.files.retained():
            manifest = self.document(surface)
            return None if manifest is None else self.read_manifest(
                surface, manifest.digest, manifest.size, manifest.parts)

    def present(self, surface):
        return self.repository.present(surface)

    def published_after(self, surface, started_at):
        return self.repository.published_after(surface, started_at)

    def input_revision(self, dependencies):
        return self.repository.input_revision(dependencies)
