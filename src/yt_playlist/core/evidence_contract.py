"""Canonical evidence bytes and references, independent of storage owners."""
import json


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def artifact_components(node):
    if isinstance(node, dict):
        if set(node) == {'$artifact'}:
            yield node['$artifact']
        else:
            for item in node.values():
                yield from artifact_components(item)
    elif isinstance(node, list):
        for item in node:
            yield from artifact_components(item)
