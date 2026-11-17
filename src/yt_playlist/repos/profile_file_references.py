"""Profile heads and bounded inline conversion inputs; SQL only."""
from .profile_file_writes import native
from .connection import ConnectionRepo

KINDS = {'recording': ('rec_musical_profiles', 'identity_key', 'recording_profile_files', 'recording_id'),
         'source': ('rec_musical_source_profiles', 'source_key', 'source_profile_files', 'source_key')}


def identity(conn, kind, key):
    if kind == 'source':
        return key
    row = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
    if row is None:
        raise ValueError('Unknown recording profile owner')
    return row[0]


def install_references(conn, kind, references):
    table, key_column, marker, subject = KINDS[kind]
    has_ids = native(conn)
    for reference in references:
        key, owner, publication, size, version, digest = reference[:6]
        file_id = reference[6] if len(reference) == 8 else None
        head = conn.execute(f'SELECT version,digest FROM {table} WHERE {key_column}=?', (key,)).fetchone()
        if head is None or tuple(head) != (version, digest):
            raise ValueError('Stale musical-profile publication')
        if identity(conn, kind, key) != owner:
            raise ValueError('Musical-profile owner changed')
        columns, values = f'{subject},publication,byte_size', (owner, publication, size)
        updates = 'publication=excluded.publication,byte_size=excluded.byte_size'
        if has_ids:
            columns += ',file_id'
            values += (file_id,)
            updates += ',file_id=excluded.file_id'
        conn.execute(f'INSERT INTO {marker}({columns}) VALUES ({",".join("?" for _ in values)}) '
                     f'ON CONFLICT({subject}) DO UPDATE SET {updates}', values)
        conn.execute(f"UPDATE {table} SET payload='' WHERE {key_column}=?", (key,))


class ProfileSources(ConnectionRepo):
    def __init__(self, conn):
        super().__init__(conn)
        with self._lock:
            if not self.conn.in_transaction:
                raise RuntimeError('Profile publication requires the writer reservation')

    def page(self, kind, after, limit):
        with self._lock:
            table, key, _, _ = KINDS[kind]
            rows = self.conn.execute(f"SELECT {key},payload,version,digest FROM {table} WHERE {key}>? "
                f"AND payload<>'' ORDER BY {key} LIMIT ?", (after, limit)).fetchall()
            return [(key, identity(self.conn, kind, key), raw, version, digest)
                    for key, raw, version, digest in rows]

    def publish(self, kind, references):
        with self._lock:
            install_references(self.conn, kind, references)
