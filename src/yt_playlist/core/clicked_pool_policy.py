"""Admission and publication inputs for a clicked library pool."""
import time
import uuid

from . import recommendation_outputs
from .recommendation_work import finite

LANES = ('wheelhouse', 'explore', 'comfort', 'temporal')
SURFACES = tuple('home_pool:' + lane for lane in LANES)


def validate(surface, value):
    if (surface not in SURFACES or not isinstance(value, dict) or set(value) != {'parameters', 'tracks'}
            or not isinstance(value['tracks'], list)):
        raise ValueError('Unknown clicked library pool owner or fields')
    parameters = value['parameters']
    if (not isinstance(parameters, dict) or set(parameters) != {'lane', 'epoch', 'include_genres', 'avoid_genres'}
            or parameters['lane'] != surface.split(':')[1] or type(parameters['epoch']) is not int
            or not 0 <= parameters['epoch'] < 2 ** 63):
        raise ValueError('Invalid clicked library pool parameters')
    for field in ('include_genres', 'avoid_genres'):
        values = parameters[field]
        if (not isinstance(values, list) or len(values) > 100 or len(set(values)) != len(values)
                or any(not isinstance(value, str) or not value or len(value.encode()) > 1024 for value in values)):
            raise ValueError('Invalid clicked library genre selection')
    return value


def decode(surface, raw):
    if not isinstance(raw, str) or len(raw.encode()) > 128 * 1024 * 1024:
        raise ValueError('Clicked library pool exceeds its publication budget')
    return validate(surface, recommendation_outputs.decode_json(raw))


def publication_inputs(surface, value, at, *, policy=None, input_revision=None, pending_policies=(), capture_only=False):
    validate(surface, value)
    policies = {track.get('pool_policy') for track in value['tracks']}
    if len(policies) > 1 or (policies and None in policies):
        raise ValueError('Clicked library pool has no coherent recorded policy')
    policy = policy or next(iter(policies), None)
    if policy is None:
        return None
    created = finite(at) if at is not None else None if capture_only else time.time()
    lane = value['parameters']['lane']
    return {'all': {lane: value['tracks']}}, dict(
        external_key='clicked-pool:' + uuid.uuid4().hex, policy_version=policy,
        input_revision=input_revision, created_at=created, pending_policies=pending_policies,
        home_surface=surface, allow_unobserved=capture_only)
