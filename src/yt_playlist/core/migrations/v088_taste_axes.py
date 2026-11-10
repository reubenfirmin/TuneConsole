"""Normalize queried genre/family axes and retain exact historical tokens."""
import re
from pathlib import Path
import sqlite3

from yt_playlist.core import taste_axes
from .runner import MigrationError

OLD_TABLES = ('rec_weights','rec_lean','home_hidden_facet','rec_theme','rec_play_grad','rec_grad_log')
RENAMES = {'rec_play_grad':'play_gradient_checkpoints','rec_grad_log':'recommendation_gradient_events'}


def transform(sql):
    table = re.match(r'CREATE TABLE\s+"?(\w+)"?\s*\(', sql.lstrip(), re.I)
    if table and table[1] in OLD_TABLES:
        sql = re.sub(r'(?<!\w)"?(axis|facet)"?\s+TEXT\b', 'axis_id INTEGER REFERENCES taste_axes(id)', sql, count=1, flags=re.I)
        for old, new in RENAMES.items():
            sql = re.sub(r'\b' + old + r'\b', new, sql)
    return sql


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_taste_axes.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete taste axis schema')


def retire_preparation_capture(conn):
    """Retain staged evidence while retiring dependency programs on old keys."""
    from ..preparation.taste_axes import JOB
    from .current import _tokens
    from .v103_entity_names import transform as rename
    renamed=conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts'").fetchone()
    for name,sql in JOB.definitions(conn):
        if not name.startswith('prepare_taste_axes_dependency_'):
            continue
        row=conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is not None:
            if _tokens(row[0])!=_tokens(rename(sql) if renamed else sql):
                raise MigrationError('Incompatible retired taste capture: '+name)
            conn.execute('DROP TRIGGER '+name)


def apply(conn, context):
    # Catalog dependency capture refreshes high-water keys from all six old
    # owners. Their axis/facet keys disappear during the explicit replacement.
    retire_preparation_capture(conn)
    for sql in statements():
        conn.execute(sql)
    for old in OLD_TABLES:
        definition = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (old,)).fetchone()[0]
        rows = [dict(row) for row in conn.execute('SELECT * FROM ' + old)]
        legacy_column = 'facet' if old == 'rec_theme' else 'axis'
        seen = {}
        normalized = []
        for row in rows:
            token = row[legacy_column]
            axis_id = taste_axes.identity(conn, token, create=True)
            if old not in ('rec_grad_log','rec_theme','rec_play_grad','home_hidden_facet') and axis_id in seen:
                raise MigrationError(f'{old}: multiple legacy axes resolve to one identity; reconcile explicitly')
            conn.execute('INSERT INTO taste_axis_legacy(table_name,row_key,raw_axis,axis_id,weight,lean_value,score,'
                         'updated_at,last_graduated_day,source,factor,new_weight,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (old,str(row.get('id',token)),token,axis_id,*[row.get(c) for c in (
                             'weight','value','score','updated_at','last_graduated_day','source','factor','new_weight','created_at')]))
            row['axis_id'] = axis_id
            del row[legacy_column]
            if axis_id in seen and old == 'rec_play_grad':
                prior = seen[axis_id]
                days = [day for day in (prior['last_graduated_day'],row['last_graduated_day']) if day is not None]
                prior['last_graduated_day'] = max(days) if days else None
            elif axis_id in seen and old == 'home_hidden_facet':
                continue
            elif old == 'rec_theme' and axis_id in seen:
                # This ledger contains additive contributions, not competing
                # configured values. Preserve each source value in the archive.
                prior = seen[axis_id]
                prior['score'] += row['score']
                prior['updated_at'] = max(prior['updated_at'],row['updated_at'])
            else:
                normalized.append(row)
                seen[axis_id] = row
        conn.execute('DROP TABLE ' + old)
        conn.execute(transform(definition))
        target = RENAMES.get(old, old)
        if normalized:
            columns = tuple(normalized[0])
            conn.executemany(f'INSERT INTO {target}({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                             [tuple(row[c] for c in columns) for row in normalized])
