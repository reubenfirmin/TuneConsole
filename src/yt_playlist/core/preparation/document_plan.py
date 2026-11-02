"""Finite durable member plans, paged before taking a SQL writer reservation."""
from bisect import bisect_right
from dataclasses import dataclass

from .. import recommendation_outputs as files
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError


UNIT_BYTES = 128 * 1024


class Units:
    def __init__(self, surface, indexes, count):
        self.surface, self.indexes, self.count = surface, indexes, count
        self.ends = [index[0] for index in indexes]
        self.index_cache = None
        self.cached = None

    def __len__(self):
        return self.count

    def __getitem__(self, position):
        if not 0 <= position < self.count:
            raise IndexError(position)
        index = bisect_right(self.ends, position)
        if self.index_cache is None or self.index_cache[0] != index:
            end, filename, digest, size = self.indexes[index]
            chunks = files.read(filename, digest, size, surface=self.surface, chunk_key='index')
            start = self.ends[index - 1] if index else 0
            ends = [chunk[0] for chunk in chunks]
            if (not 1 <= len(chunks) <= 128 or ends != sorted(set(ends))
                    or ends[0] <= start or ends[-1] != end):
                raise MigrationError('Prepared document plan index is incomplete')
            self.index_cache = index, chunks, ends
        _, chunks, ends = self.index_cache
        chunk_index = bisect_right(ends, position)
        if self.cached is None or self.cached[:2] != (index, chunk_index):
            end, filename, digest, size = chunks[chunk_index]
            values = files.read(filename, digest, size, surface=self.surface, chunk_key='units')
            start = ends[chunk_index - 1] if chunk_index else self.ends[index - 1] if index else 0
            if not isinstance(values, list) or len(values) != end - start:
                raise MigrationError('Prepared document unit chunk is incomplete')
            self.cached = index, chunk_index, values
        start = ends[chunk_index - 1] if chunk_index else self.ends[index - 1] if index else 0
        return self.cached[2][position - start]


@dataclass(frozen=True)
class Plan:
    fields: dict
    manifest: tuple
    units: Units


def build(root, surface, fields, units):
    chunks, pending, used, count = [], [], 0, 0
    def flush():
        if pending:
            chunks.append((count, *files.publish(root, files.encode(surface, 'units', pending))))
    for unit in units:
        size = len(canonical(unit))
        if pending and (len(pending) >= 128 or used + size > UNIT_BYTES):
            flush()
            pending, used = [], 0
        pending.append(unit)
        used += size
        count += 1
    flush()
    fields['unit_count'] = count
    indexes = []
    for start in range(0, len(chunks), 128):
        selected = chunks[start:start + 128]
        indexes.append((selected[-1][0], *files.publish(root, files.encode(surface, 'index', selected))))
    manifest = files.publish(root, files.encode(surface, 'plan', {'fields': fields, 'indexes': indexes}))
    return Plan(fields, manifest, Units(surface, indexes, count))


def load(surface, reference):
    value = files.read(*reference, surface=surface, chunk_key='plan')
    fields, indexes = value['fields'], value['indexes']
    ends = [index[0] for index in indexes]
    if not ends or any(type(end) is not int for end in ends) or ends != sorted(set(ends)) or ends[-1] != fields['unit_count']:
        raise MigrationError('Prepared document plan has an invalid member index')
    return Plan(fields, tuple(reference), Units(surface, indexes, fields['unit_count']))
