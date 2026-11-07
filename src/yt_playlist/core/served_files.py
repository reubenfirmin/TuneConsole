"""Database-free access to diagnostics named by their allocated impression ID."""
from pathlib import Path

from . import fao, file_collection, served_facts


class ServedFiles:
    def __init__(self, root):
        self.root = Path(root)

    def retained(self):
        return file_collection.retained_reader(self.root)

    def collector(self):
        return file_collection.collector(self.root)

    def publish_many(self, rows):
        # Validate the whole batch before publishing its first member.
        encoded = [(identity, served_facts._encoded(identity, at, trace)) for identity, at, trace in rows]
        for identity, value in encoded:
            path = self.root / served_facts.relative(identity)
            if path.is_file() and fao.read(path).bytes != value:
                # IDs are consumed even by failed attempts. A retry allocates a
                # fresh ID rather than replacing another retained diagnostic.
                raise ValueError('Conflicting served diagnostic identity; retry publication')
            fao.write(path, value)

    def diagnostic(self, row):
        if row['diagnostic_expired']:
            return None
        try:
            with fao.read(self.root / served_facts.relative(row['id'])) as stream:
                encoded = stream.read(served_facts.MAX_BYTES + 1)
        except FileNotFoundError:
            raise ValueError('Unexpectedly missing served diagnostic: ' + str(row['id'])) from None
        if len(encoded) > served_facts.MAX_BYTES:
            raise ValueError('Served diagnostic exceeds its byte budget')
        return served_facts._verified_diagnostic(encoded, row)

    def delete_many(self, identities):
        removed = []
        for identity in identities:
            path = self.root / served_facts.relative(identity)
            if not path.exists() or fao.delete(path, missing_ok=True):
                removed.append(identity)
        return removed

    def orphan_candidates(self, before, *, limit):
        def shard(name):
            return isinstance(name, str) and len(name) == 2 and name.isascii() and name.isdecimal()

        result = []
        with file_collection.collection(self, self.root, name='.collect-orphans.json', valid_shard=shard) as work:
            for _ in range(limit):
                path = work.next()
                if path is None or path.suffix != '.json' or not path.stem.isascii() or not path.stem.isdecimal():
                    continue
                identity = int(path.stem)
                if identity < 1 or path != self.root / served_facts.relative(identity):
                    continue
                info = fao.read(path).info
                if info.ctime_ns / 1e9 <= before:
                    result.append((identity, info.identity))
            work.save()
        return result

    def delete_orphans(self, rows):
        return sum(fao.delete(self.root / served_facts.relative(identity), expected=expected, missing_ok=True)
                   for identity, expected in rows)
