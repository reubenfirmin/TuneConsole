"""Typed Home demand and held listening windows, separate from prepared output."""
import json

from . import album_note_requests, evidence_maintenance, worker_settings
from .album_tuning_state import REFRESH
from .recommendation_work import finite

FORMATS = ('albums', 'mixes', 'radio')
HOME_GENRE = 'home_genre_request'
HOME_VIBE = 'home_vibe_request'
VIBE_PHASES = ('pending', 'waiting', 'ready', 'failed')
SETTINGS = (REFRESH, HOME_GENRE)
SURFACES = (*SETTINGS, *(f'choice_window:{format}' for format in FORMATS))


def _integer(value):
    if type(value) is not int or not 0 <= value < 2 ** 63:
        raise ValueError('Expected a nonnegative integer demand epoch')
    return value


def _strings(value, maximum=100):
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, str) or not item or len(item.encode()) > 1024 for item in value)):
        raise ValueError('Expected a bounded list of nonempty demand identifiers')
    return value


def parse(surface, value, at):
    if at is not None:
        finite(at)
    if surface not in SURFACES or not isinstance(value, dict):
        raise ValueError('Unknown recommendation demand owner or shape')
    if not value:
        return value
    if surface.startswith('choice_window:'):
        if set(value) != {'opportunity_id', 'window'}:
            raise ValueError('Unclassified listening window fields')
        _strings([value['opportunity_id']])
        if (len(value['opportunity_id'].encode()) > 64 or not isinstance(value['window'], str)
                or len(value['window'].encode()) > 1024):
            raise ValueError('Invalid listening window identity')
    elif surface == REFRESH:
        if set(value) != {'epoch', 'exclude'}:
            raise ValueError('Unclassified album refresh fields')
        _integer(value['epoch'])
        _strings(value['exclude'])
    else:
        if 'epoch' not in value or set(value) - {'epoch', 'include_genres', 'avoid_genres', 'held_by_vibe', 'suspended_by_vibe'}:
            raise ValueError('Unclassified Home genre demand fields')
        _integer(value['epoch'])
        for field in ('include_genres', 'avoid_genres'):
            if field in value:
                _strings(value[field])
        for field in ('held_by_vibe', 'suspended_by_vibe'):
            if field in value and type(value[field]) is not bool:
                raise ValueError('Genre hold state requires a boolean')
    return value


def write(conn, surface, value, at):
    album_note_requests._transaction(conn)
    parse(surface, value, at)
    if surface.startswith('choice_window:'):
        evidence_maintenance.write_window(conn, surface.split(':')[1], value, at)
    elif surface == HOME_GENRE and not value and at is None:
        worker_settings.save(conn, surface, None)
    else:
        worker_settings.save(conn, surface, json.dumps({**value, 'updated_at': at}))


def present(conn, surface):
    if surface in SURFACES and surface.startswith('choice_window:'):
        return evidence_maintenance.window(conn, surface.split(':')[1]) is not None
    if surface in SETTINGS:
        return conn.execute("SELECT 1 FROM settings WHERE key=? AND json_type(value,'$.epoch')='integer'",
                            (surface,)).fetchone() is not None
    raise ValueError('Unknown recommendation demand owner')


def read(conn, surface):
    if surface in SURFACES and surface.startswith('choice_window:'):
        return evidence_maintenance.window(conn, surface.split(':')[1])
    if surface in SETTINGS:
        raw = worker_settings.read(conn, surface)
        if raw is None:
            return None
        value = json.loads(raw)
        value.pop('updated_at')
        return value
    raise ValueError('Unknown recommendation demand owner')
