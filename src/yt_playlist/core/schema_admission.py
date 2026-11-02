"""Structural admission for explicit preparation, without an upgrade runner."""
from functools import lru_cache
import json
from pathlib import Path

from . import genre_contract, schema_definition as schema
from .schema_contract import MigrationError


@lru_cache(maxsize=1)
def reference():
    return json.loads((Path(__file__).with_name('migrations') / 'installed_v079.json').read_text())


def history(conn):
    table = conn.execute("SELECT type FROM sqlite_master WHERE name='schema_migrations'").fetchone()
    if table is None:
        return 0
    columns = list(conn.execute('PRAGMA table_info(schema_migrations)'))
    if (table[0]!='table' or [(r[1],r[2].upper(),r[3],r[5]) for r in columns] !=
            [('version','INTEGER',0,1),('name','TEXT',1,0),('applied_at','REAL',1,0)]):
        raise MigrationError('Incompatible schema_migrations layout')
    rows = list(conn.execute('SELECT version,name,applied_at FROM schema_migrations ORDER BY version'))
    names = reference()['history']
    if len(rows)>len(names):
        raise MigrationError('Database schema is newer than this application')
    for version,(row,name) in enumerate(zip(rows,names),1):
        if row[0]!=version or row[1]!=name or not isinstance(row[2],(int,float)):
            raise MigrationError('Migration history is not the supported contiguous prefix')
    return len(rows)


def legacy_genres(statements):
    legacy_key = ('(SELECT CASE WHEN count(DISTINCT key)=1 THEN min(key) END FROM ('
        'SELECT key FROM genre_catalog WHERE key=NEW.genre OR name=NEW.genre COLLATE NOCASE '
        'UNION SELECT genre_key FROM genre_alias WHERE label=NEW.genre COLLATE NOCASE))')
    for sql in statements:
        name = genre_contract.definition_name(sql)
        if name in genre_contract.ADDITIONS:
            continue
        if name in ('catalog_tracks_update','tracks_update'):
            sql = sql.replace(genre_contract.sql_key('NEW.genre'),legacy_key)
        elif name=='entry_field_choices':
            sql = sql.replace('key=COALESCE((SELECT genre_key FROM genre_redirects '
                              'WHERE retired_key=o.genre_key),o.genre_key)','key=o.genre_key')
        elif name=='taste_axes':
            sql = sql.replace('family_key IS NULL AND value IS NOT NULL AND length(value)>0',
                              'family_key IS NULL AND length(value)>0')
        elif name=='taste_axis_legacy':
            for column in genre_contract.ARCHIVE_COLUMNS:
                sql = sql.replace(' '+column+',\n','')
        yield sql


@lru_cache(maxsize=32)
def contract(version):
    statements = reference()['statements'] if version==79 else tuple(schema.statements(version=version))
    return schema._schema_contract(statements)


@lru_cache(maxsize=6)
def earlier_genres(version):
    return schema._schema_contract(legacy_genres(schema.statements(version=version)))


def validate_version(conn, version, *, foreign_keys=True, data=True):
    if not 79<=version<=schema.VERSION:
        raise MigrationError('Preparation requires a supported installed schema')
    expected = contract(version if version==79 or version>=86 else 85)
    replaced = frozenset()
    if 80<=version<86:
        added = {'genre_preferences','ix_genre_preference_enabled'} if version<85 else set()
        if version<84:
            added.update({'album_note_requests','album_note_request_budget','album_note_request_identity',
                          'album_note_request_revision','retired_album_note_request_insert','retired_album_note_request_update',
                          'album_note_outputs','album_note_output_budget','album_note_output_immutable',
                          'retired_album_note_output_insert','retired_album_note_output_update'})
        if version<83:
            added.update({'home_offer_dedup','home_offer_signatures','home_offer_signature_budget',
                          'retired_home_offer_insert','retired_home_offer_update'})
        if version<82:
            added.update({'model_selection_schedule','tv_preparation_state',
                          'retired_recommendation_work_insert','retired_recommendation_work_update'})
        if version==80:
            added.add('playback_feedback_receipts')
        replaced = frozenset(added)
    if 86<=version<92:
        earlier = earlier_genres(version)
        expected = expected.copy()
        objects = {name:(kind,sql) for name,kind,sql in conn.execute(
            "SELECT name,type,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}
        for name in (*genre_contract.REPLACEMENTS,'taste_axes','taste_axis_legacy'):
            if name in earlier and name in objects:
                kind,sql = objects[name]
                actual = (kind,schema._table(conn,name,sql) if kind=='table' else schema._tokens(sql))
                if actual==earlier[name]:
                    expected[name] = earlier[name]
        for name in genre_contract.ADDITIONS:
            if name not in objects:
                expected.pop(name,None)
    schema._validate(conn,expected,version,replaced=replaced,foreign_keys=foreign_keys,data=data)
