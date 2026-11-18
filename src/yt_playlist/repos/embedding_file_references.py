"""Installed vector order, conversion inputs and file heads; SQL only."""
from . import embedding_file_writes
from .connection import ConnectionRepo


def legacy_relation(conn, kind):
    if kind not in {'recording', 'artist'}:
        raise ValueError('Invalid embedding kind')
    table = kind + '_embeddings'
    ordered = conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recording_embedding_legacy_order'").fetchone()
    if ordered:
        return (f'(SELECT e.*,coalesce(o.insertion_order,e.rowid) AS insertion_order FROM {table} e '
                f'LEFT JOIN {kind}_embedding_legacy_order o ON o.row_id=e.rowid)')
    return f'(SELECT *,rowid AS insertion_order FROM {table})'


def publish_prepared(conn, kind, space, version, rows):
    if not conn.in_transaction:
        raise RuntimeError('Embedding file publication requires a writer reservation')
    conn.executemany(f'DELETE FROM {kind}_embeddings WHERE {kind}_id=? AND space=?',
                     [(row[0], space) for row in rows])
    native = embedding_file_writes.native(conn)
    current = [row for row in rows if len(row) == 7]
    if current:
        embedding_file_writes.complete(conn, kind, current)
    columns = f'{kind}_id,space,model_version,dimensions,publication,position,insertion_order,built_at'
    columns += ',file_id' if native else ''
    values = []
    for row in rows:
        subject, dimensions, publication, position, order, built = row[:6]
        value = (subject, space, version, dimensions, publication, position, order, built)
        if native:
            value += (row[6] if len(row) == 7 else None,)
        values.append(value)
    conn.executemany(f'INSERT INTO {kind}_embedding_files({columns}) VALUES (' + ','.join('?' for _ in columns.split(',')) + ') '
        f'ON CONFLICT({kind}_id,space,model_version) DO UPDATE SET publication=excluded.publication,'
        'position=excluded.position,built_at=excluded.built_at' + (',file_id=excluded.file_id' if native else ''), values)


class EmbeddingSources(ConnectionRepo):
    def __init__(self, conn):
        super().__init__(conn)
        with self._lock:
            if not self.conn.in_transaction:
                raise RuntimeError('Embedding file publication requires a writer reservation')

    def page(self, kind, limit):
        with self._lock:
            return self.conn.execute(f'SELECT insertion_order,{kind}_id,space,model_version,dimensions,vector,built_at '
                f'FROM {legacy_relation(self.conn, kind)} ORDER BY insertion_order LIMIT ?', (limit,)).fetchall()

    def publish(self, kind, space, version, rows):
        with self._lock:
            publish_prepared(self.conn, kind, space, version, rows)
