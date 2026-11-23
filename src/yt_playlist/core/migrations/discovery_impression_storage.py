"""Keep each visible Discovery view with the subject it showed."""
from itertools import groupby
import json

from ..discovery_provenance import OFFER_OWNERS, SUBJECTS
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'keep_discovery_views_on_their_subjects'
RETIRED = 'discovery_impressions'
VIEW = 'discovered_albums'
COLUMN = "discovery_views TEXT NOT NULL DEFAULT '{}' CHECK(json_type(discovery_views)='object')"


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND '
        '(tbl_name IN (?,?) OR (type=\'table\' AND name IN (?,?,?))) ORDER BY type,name',
        (RETIRED, VIEW, *OFFER_OWNERS.values()))]


def install(conn, _context):
    for kind, table in OFFER_OWNERS.items():
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {COLUMN}')
        owner = SUBJECTS[kind]
        rows = conn.execute(f'SELECT {owner},view_id,shown_at FROM {RETIRED} '
                            f'WHERE {owner} IS NOT NULL ORDER BY {owner},view_id')
        for identifier, views in groupby(rows, key=lambda row: row[0]):
            encoded = json.dumps({view: shown for _, view, shown in views}, separators=(',', ':'))
            conn.execute(f'UPDATE {table} SET discovery_views=? WHERE id=?', (encoded, identifier))

    relations = conn.execute("SELECT sql FROM sqlite_schema WHERE tbl_name=? AND sql IS NOT NULL "
                             'ORDER BY type DESC', (VIEW,)).fetchall()
    conn.execute(f'DROP VIEW {VIEW}')
    for sql, in relations:
        for old, new in (('max(shown_at)', 'max(atom)'), ('min(shown_at)', 'min(atom)'), ('count(*)', 'count(*)')):
            sql = sql.replace(f'{old} FROM {RETIRED} WHERE album_id=a.id', f'{new} FROM json_each(a.discovery_views)')
        conn.execute(sql)
    conn.execute(f'DROP TABLE {RETIRED}')


def validate(conn):
    owners = definitions(conn, (RETIRED, VIEW, *OFFER_OWNERS.values()))
    if (RETIRED in owners or 'json_each(a.discovery_views)' not in owners[VIEW]
            or any(COLUMN not in owners[table] for table in OFFER_OWNERS.values())):
        raise MigrationError('Discovery impressions were not folded into their subjects')
