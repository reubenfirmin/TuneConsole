"""Coordinate listening display files and their SQL references."""
from dataclasses import dataclass
import json

from yt_playlist.core import listening_outputs
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.schema_contract import MigrationError
from yt_playlist.rec import listening_policy as policy


@dataclass(frozen=True)
class ListeningGeneration:
    publication: object
    durable: tuple
    file_ids: tuple


class ListeningAnalytics:
    def __init__(self, repository, files, read_file, exists_file):
        self.repository = repository
        self.files = files
        self.read_file = read_file
        self.exists_file = exists_file

    def catalog_rows(self):
        return self.repository.catalog_rows()

    def rating_actions(self, now):
        return self.repository.rating_actions(now)

    def impressions(self):
        return self.repository.impressions()

    def plays(self, now):
        return self.repository.plays(now)

    def observations(self, now):
        return self.repository.observations(now)

    def player(self, now):
        return self.repository.player(now, kinds=policy.PLAYER_KINDS)

    def feedback(self, now):
        return self.repository.feedback(now, kinds=policy.FEEDBACK_KINDS,
            positive=policy.POSITIVE_FEEDBACK, negative=policy.NEGATIVE_FEEDBACK)

    def artist_names(self):
        return self.repository.artist_names()

    def mix_playlist_ids(self):
        return self.repository.mix_playlist_ids()

    def video_keys(self):
        return self.repository.video_keys()

    def encoded(self, surface='listening_analytics'):
        identity = policy.output_identity(surface)
        if identity is None:
            return None
        reference = self.repository.output_reference(*identity)
        try:
            return self.read_file(*reference, surface=identity[0], chunk_key=identity[1]) if reference else None
        except (OSError, ValueError, MigrationError):
            return None

    def prepared(self, surface='listening_analytics'):
        raw = self.encoded(surface)
        return json.loads(raw) if raw else None

    def present(self, surface):
        identity = policy.output_identity(surface)
        if identity is None:
            return False
        reference = self.repository.output_reference(*identity)
        return reference is not None and self.exists_file(reference[0])

    def prepare_generation(self, chunks, manifest, edition, now, *, input_revision):
        publication = listening_outputs.prepare(chunks, manifest, edition, now, input_revision=input_revision)
        times = {(surface, key): at for surface, key, at in publication.output_times}
        durable, file_ids = [], []
        for surface, key, encoded in publication.outputs:
            check_cancellable_work()
            integrity = self.files.describe(encoded)
            identity = self.repository.reserve_file(surface, key, integrity)
            reference = self.files.publish_reserved(identity, encoded, expected=integrity)
            durable.append((surface, key, *reference, times[surface, key]))
            file_ids.append(identity)
        return ListeningGeneration(publication, tuple(durable), tuple(file_ids))

    def publish_generation(self, generation, timezone):
        """The guarded publication callback performs only this repository's SQL."""
        return self.repository.publish_files(generation.publication, generation.durable,
                                             keep_previous=True, timezone=timezone, file_ids=generation.file_ids)

    def record_impressions(self, signatures, now):
        return self.repository.record_impressions(signatures, now,
            last_shown_before=now - policy.IMPRESSION_COOLDOWN_SECONDS)

    def publish(self, encoded, now, surface='listening_analytics'):
        if surface == 'listening_edition':
            manifest, edition = self.encoded(), encoded
        elif surface == 'listening_analytics':
            manifest, edition = encoded, self.encoded('listening_edition')
        else:
            raise ValueError('Listening chunks must publish with their complete manifest')
        chunks = {ref['key']: self.encoded('listening_chunk:' + ref['key'])
                  for ref in (json.loads(manifest).get('chunks', []) if manifest else [])}
        with self.files.retained():
            generation = self.prepare_generation(chunks, manifest, edition, now, input_revision=None)
            return self.repository.publish_files(generation.publication, generation.durable,
                                                 keep_previous=True, file_ids=generation.file_ids)
