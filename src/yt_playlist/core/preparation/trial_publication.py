"""Publish retained trial decisions, monitoring baselines and held assignments."""
from .. import model_comparison_state as state
from ..schema_contract import MigrationError
from . import model_trials as preparation
from .document_publication import DocumentPublication, main as run


class TrialPreparation:
    MAX_BYTES = preparation.MAX_BYTES
    describe = staticmethod(state.decode)

    @staticmethod
    def tables(source):
        header, current, originals = preparation.tables(source)
        return header, originals, current

    @staticmethod
    def read_original(conn, header):
        original = conn.execute('SELECT raw_value FROM '+preparation.tables(header['source'])[2]+
            ' WHERE source=? AND digest=?', (header['source'], header['source_digest'])).fetchone()
        if original is None:
            raise MigrationError('Retained model trial original is missing')
        return original[0]


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='model_comparison_plan'").fetchone() is not None


def verify_rows(conn, header, value):
    source, revision = header['source'], header['preparation_revision']
    row = conn.execute('SELECT surface,CAST(payload AS BLOB) AS payload,built_at '
        'FROM main.rec_proposals WHERE surface=?', (source,)).fetchone()
    if row is None:
        raise MigrationError('Model trial source is missing')
    plan = preparation.prepare(conn, row)
    fields = dict(plan.fields)
    identity = fields['comparison_run_id' if source == 'model_album_assignment' else 'run_id']
    reference = state._run(conn, identity)
    fields.update(source=source, preparation_revision=revision, source_digest=plan.digest,
        run_ref=reference, run_resolution=None if identity is None or reference is not None else 'missing_run',
        built_at=row['built_at'])
    columns = conn.execute('PRAGMA main.table_info('+preparation.tables(source)[0]+')').fetchall()
    expected = tuple(float(fields[column['name']]) if column['type'] == 'REAL' and fields.get(column['name']) is not None
        else fields.get(column['name']) for column in columns)
    if tuple(header) != expected:
        raise MigrationError('Model trial fields differ from their original or run reference')
    original = conn.execute('SELECT raw_value,built_at FROM '+preparation.tables(source)[2]+
        ' WHERE source=? AND digest=?', (source, plan.digest)).fetchone()
    if original is None or tuple(original) != (plan.raw, row['built_at']):
        raise MigrationError('Model trial original or observation time differs from its source')
    if source == 'model_listening_monitor':
        actual = list(map(tuple, conn.execute('SELECT position,value FROM '+preparation.BASELINE+
            ' WHERE source=? AND preparation_revision=? ORDER BY position LIMIT ?',
            (source, revision, preparation.MAX_BASELINE+1))))
        if actual != list(enumerate(plan.units)):
            raise MigrationError('Model trial baseline differs from its original order or values')


class TrialPublication(DocumentPublication):
    def read_source(self, conn):
        return conn.execute("SELECT payload IS NULL,COALESCE(substr(CAST(payload AS BLOB),1,?),X'') "
            'FROM main.rec_proposals WHERE surface=?', (preparation.MAX_BYTES+1, self.source)).fetchone()


OWNERS = {name:TrialPublication(source, TrialPreparation, preparation.JOBS[name], native, verify_rows,
    generation_column='preparation_revision', immutable_originals=(preparation.tables(source)[2],))
    for name, source in preparation.SURFACES.items()}
SOURCES = {owner.source:owner for owner in OWNERS.values()}


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    return run(OWNERS, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
