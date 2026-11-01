"""Publish verified TV state independently while retaining compatible writers."""
from .. import tv_recommendation_state as state
from ..migrations.runner import MigrationError
from . import artist_lookup, tv_state as preparation
from .document_publication import DocumentPublication, main as run


class TVPreparation:
    MAX_BYTES = state.MAX_BYTES
    describe = staticmethod(state.decode)
    read_original = staticmethod(preparation.read_original)

    @staticmethod
    def tables(source):
        header, current, parts = preparation.tables(source)
        return header, parts, current


def native(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='tv_pool_state'").fetchone() is not None


def verify_rows(conn, header, value):
    source, revision = header['source'], header['preparation_revision']
    if source in ('tv_artist_artwork', 'tv_artist_artwork_requests'):
        artist_lookup.require(conn)
    built_at = conn.execute('SELECT built_at FROM main.rec_proposals WHERE surface=?', (source,)).fetchone()[0]
    if header['built_at'] != (float(built_at) if built_at is not None else None):
        raise MigrationError('Prepared TV observation time differs from its source')
    rows = {}
    for relation in preparation.RELATIONS[source]:
        table = preparation.relation(relation)
        columns = conn.execute('PRAGMA main.table_info('+table+')').fetchall()
        keys = sorted((column['pk'], column['name']) for column in columns if column['pk'])
        order = ','.join('"'+name+'"' for _, name in keys)
        rows[relation] = (columns, conn.execute('SELECT * FROM '+table+
            ' WHERE source=? AND preparation_revision=? ORDER BY '+order, (source, revision)))
    count = 0
    for relation, fields, kind in preparation.rows(source, value, built_at):
        preparation.resolve(conn, fields, kind)
        fields.update(source=source, preparation_revision=revision)
        columns, reader = rows[relation]
        expected = tuple(float(fields[column['name']]) if column['type'] == 'REAL' and fields.get(column['name']) is not None
                         else fields.get(column['name']) for column in columns)
        row = reader.fetchone()
        if row is None or tuple(row) != expected:
            raise MigrationError('Prepared TV member differs from its original or references')
        count += 1
    if header['row_count'] != count or any(reader.fetchone() is not None for _, reader in rows.values()):
        raise MigrationError('Prepared TV state has missing or extra members')


class TVPublication(DocumentPublication):
    def read_source(self, conn):
        return conn.execute("SELECT payload IS NULL,COALESCE(substr(CAST(payload AS BLOB),1,?),X'') "
            'FROM main.rec_proposals WHERE surface=?', (state.MAX_BYTES+1, self.source)).fetchone()


OWNERS = {name:TVPublication(source, TVPreparation, preparation.JOBS[name], native,
    verify_rows, generation_column='preparation_revision') for name, source in preparation.SOURCES.items()}
SOURCES = {owner.source:owner for owner in OWNERS.values()}


def validate_installed(conn):
    for owner in OWNERS.values():
        owner.validate_installed(conn)


def main(argv=None):
    return run(OWNERS, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
