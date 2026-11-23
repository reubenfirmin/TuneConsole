"""Convert bounded installed float32 bodies without SQL hidden in the file port."""
from yt_playlist.core import fao


class EmbeddingPreparation:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def prepare(self, kind, limit=128):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError('Embedding batch must be 1 to 128')
        rows = self.repository.page(kind, limit)
        groups = {}
        for order, subject, space, version, dimensions, vector, built in rows:
            groups.setdefault((space, version, dimensions), []).append((subject, vector, built, order))
        for (space, version, dimensions), values in groups.items():
            references = self.files.write(kind, dimensions, values)
            heads = [(subject, None, width, digest, position)
                     for subject, width, digest, position, _, _ in references]
            if [vector for _, vector in self.files.read_many(kind, heads)] != [value[1] for value in values]:
                raise fao.CorruptFile('Embedding conversion changed float32 bytes')
            self.repository.publish(kind, space, version, references)
        return len(rows)
