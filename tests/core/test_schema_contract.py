"""Schema comparisons preserve SQL literals while normalizing identifiers."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core import schema_definition
from yt_playlist.core.schema_contract import tokens


def test_literals_quoted_names_and_comments_keep_distinct_sql_meanings():
    assert tokens('''-- CREATE TRIGGER ignored
        CREATE /* 'also ignored' */ TABLE "Quoted" (
            `Escaped``Name` TEXT DEFAULT 'MiX''Ed -- /* literal */',
            [Brackets] TEXT CHECK ([Brackets] <> 'Other'))''') == (
        'create', 'table', 'quoted', '(', 'escaped`name', 'text', 'default',
        "'MiX''Ed -- /* literal */'", ',', 'brackets', 'text', 'check', '(',
        'brackets', '<', '>', "'Other'", ')', ')')
    assert tokens("SELECT 'Case'") != tokens("SELECT 'case'")
    assert tokens('SELECT "Escaped""Name" - 1 / 2') == (
        'select', 'escaped"name', '-', '1', '/', '2')
    assert tokens(None) == ()


@pytest.mark.parametrize('constraint', [
    'CHECK (parent_id > 0)',
    'FOREIGN KEY (parent_id) REFERENCES parent(id)',
])
def test_structural_comparison_preserves_repeated_constraints(constraint):
    def observed(copies):
        sql = 'CREATE TABLE child (parent_id INTEGER, ' + ', '.join([constraint] * copies) + ')'
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
            conn.execute(sql)
            return schema_definition._table(conn, 'child', sql)

    assert observed(2) == observed(2)
    assert observed(1) != observed(2)
