"""Fold Mix Tape display facts and album-menu state into their owners."""
import json

from .. import mix_choices
from ..evidence_contract import canonical
from ..schema_contract import MigrationError


NAME = 'fold_mix_menu_storage'
MARKER = 'mix_choice_descriptors_genres'
RETIRED = ('mix_choice_themes', 'mix_choice_raw_genres', 'mix_choice_genre_sets', 'mix_choice_genre_members',
           'mix_choice_preparation', 'album_menu_control_genres', 'album_menu_refresh_exclusions',
           'album_menu_refresh_state')
OWNERS = ('album_menu_controls', 'mix_choice_descriptors', *RETIRED)
ALBUM_CONTROLS = '''CREATE TABLE _album_menu_controls (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    expires_at REAL CHECK(expires_at IS NULL OR (typeof(expires_at) IN ('integer','real') AND abs(expires_at)<=1.7976931348623157e308)),
    novelty REAL CHECK(novelty IS NULL OR (typeof(novelty) IN ('integer','real') AND novelty BETWEEN 0 AND 1)),
    genres_json TEXT CHECK(genres_json IS NULL OR (json_valid(genres_json) AND json_type(genres_json)='array'
        AND json_array_length(genres_json)<=100)),
    refresh_epoch INTEGER CHECK(refresh_epoch IS NULL OR (typeof(refresh_epoch)='integer' AND refresh_epoch>=0)),
    refresh_exclusions_json TEXT CHECK(refresh_exclusions_json IS NULL OR (json_valid(refresh_exclusions_json)
        AND json_type(refresh_exclusions_json)='array' AND json_array_length(refresh_exclusions_json)<=100)),
    refresh_updated_at REAL CHECK(refresh_updated_at IS NULL OR (typeof(refresh_updated_at) IN ('integer','real')
        AND abs(refresh_updated_at)<=1.7976931348623157e308)),
    CHECK((expires_at IS NULL)=(novelty IS NULL) AND (expires_at IS NULL)=(genres_json IS NULL)),
    CHECK(refresh_exclusions_json IS NOT NULL OR (refresh_epoch IS NULL AND refresh_updated_at IS NULL)),
    CHECK(expires_at IS NOT NULL OR refresh_exclusions_json IS NOT NULL))'''


def predecessor_objects(conn):
    names = ','.join('?' for _ in OWNERS)
    return [sql for sql, in conn.execute(f'SELECT sql FROM sqlite_schema WHERE tbl_name IN ({names}) '
                                        'AND sql IS NOT NULL ORDER BY type,name', OWNERS)]


def _install_descriptors(conn):
    definition = mix_choices.DESCRIPTORS.replace('mix_choice_descriptors', '_mix_descriptors', 1)
    conn.execute(definition)
    rows = conn.execute('''SELECT d.id,t.intent,t.label,t.note,
        (SELECT json_group_array(g.label ORDER BY g.label) FROM mix_choice_genre_members m
         JOIN mix_choice_raw_genres g ON g.id=m.genre_id WHERE m.set_id=d.genre_set_id)
        FROM mix_choice_descriptors d JOIN mix_choice_themes t ON t.id=d.theme_id''')
    conn.executemany('INSERT INTO _mix_descriptors(id,intent,label,note,genres_json) VALUES (?,?,?,?,?)',
                     ((*row[:4], canonical(json.loads(row[4])).decode()) for row in rows))
    conn.execute('DROP TABLE mix_choice_descriptors')
    conn.execute('ALTER TABLE _mix_descriptors RENAME TO mix_choice_descriptors')
    conn.execute(mix_choices.IMMUTABLE_DESCRIPTOR)
    conn.execute(f'CREATE TRIGGER {MARKER} BEFORE INSERT ON mix_choice_descriptors '
                 "WHEN EXISTS(SELECT 1 FROM json_each(NEW.genres_json) WHERE type<>'text' OR value='') "
                 "BEGIN SELECT RAISE(ABORT,'Invalid displayed mix genre'); END")


def _install_controls(conn):
    conn.execute(ALBUM_CONTROLS)
    conn.execute('''INSERT INTO _album_menu_controls(singleton,expires_at,novelty,genres_json,
            refresh_epoch,refresh_exclusions_json,refresh_updated_at)
        SELECT 1,t.expires_at,t.novelty,CASE WHEN t.singleton IS NOT NULL THEN
            (SELECT json_group_array(json_array(genre_key,unresolved_value) ORDER BY position)
             FROM album_menu_control_genres) END,
            r.epoch,CASE WHEN r.id IS NOT NULL THEN
            (SELECT json_group_array(browse_id ORDER BY position) FROM album_menu_refresh_exclusions) END,
            r.updated_at FROM album_menu_controls t FULL OUTER JOIN album_menu_refresh_state r ON r.id=t.singleton''')
    conn.execute('DROP TABLE album_menu_controls')
    conn.execute('ALTER TABLE _album_menu_controls RENAME TO album_menu_controls')


def install(conn, _context):
    _install_descriptors(conn)
    _install_controls(conn)
    for name in RETIRED:
        conn.execute(f'DROP TABLE {name}')


def validate(conn):
    names = ','.join('?' for _ in RETIRED)
    if not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (MARKER,)).fetchone():
        raise MigrationError('Mix menu storage was not folded')
    if conn.execute(f'SELECT 1 FROM sqlite_schema WHERE name IN ({names}) LIMIT 1', RETIRED).fetchone():
        raise MigrationError('Retired mix menu storage remains')
