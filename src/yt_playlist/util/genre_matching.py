"""Immutable vocabulary values and neutral provider tag normalization."""
from dataclasses import dataclass
from types import MappingProxyType

from yt_playlist.util import genre_catalog


@dataclass(frozen=True)
class GenreMatcher:
    canon: object
    keys: object

    @classmethod
    def from_values(cls, names, keys=None):
        return cls(MappingProxyType(dict(names)), MappingProxyType(dict(keys or {})))


def build_canon(names):
    records, labels = genre_catalog.catalog()
    enabled, custom = {}, {}
    for name in names:
        key = genre_catalog.normalize(name)
        if not key:
            continue
        if label := labels.get(key):
            enabled[label[0]] = records[label[0]]["name"]
        else:
            custom.setdefault(key, name)
    return {**{key: enabled[value[0]] for key, value in labels.items() if value[0] in enabled},
            **custom}
