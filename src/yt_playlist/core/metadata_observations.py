"""Source-native evidence for descriptive profiles, independent of catalog selection.

Classifier probabilities are values, not calibrated reliability. Entity IDs and
match evidence describe the lookup without asserting a verified recording match.
Unknown provider/model versions remain unknown rather than borrowing app versions.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math

SCHEMA = 1
SCOPES = frozenset({'recording', 'release', 'artist', 'genre', 'item'})
KINDS = frozenset({'reported', 'measurement', 'prediction', 'curated', 'inherited', 'derived'})


class SubjectChanged(ValueError):
    """The lookup's catalog entry no longer belongs to its original recording."""


@dataclass(frozen=True)
class MetadataObservation:
    field: str
    value: object
    scope: str
    entity_id: str
    kind: str
    unit: str | None = None
    source_path: str | None = None
    provider_version: object = None
    model_version: object = None
    derivation_version: str | None = None
    dependencies: tuple[str, ...] = ()
    match_evidence: dict = field(default_factory=dict)

    def payload(self):
        if not isinstance(self.field, str) or not self.field.strip():
            raise ValueError('Observation field is required')
        if self.scope not in SCOPES or self.kind not in KINDS:
            raise ValueError('Unknown observation scope or evidence kind')
        if not isinstance(self.entity_id, str) or not self.entity_id.strip():
            raise ValueError('Observation source entity ID is required')
        if self.kind in {'derived', 'inherited'} and (
                not isinstance(self.derivation_version, str) or not self.derivation_version.strip()):
            raise ValueError('Derived evidence requires a derivation version')
        if not isinstance(self.match_evidence, dict):
            raise ValueError('Recording-match evidence must be an object')
        payload = {'schema': SCHEMA, **asdict(self)}
        # Detach caller-owned mappings and reject non-JSON/nonfinite evidence.
        return json.loads(json.dumps(payload, sort_keys=True, allow_nan=False))


def record(conn, entry_id, provider, observations, *, run_id, now, expected_recording_id=None):
    """Append evidence within the caller's transaction; retries of a run are idempotent.

    The entry/recording is the lookup context. The observation's declared scope
    identifies what the source actually described, including artist/release tags.
    This never selects a typed field or replaces an explicit catalog correction.
    """
    if (not isinstance(provider, str) or not provider.strip()
            or not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError('Observation provider and delivery ID are required')
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('Observation time must be finite')
    subject = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
    if subject is None:
        raise ValueError('Unknown observation catalog entry')
    if expected_recording_id is not None and subject[0] != expected_recording_id:
        raise SubjectChanged('Observation catalog subject changed during lookup')
    rows = []
    for observation in observations:
        payload = observation.payload()
        value = json.dumps(payload.pop('value'), sort_keys=True, allow_nan=False)
        name = payload.pop('field')
        payload['delivery_id'] = run_id
        provenance = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        delivery = json.dumps([run_id, entry_id, provider, name, value, provenance], separators=(',', ':'))
        key = hashlib.sha256(delivery.encode()).hexdigest()
        rows.append((entry_id, subject[0], name, provider, value, now, provenance, key))
    conn.executemany('INSERT INTO metadata_findings(entry_id,recording_id,field,provider,value_json,'
                     'observed_at,provenance_json,observation_key) VALUES (?,?,?,?,?,?,?,?) '
                     'ON CONFLICT(observation_key) WHERE observation_key IS NOT NULL DO NOTHING', rows)
