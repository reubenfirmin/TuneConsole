"""Opaque recommendation output access, independent of SQLite owners."""
from pathlib import Path
import hashlib

from . import fao, recommendation_outputs, policy_documents


def decode_display(chunks, ranks, surface):
    return RecommendationFiles().display(chunks, ranks, surface)


class RecommendationFiles:
    def __init__(self, root=None):
        self.root = root

    def retained(self):
        return policy_documents.retained_reader(self.root)

    def publish(self, encoded):
        return recommendation_outputs.publish(self.root, encoded)

    def exists(self, filename):
        return Path(filename).is_file()

    def write_cache(self, encoded):
        digest = hashlib.sha256(encoded).hexdigest()
        path = self.root / digest[:2] / (digest + '.json')
        fao.write(path, encoded)
        return str(path), digest, len(encoded)

    def read(self, reference, *, surface, chunk_key):
        return recommendation_outputs.read(reference['filename'], reference['content_digest'],
            reference['byte_size'], surface=surface, chunk_key=chunk_key)

    def read_original(self, reference):
        """Read the retained plain-text format used by installed Radio checkpoints."""
        from .schema_contract import MigrationError
        with fao.read(reference['filename']) as stream:
            encoded = stream.read(reference['byte_size'] + 1)
        if (len(encoded) != reference['byte_size'] or
                hashlib.sha256(encoded).hexdigest() != reference['digest'] or
                Path(reference['filename']).stem != reference['digest']):
            raise MigrationError('Prepared Radio file does not match its digest')
        return encoded.decode('utf-8')

    def display(self, chunks, ranks, surface):
        wanted, result = set(ranks), {}
        for row in chunks:
            value = self.read(row, surface=surface, chunk_key=row['chunk_key'])
            for rank, owner, explanation in value['rows']:
                if rank in wanted:
                    result[rank] = {**explanation, 'owner': owner}
        return result
