"""Published recording and artist model state in the current schema."""
import json
from dataclasses import dataclass
from enum import Enum

from .db_revisions import setting_reads


KEYS = {'recording': 'recording_model_state', 'artist': 'artist_model_state'}
REFERENCE_FIELDS = ('recovery_filename', 'recovery_digest', 'recovery_bytes')


class ModelField(Enum):
    UNCHANGED = 0


UNCHANGED = ModelField.UNCHANGED
Reference = tuple[str, str, int]


@dataclass(frozen=True)
class RecordingModelState:
    generation: str | None = None
    input_fingerprint: str | None = None
    genre_revision: str | None = None
    fit_fingerprint: str | None = None
    reference: Reference | None = None


@dataclass(frozen=True)
class ArtistModelState:
    fit_fingerprint: str | None = None
    reference: Reference | None = None


@dataclass(frozen=True)
class RecordingModelUpdate:
    encoder: str | None | ModelField = UNCHANGED
    generation: str | None | ModelField = UNCHANGED
    input_fingerprint: str | None | ModelField = UNCHANGED
    genre_revision: str | None | ModelField = UNCHANGED
    fit_fingerprint: str | None | ModelField = UNCHANGED


@dataclass(frozen=True)
class ArtistModelUpdate:
    encoder: str | None | ModelField = UNCHANGED
    fit_fingerprint: str | None | ModelField = UNCHANGED


def validate_encoder(encoder: str | None | ModelField) -> None:
    if encoder is UNCHANGED or encoder is None or encoder == '':
        return
    if not isinstance(encoder, str) or not isinstance(json.loads(encoder), dict):
        raise ValueError('Content encoder must be a supported JSON object')


def recording(conn) -> RecordingModelState:
    state = _read(conn, 'recording')
    return RecordingModelState(state.get('generation'), state.get('input_fingerprint'),
                               state.get('genre_revision'), state.get('fit_fingerprint'), _reference(state))


def artist(conn) -> ArtistModelState:
    state = _read(conn, 'artist')
    return ArtistModelState(state.get('fit_fingerprint'), _reference(state))


def _read(conn, kind):
    key = KEYS[kind]
    with setting_reads(conn, (key,)):
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else {}


def _reference(state):
    return tuple(state[field] for field in REFERENCE_FIELDS) if state.get('recovery_filename') is not None else None


def references(conn):
    for kind in KEYS:
        reference = _reference(_read(conn, kind))
        if reference is not None:
            yield kind, reference


def relocate(conn, kind, filename):
    conn.execute("UPDATE settings SET value=json_set(value, '$.recovery_filename', ?) WHERE key=?",
                 (filename, KEYS[kind]))


def _updated(current, update):
    return current if update is UNCHANGED else update


def save_recording(conn, update: RecordingModelUpdate, reference: Reference | None | ModelField) -> None:
    _save(conn, 'recording', vars(update), reference)


def save_artist(conn, update: ArtistModelUpdate, reference: Reference | None | ModelField) -> None:
    _save(conn, 'artist', vars(update), reference)


def _save(conn, kind, updates, reference):
    state = _read(conn, kind)
    state.update({field: _updated(state.get(field), value) for field, value in updates.items() if field != 'encoder'})
    state.update(zip(REFERENCE_FIELDS, _updated(_reference(state), reference) or (None, None, None)))
    state['revision'] = state.get('revision', 0) + 1
    conn.execute(
        'INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (KEYS[kind], json.dumps(state, allow_nan=False)))
