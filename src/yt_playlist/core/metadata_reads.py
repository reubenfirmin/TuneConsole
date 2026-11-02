"""Read retained metadata selections without constructing preparation jobs."""
from contextlib import ExitStack
import json

from . import metadata_contract, metadata_delivery_files, metadata_evidence
from .db_revisions import keyed_reads
from .schema_contract import MigrationError

OWNERS = frozenset(('metadata_findings', 'enrichment_log', 'metadata_decisions',
    'entry_field_choices', 'provider_attempts', 'enrichment_provider_state',
    'recording_identifiers', 'catalog_field_hints', 'recording_field_sources', 'item_field_sources'))


def table(name):
    return 'published_metadata_' + name.removeprefix('prepared_metadata_')


def selected(conn):
    if metadata_contract.runtime_active(conn):
        return None
    if metadata_evidence.available(conn) or not conn.execute(
            'SELECT 1 FROM main.sqlite_schema WHERE name=?', (table('head'),)).fetchone():
        return None
    row = conn.execute('SELECT revision FROM '+table('head')+' WHERE id=1 AND active=1').fetchone()
    return row[0] if row else None


def read_table(conn, owner):
    if owner not in OWNERS:
        raise ValueError('Unknown metadata source owner')
    return table(owner) if selected(conn) is not None else owner


def destination_table(name):
    return 'published_metadata_destination_' + name


def destination_selected(conn, *, store=None):
    with ExitStack() as guards:
        if store is not None:
            for owner in (table('head'), destination_table('head')):
                guards.enter_context(keyed_reads(store, owner, [1]))
        revision = selected(conn)
        if revision is None or not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',
                (destination_table('head'),)).fetchone():
            return None
        row = conn.execute('SELECT revision FROM '+destination_table('head')+' WHERE id=1').fetchone()
        return revision if row and row[0] == revision else None


def operational_fields(conn, owner, key):
    revision = destination_selected(conn)
    if revision is None:
        return None
    row = conn.execute('SELECT d.* FROM '+destination_table('records')+' r JOIN '+destination_table('deliveries')+
        ' d ON d.id=r.delivery_id WHERE r.stage=? AND r.owner=? AND r.source_key=?', (revision, owner, key)).fetchone()
    if row is None:
        return None
    original = metadata_delivery_files.read(row['filename'], row['digest'], row['byte_size'], row['byte_offset'])['row']
    values = json.loads(original['fields_json'])
    fields = conn.execute('SELECT field FROM '+destination_table('fields')+
        ' WHERE stage=? AND owner=? AND source_key=? ORDER BY position', (revision, owner, key)).fetchall()
    if not isinstance(values, dict) or list(values) != [row[0] for row in fields]:
        raise MigrationError('Metadata operational fields differ from their retained delivery')
    return values
