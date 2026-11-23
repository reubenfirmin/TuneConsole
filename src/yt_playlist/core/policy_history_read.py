"""Read archived policy evidence without depending on its retention writer."""
import json

from yt_playlist.core import file_collection, file_paths
from . import fao, file_map, worker_settings
from . import policy_history_files
from .policy_config import BASE_FIELD
from .policy_paths import record_path


ARCHIVE_HEAD = 'policy_history_publication'
NEXT_ARCHIVE_FILE = 'policy_history_last_file'
PREFIX = 'recommendation_policy_archive:'
PATTERN = PREFIX + '*'
VIEW = 'recommendation_policy_archive_refs'
FIELDS = ('id', 'created_at', 'state', 'archive_file_id', 'archive_digest', 'archive_byte_size', 'shared_archive_head')


def publication(conn):
    row = conn.execute(f'SELECT shared_archive_head FROM {VIEW} WHERE shared_archive_head IS NOT NULL LIMIT 1').fetchone()
    return row[0] if row else None


def directory(conn):
    return file_paths.directory(conn, 'policy-history')


def available(conn):
    return conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (VIEW,)).fetchone() is not None


def view():
    fields = ','.join(f"value->>'$[{index}]' AS {field}" for index, field in enumerate(FIELDS))
    return (f"CREATE VIEW {VIEW} AS SELECT substr(key,{len(PREFIX) + 1}) AS version,{fields} "
            f"FROM settings WHERE key GLOB '{PATTERN}'")


def entry(conn, version):
    value = worker_settings.read(conn, PREFIX + version)
    return None if value is None else dict(zip(FIELDS, json.loads(value)))


def header(conn, version):
    row = entry(conn, version)
    if row is None:
        return None
    identity = row['archive_file_id']
    return {'state': row['state'], 'publication': row['shared_archive_head'],
            'file': {'id': identity, 'content_digest': row['archive_digest'],
                     'byte_size': row['archive_byte_size']} if identity is not None else None}


def document(conn, version):
    root = directory(conn)
    with file_collection.retained_reader(root):
        return read(root, version, header(conn, version))


def read(root, version, saved):
    if saved is None or saved['state'] == 'expired':
        return None
    file = saved.get('file')
    value = (policy_history_files.read(root, version, file) if file is not None
             else file_map.get(root, saved['publication'], version))
    if value is None or value.get('state') != 'archived':
        raise fao.CorruptFile('Required historical policy is missing')
    return value


def archive_ids(conn):
    return (row[0] for row in conn.execute(f'SELECT archive_file_id FROM {VIEW} WHERE archive_file_id IS NOT NULL'))


def references(conn, *, root=None):
    root = directory(conn) if root is None else root
    yield from file_map.pages(root, publication(conn))
    yield from (record_path(root, identity) for identity in archive_ids(conn))


def unpack(saved):
    value = dict(saved['configuration'])
    value.pop(BASE_FIELD, None)
    return {**(saved['base']['configuration'] if saved['base'] else {}), **value}
