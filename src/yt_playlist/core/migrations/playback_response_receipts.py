"""Index verified playback on the same receipts that select its response files."""
import json

from .. import evidence_ledger, fao, playback_bindings as bindings, playlist_scopes, worker_settings
from ..evidence_record_files import EvidenceRecordFiles
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .playback_binding_storage import KEY as PREVIOUS_KEY
from .runner import PreparationChanged


NAME = 'own_playback_attribution_on_choice_response_receipts'
POINT = 'ix_choice_response_playback_coordinates'
WINDOW = 'ix_choice_response_playback_window'


def predecessor_objects(conn):
    return [conn.execute("SELECT sql FROM sqlite_schema WHERE name='collection_file_writes'").fetchone()[0]]


def _binding_key(row):
    return tuple(row[field] for field in (*bindings.ORDER, 'bound_at', 'opportunity_id', 'option_id'))


def prepare(conn, _version):
    raw = worker_settings.read(conn, PREVIOUS_KEY)
    if not raw:
        return raw, ()
    wanted = {_binding_key(row) for row in json.loads(raw)}
    references = {(row[0], row[1]): tuple(row[2:]) for row in conn.execute("""
        SELECT owner_id->>0,owner_id->>1,file_address,content_digest,byte_size FROM collection_file_writes
        WHERE kind='listening-evidence' AND selected=1 AND owner_id->>0='response'""")}
    files = EvidenceRecordFiles(evidence_ledger.directory(conn), 0, references=references)
    matches = []
    with files.retained():
        for kind, identity in references:
            try:
                record = files.get(kind, identity)
            except fao.CorruptFile:
                continue
            if record is None or (coordinates := bindings.coordinates(record)) is None:
                continue
            account, playlist, video, at = coordinates
            try:
                scope = playlist_scopes.resolve(conn, playlist, account)
            except ValueError:
                continue
            key = account, scope, video, at, record['opportunity_id'], record['option_id']
            if key in wanted:
                wanted.remove(key)
                matches.append((references[kind, identity][0], record))
                if not wanted:
                    break
    return raw, matches


def install(conn, context):
    raw, verified = context.prepared
    if worker_settings.read(conn, PREVIOUS_KEY) != raw:
        raise PreparationChanged()
    conn.execute('ALTER TABLE collection_file_writes ADD COLUMN playback_account_id INTEGER')
    conn.execute('ALTER TABLE collection_file_writes ADD COLUMN playback_scope_id TEXT')
    conn.execute('ALTER TABLE collection_file_writes ADD COLUMN playback_video_id TEXT')
    conn.execute("""ALTER TABLE collection_file_writes ADD COLUMN playback_at REAL CHECK(
        CASE WHEN playback_account_id IS NULL THEN playback_scope_id IS NULL AND playback_video_id IS NULL
            AND playback_at IS NULL ELSE COALESCE(kind='listening-evidence' AND owner_id->>0='response'
            AND file_status='ready' AND typeof(playback_account_id)='integer'
            AND length(playback_scope_id)>0 AND length(playback_video_id)>0
            AND typeof(playback_at) IN ('integer','real') AND abs(playback_at)<=1.7976931348623157e308,0) END)""")
    conn.execute(f'CREATE INDEX {POINT} ON collection_file_writes('
                 f'playback_account_id,playback_scope_id,playback_video_id,playback_at) WHERE {bindings.SELECTED}')
    conn.execute(f'CREATE INDEX {WINDOW} ON collection_file_writes('
                 f'playback_account_id,playback_at) WHERE {bindings.SELECTED}')
    if raw:
        bindings.save(conn, json.loads(raw))
        for identity, record in verified:
            bindings.attach(conn, identity, record)
        worker_settings.save(conn, PREVIOUS_KEY, None)


def validate(conn):
    if set(definitions(conn, (POINT, WINDOW))) != {POINT, WINDOW}:
        raise MigrationError('Verified playback response indexes are missing')
