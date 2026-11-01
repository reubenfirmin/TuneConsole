"""Reject oversized SQL rows before transferring their payload to Python."""
import json

from ..migrations.runner import MigrationError


def rows(conn, query, parameters=(), *, byte_limit, message):
    cursor = conn.execute('SELECT * FROM ('+query+') LIMIT 0', parameters)
    columns = [field[0] for field in cursor.description]
    quote = lambda name: '"'+name.replace('"', '""')+'"'
    size = '+'.join(f"CASE WHEN typeof({quote(name)}) IN ('text','blob') "
        f'THEN length(CAST({quote(name)} AS BLOB)) ELSE 0 END' for name in columns)
    overflow = '('+size+')>'+str(byte_limit)
    projection = ','.join(f'CASE WHEN {overflow} THEN NULL ELSE {quote(name)} END AS {quote(name)}' for name in columns)
    result = []
    for row in conn.execute(f'SELECT {projection},({overflow}) AS _oversized FROM ({query})', parameters):
        value = dict(row)
        if value.pop('_oversized') or len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > byte_limit:
            raise MigrationError(message)
        result.append(value)
    return result
