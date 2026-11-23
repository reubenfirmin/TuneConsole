"""Store each retained group context as one document under its evidence identity."""
import json

from .. import group_evidence
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from . import group_context_owners


NAME = 'store_group_evidence_with_its_context'
RETIRED = group_context_owners.TABLE
PROPOSAL_GUARD = group_context_owners.PROPOSAL_GUARD
OWNERS = ('group_generations', 'group_versions')
PIN = 'evidence_holds'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND (tbl_name=? OR name IN (?,?,?,?)) ORDER BY type,name',
        (RETIRED, *OWNERS, group_evidence.CONTEXTS, PROPOSAL_GUARD))]


def _guards(conn):
    prefix = group_evidence.PREFIX
    conn.execute(f'''CREATE TRIGGER group_evidence_setting_insert BEFORE INSERT ON settings
        WHEN NEW.key GLOB '{prefix}*' AND (
            (NEW.value->>4 IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM group_generations WHERE id=NEW.value->>4))
            OR EXISTS(SELECT 1 FROM json_each(NEW.value,'$[5]') reference
                WHERE reference.value->>1='resolved_version' AND NOT EXISTS(
                    SELECT 1 FROM group_versions WHERE id=reference.value->>2
                      AND external_version=reference.value->>0))
        ) BEGIN SELECT RAISE(ABORT,'Musical group evidence names a missing generation or version'); END''')
    conn.execute(f'''CREATE TRIGGER group_evidence_setting_update BEFORE UPDATE ON settings
        WHEN OLD.key GLOB '{prefix}*' OR NEW.key GLOB '{prefix}*'
        BEGIN SELECT RAISE(ABORT,'Musical group evidence references are immutable'); END''')
    for event, row, delta in (('INSERT', 'NEW', '+1'), ('DELETE', 'OLD', '-1')):
        conn.execute(f'''CREATE TRIGGER group_evidence_setting_{event.lower()}_holds AFTER {event} ON settings
            WHEN {row}.key GLOB '{prefix}*' BEGIN
                UPDATE group_generations SET {PIN}={PIN}{delta} WHERE id={row}.value->>4;
                UPDATE group_versions SET {PIN}={PIN}{delta} WHERE id IN (
                    SELECT reference.value->>2 FROM json_each({row}.value,'$[5]') reference
                    WHERE reference.value->>1='resolved_version');
            END''')
    for table in OWNERS:
        keys = ('id', 'external_version') if table == 'group_versions' else ('id',)
        identity = ','.join(keys)
        changed = ' OR '.join(f'OLD.{key} IS NOT NEW.{key}' for key in keys)
        for event, clause, extra in (('delete', 'DELETE', ''), ('update', f'UPDATE OF {identity}', f' AND ({changed})')):
            conn.execute(f'''CREATE TRIGGER retained_{table}_evidence_{event} BEFORE {clause} ON {table}
                WHEN OLD.{PIN}>0{extra} BEGIN
                    SELECT RAISE(ABORT,'Musical group evidence retains this identity'); END''')


def install(conn, _context):
    for table in OWNERS:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {PIN} INTEGER NOT NULL DEFAULT 0 CHECK({PIN}>=0)')
    conn.execute(f'CREATE INDEX ix_group_version_evidence ON group_versions(generation_id) WHERE {PIN}>0')
    _guards(conn)

    details = ','.join(group_evidence.CONTEXT_FIELDS)
    for kind, identity, *context in conn.execute(f'SELECT kind,external_key,{details} FROM {RETIRED} '
                                                  "WHERE reference_kind='context'"):
        references = [tuple(row) for row in conn.execute(
            f'SELECT reference_key,reference_kind,group_version_id,reason FROM {RETIRED} '
            "WHERE kind=? AND external_key=? AND reference_kind<>'context' ORDER BY reference_kind,reference_key",
            (kind, identity))]
        conn.execute('INSERT INTO settings(key,value) VALUES (?,?)',
                     (group_evidence.setting_key(kind, identity), json.dumps([*context, references], allow_nan=False)))

    conn.execute(f'DROP VIEW {group_evidence.CONTEXTS}')
    conn.execute(group_evidence.contexts_view())
    conn.execute(group_evidence.references_view())
    old = f"SELECT kind,external_key,{details} FROM {RETIRED} WHERE reference_kind='context'"
    new = group_evidence.setting_contexts()
    columns = 'kind,external_key,reference_key,reference_kind,group_version_id,reason'
    old_refs = f"SELECT {columns} FROM {RETIRED} WHERE reference_kind<>'context'"
    new_refs = f'SELECT {columns} FROM {group_evidence.REFERENCES}'
    for before, after in ((old, new), (old_refs, new_refs)):
        if conn.execute(f'{before} EXCEPT {after}').fetchone() or conn.execute(f'{after} EXCEPT {before}').fetchone():
            raise MigrationError('Group evidence changed during its storage upgrade')

    conn.execute(f'DROP TRIGGER {PROPOSAL_GUARD}')
    conn.execute(f'DROP TABLE {RETIRED}')
    keys = ','.join(f"'{group_evidence.setting_key(kind, 'proposal:')}'||OLD.surface||':{field}'"
        for kind in ('prepared', 'comparison') for field in ('context', 'choice_context', 'album_choice_context'))
    conn.execute(f'''CREATE TRIGGER {PROPOSAL_GUARD} BEFORE DELETE ON rec_proposals BEGIN
        DELETE FROM settings WHERE key IN ({keys}); END''')


def validate(conn):
    names = (group_evidence.CONTEXTS, group_evidence.REFERENCES, PROPOSAL_GUARD)
    if set(definitions(conn, (*names, RETIRED))) != set(names):
        raise MigrationError('Incomplete group evidence settings cutover')
