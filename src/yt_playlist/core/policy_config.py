"""Content-addressed policy bodies shared by compact, immutable ranking variants."""
import hashlib
import json

BASE_FIELD = '_policy_base'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def base_record(configuration, now):
    body = {key: value for key, value in configuration.items() if key not in ('ranker', 'experiment')}
    raw = canonical(body)
    version = hashlib.sha256(raw.encode()).hexdigest()
    return version, raw, len(raw.encode()), now


def variant_record(base, dimensions, now, *, version=None):
    raw = canonical({BASE_FIELD: base, **dimensions})
    return version or hashlib.sha256(raw.encode()).hexdigest()[:16], raw, now


def pack(configuration, now, *, version=None):
    base = base_record(configuration, now)
    dimensions = {key: configuration[key] for key in ('ranker', 'experiment') if key in configuration}
    return variant_record(base[0], dimensions, now, version=version), base
