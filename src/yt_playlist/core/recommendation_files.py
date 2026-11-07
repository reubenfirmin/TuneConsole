"""Opaque recommendation output access, independent of SQLite owners."""
from . import recommendation_outputs, policy_documents


def decode_display(chunks, ranks, surface):
    return RecommendationFiles().display(chunks, ranks, surface)


class RecommendationFiles:
    def __init__(self, root=None):
        self.root = root

    def retained(self):
        return policy_documents.retained_reader(self.root)

    def publish(self, encoded):
        return recommendation_outputs.publish(self.root, encoded)

    def read(self, reference, *, surface, chunk_key):
        return recommendation_outputs.read(reference['filename'], reference['content_digest'],
            reference['byte_size'], surface=surface, chunk_key=chunk_key)

    def display(self, chunks, ranks, surface):
        wanted, result = set(ranks), {}
        for row in chunks:
            value = self.read(row, surface=surface, chunk_key=row['chunk_key'])
            for rank, owner, explanation in value['rows']:
                if rank in wanted:
                    result[rank] = {**explanation, 'owner': owner}
        return result
