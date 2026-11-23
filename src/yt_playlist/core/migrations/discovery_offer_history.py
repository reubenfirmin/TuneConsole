"""Fold retained Discovery offer history into its recording, artist and album owners."""
from .. import domain_contracts
from ..discovery_provenance import EARLIER_OFFERS, OFFER_OWNERS, SUBJECTS
from ..schema_contract import MigrationError
from .track_source_cutover import _replace_table


OFFER_CACHES = {
    'legacy_offered_count': ('earlier_offer_count', 'INTEGER NOT NULL DEFAULT 0', domain_contracts.integer),
    'legacy_first_shown': ('earlier_first_shown', 'REAL', domain_contracts.finite),
    'legacy_last_shown': ('earlier_last_shown', 'REAL', domain_contracts.finite),
}


def fold_earlier_offers(conn, _context):
    relations = list(conn.execute('SELECT type,name,sql FROM sqlite_schema WHERE tbl_name=? ORDER BY type DESC',
                                  ('discovered_albums',)))
    conn.execute('DROP VIEW discovered_albums')
    fields = ','.join(column for column, *_ in EARLIER_OFFERS)
    source = ','.join(source for _, source, *_ in EARLIER_OFFERS)
    for kind, table in OFFER_OWNERS.items():
        for column, _, declaration in EARLIER_OFFERS:
            check = domain_contracts.EARLIER_DISCOVERY_OFFERS[column]
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration} CHECK({check})')
        conn.execute(f'UPDATE {table} SET ({fields})=(SELECT {source} FROM discovery_legacy_offers '
                     f'WHERE {SUBJECTS[kind]}={table}.id) WHERE id IN '
                     f'(SELECT {SUBJECTS[kind]} FROM discovery_legacy_offers WHERE {SUBJECTS[kind]} IS NOT NULL)')
    for table in ('album_discovery_memberships', 'discovered_artists'):
        sql = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (table,)).fetchone()[0]
        sql = sql.replace(' CHECK(legacy_offered_count>=0)', '')
        for old, (_, declaration, check) in OFFER_CACHES.items():
            sql = sql.replace(f'{old} {declaration},', '')
            sql = sql.replace(f',CONSTRAINT ck_{table}_{old} CHECK({check(old)})', '')
        sql = '\n'.join(line for line in sql.splitlines() if line.strip())
        dependencies = [row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
            (table,))]
        _replace_table(conn, table, sql)
        for statement in dependencies:
            conn.execute(statement)
    for kind, name, sql in relations:
        for old, (column, _, _) in OFFER_CACHES.items():
            if kind == 'view':
                sql = sql.replace(f'm.{old}', f'a.{column} AS {old}')
            elif name == 'discovered_albums_update':
                sql = sql.replace(f',{old}=NEW.{old}', '')
        conn.execute(sql)
    conn.execute('DROP TABLE discovery_legacy_offers')


def validate_folded_offers(conn):
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_legacy_offers'").fetchone():
        raise MigrationError('Obsolete Rediscover offer storage is still installed')
