"""Shipped complete destination DDL and seeds, compiled outside startup.

Domain owners remain the maintenance sources. Historical definitions serve
explicit upgrades and the compiler; application initialization reads this data
resource directly. Installed text-version columns retain their existing format
until an explicit rebuild, while new destinations use normalized identities.
"""
from functools import lru_cache
import json
from pathlib import Path

from .schema_contract import MigrationError


FORMAT = 1
DEFINITION = Path(__file__).with_name('schema') / 'current.json'


@lru_cache(maxsize=1)
def definition():
    try:
        document = json.loads(DEFINITION.read_text())
    except (OSError, ValueError) as error:
        raise MigrationError('Cannot read the shipped destination definition') from error
    if (not isinstance(document, dict) or type(document.get('format')) is not int
            or document['format'] != FORMAT):
        raise MigrationError('Unsupported shipped destination definition')
    for key in ('numbered_history', 'regular_history'):
        rows = document.get(key)
        if (not isinstance(rows, list) or not rows
                or any(not isinstance(row, list) or len(row) != 2
                       or type(row[0]) is not int or row[0] != index
                       or not isinstance(row[1], str) or not row[1]
                       for index, row in enumerate(rows, 1))
                or len({row[1] for row in rows}) != len(rows)):
            raise MigrationError('Incomplete shipped destination history: '+key)
    for key in ('installed_statements', 'destination_statements', 'destination_seeds'):
        values = document.get(key)
        if not isinstance(values, list) or not values or any(not isinstance(sql, str) or not sql.strip() for sql in values):
            raise MigrationError('Incomplete shipped destination definition: '+key)
    return document


def statements(*, normalized=True):
    yield from definition()['destination_statements' if normalized else 'installed_statements']


def complete(conn):
    """The complete snapshot applies after every represented bounded change.

    Earlier installed owners keep their frozen numbered contract and ordinary
    bounded admission. This check never advances or rewrites either history.
    """
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='schema_changes'").fetchone():
        return False
    rows = [list(row) for row in conn.execute('SELECT version,name FROM schema_changes ORDER BY version')]
    expected = definition()['regular_history']
    if rows != expected[:len(rows)]:
        raise MigrationError('Regular schema history is not the supported contiguous prefix')
    return len(rows) == len(expected)
