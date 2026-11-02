"""Read archived policy evidence without depending on its retention writer."""
from . import fao, file_map
from .policy_config import BASE_FIELD


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='policy_history_archive'").fetchone() is not None


def directory(conn):
    return fao.directory(conn, 'policy-history')


def head(conn):
    row = conn.execute('SELECT publication FROM policy_history_archive WHERE id=1').fetchone()
    return row[0].hex() if row else None


def document(conn, version):
    row = conn.execute('SELECT state FROM recommendation_policy_history WHERE version=?', (version,)).fetchone()
    if row is None or row[0] == 'expired':
        return None
    with fao.retained_reader(directory(conn)):
        value = file_map.get(directory(conn), head(conn), version)
    if value is None or value.get('state') != 'archived':
        raise fao.CorruptFile('Required historical policy is missing')
    return value


def configuration(conn, version):
    saved = document(conn, version)
    if saved is None:
        return None
    return unpack(saved)


def unpack(saved):
    value = dict(saved['configuration'])
    value.pop(BASE_FIELD, None)
    return {**(saved['base']['configuration'] if saved['base'] else {}), **value}
