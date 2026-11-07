"""Embedding validation, immutable file writes and ordered SQL publication."""
import math
import sqlite3
import time
import uuid

from yt_playlist.core import embeddings


class Embeddings:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def retained(self):
        return self.files.retained()

    def discard_history_models(self):
        return self.repository.delete_spaces(('collaborative',), None)

    def assert_reader(self):
        return self.repository.assert_reader()

    def rows(self, name, *, keys=None):
        kind, _space, _table, _column = embeddings.descriptor(name)
        with self.files.retained():
            return self.files.read_many(kind, self.repository.rows(name, keys))

    def head(self, name):
        return self.repository.head(name)

    def count(self, name):
        return self.repository.count(name)

    def prepare(self, name, upserts, removed=(), *, replace=False, model_version=None, now=None):
        kind, space, _table, _column = embeddings.descriptor(name)
        upserts, removed = tuple(upserts), tuple(removed)
        subjects = self.repository.subjects(kind, [key for key, _vector in upserts])
        admitted, seen, size = [], set(), None
        for key, vector in upserts:
            subject = subjects.get(key)
            if subject is None:
                raise sqlite3.IntegrityError(f'Unknown current {kind} owner: {key!r}')
            if subject in seen:
                raise ValueError('Duplicate training vector owner')
            seen.add(subject)
            dim = embeddings.dimensions(vector)
            if size is not None and dim != size:
                raise ValueError('Mixed dimensions in an embedding space')
            size = dim
            admitted.append((subject, vector))
        previous = self.repository.head(name)
        model_version = model_version or (previous[0] if previous and not replace else 'manual:' + uuid.uuid4().hex)
        now = time.time() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError('Embedding publication time must be finite')
        retracted = list(self.repository.subjects(kind, removed, retired=True).values())
        files = None
        if admitted and self.repository.file_owned():
            orders, order = ({}, 0) if replace else self.repository.insertion_orders(
                kind, space, [subject for subject, _vector in admitted], retracted)
            for subject in retracted:
                orders.pop(subject, None)
            values = []
            for subject, vector in admitted:
                previous_order = orders.get(subject)
                if previous_order is None:
                    order += 1
                values.append((subject, vector, now, previous_order if previous_order is not None else order))
            files = self.files.write(kind, size, values)
        return embeddings.Publication(name, model_version, tuple(admitted), tuple(retracted), replace, now, size, files)

    def publish(self, publications):
        return self.repository.publish(publications)

    def replace(self, name, rows, settings):
        rows = tuple(rows)
        dimension = embeddings.model_dimensions(name, settings)
        if dimension is not None and any(embeddings.dimensions(blob) != dimension for _key, blob in rows):
            raise ValueError('Embedding dimensions do not match their encoder')
        with self.files.retained():
            publication = self.prepare(name, rows, replace=True, model_version=embeddings.version(name, settings))
            self.publish([publication])
