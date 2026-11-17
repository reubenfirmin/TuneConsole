"""Bounded genre write guards retained in the ordinary schema history."""
import re

from .schema_contract import MigrationError, tokens

AXIS_CHECK = """COALESCE((kind IN ('genre','family','other','unresolved_genre') AND (
    (kind='genre' AND genre_key IS NOT NULL AND family_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
 OR (kind='family' AND family_key IS NOT NULL AND genre_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
 OR (kind='other' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL AND length(value)>0
     AND value NOT LIKE 'genre:%' AND value NOT LIKE 'family:%' AND resolution_reason IS NULL)
 OR (kind='unresolved_genre' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL
     AND resolution_reason IN ('unknown_genre','ambiguous_genre')))),0)"""


def guards():
    expression = re.sub(r'\b(kind|genre_key|family_key|value|resolution_reason)\b', r'NEW.\1', AXIS_CHECK)
    for operation in ('INSERT', 'UPDATE'):
        name = 'installed_taste_axis_guard_' + operation.lower()
        yield name, ('CREATE TRIGGER '+name+' BEFORE '+operation+' ON main.taste_axes '
            'WHEN ('+expression+") IS NOT 1 BEGIN SELECT RAISE(ABORT,'Invalid future taste axis; historical audit is separate'); END")


def install(conn, context=None):
    for name, sql in guards():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None:
            conn.execute(sql)
        elif tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible installed genre repair: ' + name)


install_guards = install


def validate(conn, *, allow_missing_guards=False):
    for name, sql in guards():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None and allow_missing_guards:
            continue
        if row is None or tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible installed genre repair: ' + name)
