"""Installed vector order, conversion inputs and file heads; SQL only."""
from .connection import ConnectionRepo


def publish_prepared(conn, kind, space, version, rows):
    if not conn.in_transaction:
        raise RuntimeError('Embedding file publication requires a writer reservation')
    columns = f'{kind}_id,space,model_version,dimensions,publication,position,insertion_order,built_at,file_id'
    values = [(subject, space, version, dimensions, publication, position, order, built, None)
              for subject, dimensions, publication, position, order, built in rows]
    conn.executemany(f'INSERT INTO {kind}_embedding_files({columns}) VALUES (?,?,?,?,?,?,?,?,?) '
        f'ON CONFLICT({kind}_id,space,model_version) DO UPDATE SET publication=excluded.publication,'
        'position=excluded.position,built_at=excluded.built_at,file_id=excluded.file_id', values)


class EmbeddingSources(ConnectionRepo):
    def __init__(self, conn):
        super().__init__(conn)
        with self._lock:
            if not self.conn.in_transaction:
                raise RuntimeError('Embedding file publication requires a writer reservation')

    def page(self, kind, limit):
        if kind not in ('recording', 'artist'):
            raise ValueError('Invalid embedding kind')
        with self._lock:
            return self.conn.execute(f'SELECT insertion_order,{kind}_id,space,model_version,dimensions,vector,built_at '
                f'FROM {kind}_embeddings ORDER BY insertion_order LIMIT ?', (limit,)).fetchall()

    def publish(self, kind, space, version, rows):
        with self._lock:
            self.conn.executemany(f'DELETE FROM {kind}_embeddings WHERE {kind}_id=? AND space=?',
                                  [(row[0], space) for row in rows])
            publish_prepared(self.conn, kind, space, version, rows)
