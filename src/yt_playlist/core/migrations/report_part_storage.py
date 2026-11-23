"""Move each bounded recommendation report manifest onto its publication."""
from ..recommendation_outputs import REPORT_OWNER, REPORT_PARTS
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _preserve_sequence, _sequence


NAME = 'own_recommendation_report_parts'
TABLE = 'recommendation_report_publications'
RETIRED = REPORT_OWNER


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
                                        'AND tbl_name IN (?,?) ORDER BY type,name', (TABLE, RETIRED))]


def install(conn, _context):
    highwater = _sequence(conn, RETIRED)
    if highwater is not None:
        _preserve_sequence(conn, TABLE, highwater // 64)
    conn.execute('DROP TRIGGER recommendation_report_transition')
    conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN parts TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_valid(parts) AND json_type(parts)='array' AND json_array_length(parts)<=64)")
    # Incomplete rows can have gaps; keeping slots preserves exact chunk identities.
    conn.execute(f'''WITH RECURSIVE slots(position) AS (
            SELECT 0 UNION ALL SELECT position+1 FROM slots WHERE position<63)
        UPDATE {TABLE} SET parts=(SELECT json_group_array(json(part)) FROM (
            SELECT CASE WHEN f.id IS NOT NULL THEN json_array(f.id,f.filename,f.content_digest,f.byte_size) END part
            FROM slots LEFT JOIN {RETIRED} f ON f.publication_id={TABLE}.id AND f.chunk_key=CAST(slots.position AS TEXT)
            WHERE slots.position<=(SELECT max(CAST(chunk_key AS INTEGER)) FROM {RETIRED} WHERE publication_id={TABLE}.id)
            ORDER BY slots.position))''')
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f'''CREATE VIEW {REPORT_PARTS} AS
        SELECT part.value->>0 id,p.id publication_id,p.surface,CAST(part.key AS TEXT) chunk_key,
               part.value->>1 filename,part.value->>2 content_digest,part.value->>3 byte_size
        FROM {TABLE} p,json_each(p.parts) part WHERE part.type='array' ''')
    conn.execute(f'''CREATE TRIGGER recommendation_report_part_update INSTEAD OF UPDATE OF filename ON {REPORT_PARTS}
        BEGIN UPDATE {TABLE} SET parts=json_set(parts,'$['||OLD.chunk_key||'][1]',NEW.filename)
        WHERE id=OLD.publication_id; END''')
    conn.execute(f'''CREATE TRIGGER valid_recommendation_report_parts BEFORE UPDATE OF parts ON {TABLE}
        WHEN EXISTS(SELECT 1 FROM json_each(NEW.parts) p WHERE CASE WHEN p.type<>'array' THEN 1 ELSE
            json_array_length(p.value)<>4
            OR json_type(p.value,'$[0]') IS NOT 'integer' OR p.value->>0<=0
            OR json_type(p.value,'$[1]') IS NOT 'text' OR length(p.value->>1)=0
            OR json_type(p.value,'$[2]') IS NOT 'text' OR length(p.value->>2)<>64
            OR p.value->>2 GLOB '*[^0-9a-f]*' OR json_type(p.value,'$[3]') IS NOT 'integer'
            OR p.value->>3 NOT BETWEEN 1 AND 1048576 END)
        OR (OLD.status<>'building' AND (json_array_length(NEW.parts)<>json_array_length(OLD.parts)
            OR EXISTS(SELECT 1 FROM json_each(OLD.parts) p JOIN json_each(NEW.parts) n USING(key)
                      WHERE json_remove(p.value,'$[1]') IS NOT json_remove(n.value,'$[1]'))))
        BEGIN SELECT RAISE(ABORT,'Report parts are invalid or retained'); END''')
    conn.execute(f'''CREATE TRIGGER recommendation_report_parts_insert BEFORE INSERT ON {TABLE}
        WHEN NEW.parts<>'[]' BEGIN SELECT RAISE(ABORT,'Report parts require a publication'); END''')
    conn.execute(f'''CREATE TRIGGER recommendation_report_transition BEFORE UPDATE OF status ON {TABLE}
        WHEN NOT ((OLD.status='building' AND NEW.status='published' AND NEW.part_count=json_array_length(NEW.parts)
                   AND NOT EXISTS(SELECT 1 FROM json_each(NEW.parts) part WHERE part.type<>'array'))
                 OR (OLD.status='published' AND NEW.status='retiring' AND NEW.selected=0))
        BEGIN SELECT RAISE(ABORT,'Report is incomplete or retained'); END''')


def validate(conn, *, display_settings=False):
    if set(definitions(conn, (RETIRED, REPORT_PARTS))) != (set() if display_settings else {REPORT_PARTS}):
        raise MigrationError('Retired recommendation report-part storage remains')
