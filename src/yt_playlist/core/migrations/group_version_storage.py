"""Retire group projections already served by definitions and membership."""
from ..schema_contract import MigrationError
from ..schema_inspection import objects


NAME = 'retire_duplicate_group_version_storage'
TABLES = ('group_assignments', 'unresolved_group_assignments', 'group_representatives')


def install(conn, _context):
    for table in TABLES:
        conn.execute('DROP TABLE ' + table)


def validate(conn):
    retired = next(iter(objects(conn, TABLES)), None)
    if retired is not None:
        raise MigrationError(f'Superseded musical group table remains: {retired}')
