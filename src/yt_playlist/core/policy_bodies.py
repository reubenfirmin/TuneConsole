"""File coordinates for the shared part of compact recommendation policies."""
import json

from . import worker_settings


PREFIX = 'recommendation_policy_body:'
PATTERN = PREFIX + '*'
VIEW = 'recommendation_policy_body_refs'
COLUMNS = ('configuration_filename', 'content_digest', 'byte_size', 'configuration_offset', 'created_at')


def read(conn, version):
    raw = worker_settings.read(conn, PREFIX + version)
    return None if raw is None else dict(zip(COLUMNS, json.loads(raw)))


def save(conn, version, filename, digest, size, offset, created):
    worker_settings.save(conn, PREFIX + version, json.dumps([filename, digest, size, offset, created],
                                                           separators=(',', ':'), allow_nan=False))


def delete(conn, versions):
    conn.executemany('DELETE FROM settings WHERE key=?', ((PREFIX + version,) for version in versions))


def view():
    version = f'substr(s.key,{len(PREFIX) + 1})'
    fields = ','.join(f"json_extract(s.value,'$[{offset}]') AS {field}" for offset, field in enumerate(COLUMNS))
    return f'''CREATE VIEW {VIEW} AS SELECT {version} AS version,{fields},
        EXISTS(SELECT 1 FROM recommendation_policies p WHERE p.base_version={version} AND p.served=1) AS served
        FROM settings s WHERE s.key GLOB '{PATTERN}' '''.strip()
