"""Keep selected publication manifests and clicked parameters on their owners."""
import json

from .. import publication_files
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'fold_publication_manifests'
RETIRED = ('clicked_library_pools', 'home_display_files', 'home_display_group_roots', 'home_display_policies',
           'home_display_recommendation_roots', 'listening_output_files')
OWNERS = ('home_display_publications', 'listening_output_publications', 'recommendation_generations')
CLICKED = {
    'clicked_lane': "TEXT CHECK(clicked_lane IN ('wheelhouse','explore','comfort','temporal'))",
    'clicked_epoch': "INTEGER CHECK(clicked_epoch IS NULL OR (typeof(clicked_epoch)='integer' AND clicked_epoch>=0))",
    'clicked_built_at': 'REAL CHECK(abs(clicked_built_at)<=1.7976931348623157e308)',
    'clicked_include_genres': "TEXT CHECK(clicked_include_genres IS NULL OR json_type(clicked_include_genres) IS 'array')",
    'clicked_avoid_genres': "TEXT CHECK(clicked_avoid_genres IS NULL OR json_type(clicked_avoid_genres) IS 'array')",
}
HOME_TRANSITION = '''CREATE TRIGGER home_display_transition BEFORE UPDATE OF status ON home_display_publications
WHEN NOT ((OLD.status='building' AND NEW.status='published' AND NEW.part_count=json_array_length(NEW.files)
    AND NOT EXISTS(SELECT 1 FROM json_each(NEW.files) f WHERE f.value->>2 IS NOT CAST(f.key AS TEXT)))
    OR (OLD.status='published' AND NEW.status='retiring' AND OLD.is_current=0))
BEGIN SELECT RAISE(ABORT,'Home display is incomplete or retained'); END'''
LISTENING_TRANSITION = '''CREATE TRIGGER listening_output_transition BEFORE UPDATE OF status ON listening_output_publications
WHEN NOT ((OLD.status='building' AND NEW.status='published' AND NEW.output_count=json_array_length(NEW.files)
    AND NEW.chunk_count=(SELECT count(*) FROM json_each(NEW.files) f WHERE f.value->>7 IS NOT NULL)
    AND NOT EXISTS(SELECT 1 FROM json_each(NEW.files) f
        WHERE f.value->>7 IS NOT NULL AND f.value->>7 IS NOT f.key))
    OR (OLD.status='published' AND NEW.status='retiring'))
BEGIN SELECT RAISE(ABORT,'Listening publication is incomplete or retained'); END'''


def predecessor_objects(conn):
    owners = (*RETIRED, *OWNERS)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM main.sqlite_schema WHERE tbl_name IN ({",".join("?" for _ in owners)}) '
        'AND sql IS NOT NULL ORDER BY type,name', owners)]


def _json_column(conn, table, column):
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]' "
                 f"CHECK(json_type({column}) IS 'array')")


def _files(conn, owner, table):
    _json_column(conn, table, 'files')
    fields = 'id,surface,chunk_key,filename,content_digest,byte_size'
    if owner == 'home_display_files':
        conn.execute(f'UPDATE {table} AS p SET files=(SELECT json_group_array(json_array({fields}) '
                     'ORDER BY CAST(chunk_key AS INTEGER)) '
                     f'FROM {owner} WHERE publication_id=p.id)')
    else:
        # Keep the full precision of SQLite REAL observation times.
        by_publication = {}
        for identity, *descriptor in conn.execute(f'SELECT publication_id,{fields},built_at,position,'
                'from_day,until_day,chunk_bytes FROM listening_output_files '
                'ORDER BY publication_id,position IS NULL,position,surface,chunk_key'):
            by_publication.setdefault(identity, []).append(descriptor)
        conn.executemany('UPDATE listening_output_publications SET files=? WHERE id=?',
                         ((json.dumps(rows), identity) for identity, rows in by_publication.items()))
    high_water = conn.execute(f'SELECT max(coalesce((SELECT seq FROM sqlite_sequence WHERE name=?),0),'
                              f'coalesce((SELECT max(id) FROM {owner}),0))', (owner,)).fetchone()[0]
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?)', (publication_files.key(owner), str(high_water)))


def _clicked(conn):
    for field, declaration in CLICKED.items():
        conn.execute(f'ALTER TABLE recommendation_generations ADD COLUMN {field} {declaration}')
    conn.execute(f'UPDATE recommendation_generations AS g SET ({",".join(CLICKED)})='
                 '(p.lane,p.epoch,p.built_at,p.include_genres,p.avoid_genres) '
                 'FROM clicked_library_pools p WHERE g.id=p.generation_id')
    conn.execute('''CREATE TRIGGER clicked_pool_install BEFORE UPDATE OF clicked_lane,clicked_epoch,clicked_built_at,
        clicked_include_genres,clicked_avoid_genres ON recommendation_generations
        WHEN OLD.clicked_lane IS NOT NULL OR NEW.clicked_lane IS NULL OR NEW.clicked_epoch IS NULL
          OR NOT EXISTS(SELECT 1 FROM recommendation_generation_surfaces s
              WHERE s.generation_id=NEW.id AND s.surface='home_pool:'||NEW.clicked_lane)
          OR EXISTS(SELECT 1 FROM recommendation_generation_surfaces s WHERE s.generation_id=NEW.id AND s.selected=1)
        BEGIN SELECT RAISE(ABORT,'Clicked pool parameters require an unpublished matching pool'); END''')


def _home_roots(conn):
    for name, relation, fields, order in (
        ('group_roots', 'home_display_group_roots', 'r.field,r.kind,r.external_key', 'r.field'),
        ('recommendation_roots', 'home_display_recommendation_roots',
         'r.field,r.kind,r.external_key,r.generation_id', 'r.field,r.generation_id'),
        ('policy_roots', 'home_display_policies', 'r.raw_key,r.policy_version,r.resolution_reason', 'r.raw_key'),
    ):
        _json_column(conn, 'home_display_publications', name)
        conn.execute(f'UPDATE home_display_publications AS p SET {name}='
                     f'(SELECT json_group_array(json_array({fields}) ORDER BY {order}) '
                     f'FROM {relation} r WHERE r.publication_id=p.id)')


def _guards(conn):
    for table, fields in (
        ('home_display_publications', 'files,group_roots,recommendation_roots,policy_roots'),
        ('listening_output_publications', 'files'),
    ):
        conn.execute(f'''CREATE TRIGGER sealed_{table}_manifest BEFORE UPDATE OF {fields} ON {table}
            WHEN OLD.status<>'building'
            BEGIN SELECT RAISE(ABORT,'Published manifest is immutable'); END''')
    for parent, column, predicate in (
        ('group_evidence_owners', 'group_roots', 'r.value->>1=OLD.kind AND r.value->>2=OLD.external_key'),
        ('recommendation_generation_refs', 'recommendation_roots',
         'r.value->>1=OLD.kind AND r.value->>2=OLD.external_key AND r.value->>3=OLD.generation_id'),
        ('recommendation_policies', 'policy_roots', 'r.value->>1=OLD.version'),
    ):
        conn.execute(f'''CREATE TRIGGER home_retains_{parent} BEFORE DELETE ON {parent}
            WHEN EXISTS(SELECT 1 FROM home_display_publications p,json_each(p.{column}) r WHERE {predicate})
            BEGIN SELECT RAISE(ABORT,'Home display retains this reference'); END''')


def install(conn, _context):
    _clicked(conn)
    for owner, table in publication_files.OWNERS.items():
        _files(conn, owner, table)
    _home_roots(conn)
    for name in ('home_display_transition', 'listening_output_transition'):
        conn.execute(f'DROP TRIGGER {name}')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    conn.execute(HOME_TRANSITION)
    conn.execute(LISTENING_TRANSITION)
    _guards(conn)


def validate(conn, *, display_settings=False):
    names = (*RETIRED, 'sealed_home_display_publications_manifest', 'sealed_listening_output_publications_manifest')
    if set(definitions(conn, names)) != (set() if display_settings else set(names[-2:])):
        raise MigrationError('Home and listening publication manifests were not folded')
