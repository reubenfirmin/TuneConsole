"""Taste-weight behavior and embedding deltas over detached model facts."""
from dataclasses import dataclass

from . import embeddings

WEIGHT_MIN, WEIGHT_MAX = .2, 3.


@dataclass(frozen=True)
class VectorChanges:
    upserts: tuple[tuple[str, bytes], ...]
    removed: tuple[str, ...]
    replace: bool = False
    model_version: str | None = None
    publication: embeddings.Publication | None = None


def reverted(weight, updated_at, now, halflife_d):
    if updated_at is None or now is None:
        return weight
    age = max(0., (now - updated_at) / 86400.)
    return 1. + (weight - 1.) * .5 ** (age / float(halflife_d))


def effective(axis, row, now, halflife_d):
    if axis.startswith('genre:') and row['weight'] == 0.:
        return 0.
    return reverted(row['weight'], row['updated_at'], now, halflife_d)


def training_delta(table, rows, current, head, settings):
    rows, current = tuple(rows), dict(current)
    version = embeddings.version(table, settings)
    if len({key for key, _ in rows}) != len(rows):
        raise ValueError('Duplicate training vector key')
    dimension = embeddings.model_dimensions(table, settings)
    if dimension is not None and any(embeddings.dimensions(blob) != dimension for _key, blob in rows):
        raise ValueError('Embedding dimensions do not match their encoder')
    if version and head and version != head[0]:
        return VectorChanges(rows, (), replace=True, model_version=version)
    old_order = tuple(current) if table == 'rec_discovered_content_vectors' else None
    upserts, seen = [], set()
    for key, blob in rows:
        seen.add(key)
        if current.pop(key, None) != blob:
            upserts.append((key, blob))
    if old_order is not None:
        old_keys = set(old_order)
        order = [key for key in old_order if key in seen]
        order.extend(key for key, _ in rows if key not in old_keys)
        if order != [key for key, _ in rows]:
            return VectorChanges(rows, (), replace=True, model_version=version)
    if upserts or current:
        return VectorChanges(tuple(upserts), tuple(current), model_version=version if head is None else None)
