"""Publish independently verified taste controls without retiring their settings."""
import hashlib

from .. import collection_recovery, experiment_decision_state, taste_control_state
from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from . import taste_controls as preparation
from .document_publication import DocumentPublication, main as run, verify_paged_plan


def verify_files(conn, header, described):
    """Check every retained plan and recovery file before entering a snapshot."""
    if conn.in_transaction:
        raise MigrationError('Taste file verification requires an idle connection')
    source, revision = header['source'], header['preparation_revision']
    fields, value = described
    parts = conn.execute('SELECT position,filename,content_digest,byte_size FROM '+preparation.table(source, 'parts')+
        ' WHERE source=? AND preparation_revision=? ORDER BY position LIMIT ?',
        (source, revision, fields['part_count']+1)).fetchall()
    units = [('part', tuple(row)) for row in parts]
    if len(parts) != fields['part_count']:
        raise MigrationError('Taste plan differs from its original parts')

    def retained(root, kind, owner, state):
        row = (conn.execute('SELECT recovery_filename,recovery_digest,recovery_bytes FROM '+
            preparation.table(source, 'decisions')+' WHERE source=? AND preparation_revision=? AND position=?',
            (source, revision, owner-1)).fetchone() if source == preparation.DECISIONS else
            tuple(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes')))
        encoded = canonical({'format':collection_recovery.FORMAT, 'kind':kind, 'owner':str(owner), 'state':state})
        if row is None or (row[1], row[2]) != (hashlib.sha256(encoded).hexdigest(), len(encoded)):
            raise MigrationError('Taste recovery differs from its original evidence')
        collection_recovery.read(*row, kind=kind, owner=owner)
        return tuple(row)

    if source == preparation.DECISIONS:
        units.extend(preparation.decisions(None, value, fields, retain=retained))
    elif value is not None:
        planner = preparation.checkpoint if source == preparation.CHECKPOINT else preparation.baseline
        units.extend(planner(conn, None, value, fields, retain=retained))
    fields['unit_count'] = len(units)
    verify_paged_plan(source, header, fields, units)
    return fields, units


def verify_rows(conn, header, described):
    fields, units = described
    source, revision = header['source'], header['preparation_revision']
    if any(header[key] != (float(value) if key == 'saved_at' and value is not None else value)
           for key, value in fields.items()):
        raise MigrationError('Prepared taste metadata differs from its original')
    suffixes = {'part':'parts', 'weight':'weights', 'entry':'entries', 'decision':'decisions'}
    members = 'weight' if source == preparation.CHECKPOINT else 'entry' if source == preparation.BASELINE else 'decision'
    readers = {kind:conn.execute('SELECT * FROM '+preparation.table(source, suffixes[kind])+
        ' WHERE source=? AND preparation_revision=? ORDER BY position', (source, revision))
        for kind in ('part', members)}
    for kind, values in units:
        numeric = {'part':(), 'weight':(9, 10), 'entry':(5, 7), 'decision':(4,)}[kind]
        values = tuple(float(value) if index in numeric and value is not None else value
                       for index, value in enumerate(values))
        row = readers[kind].fetchone()
        if row is None or tuple(row) != (source, revision, *values):
            raise MigrationError('Prepared taste member differs from its original or references')
    if any(rows.fetchone() is not None for rows in readers.values()):
        raise MigrationError('Prepared taste control contains extra members')


class TastePublication(DocumentPublication):
    def definitions(self):
        yield from super().definitions()
        # Optional references have their own preparation capture, including when
        # a historical upgrade first adds them. No dynamic publication DDL is
        # needed on owners that do not exist in the older installed format.
        name = self.tables[1]+'_preparation_input'
        yield name, (f'CREATE TRIGGER {name} AFTER UPDATE ON main.{self.job.state} '
            'WHEN NEW.input_revision<>OLD.input_revision BEGIN '
            f'UPDATE {self.tables[1]} SET active=0 WHERE active=1; END')

    def validate_preparation(self, conn):
        from ..schema_contract import tokens as _tokens
        optional = {f'prepare_{self.job.name}_dependency_{owner}_{operation}'
            for owner in self.job.optional_dependencies for operation in ('insert', 'update', 'delete')}
        for name, sql in self.job.definitions(conn):
            row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
            # Newly added optional owners require explicit worker installation;
            # they withdraw selection below but never prevent regular DDL.
            if row is None and name in optional:
                continue
            if row is None or _tokens(row[0]) != _tokens(sql):
                raise MigrationError('Incompatible preparation object: '+name)

    def selected(self, conn):
        revision = super().selected(conn)
        if revision is None:
            return None
        for owner in self.job.optional_dependencies:
            if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (owner,)).fetchone():
                for operation in ('insert', 'update', 'delete'):
                    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',
                            (f'prepare_{self.job.name}_dependency_{owner}_{operation}',)).fetchone():
                        return None
        return revision


OWNERS = {name:TastePublication(source, preparation, preparation.JOBS[name],
    experiment_decision_state.available if name == 'decisions' else taste_control_state.available,
    verify_rows, generation_column='preparation_revision', verify_files=verify_files)
    for name, source in preparation.SOURCES.items()}
SOURCES = {owner.source:owner for owner in OWNERS.values()}


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    return run(OWNERS, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
