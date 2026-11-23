"""Store offered options, format usage and attribution on their existing owners."""
import json

from ..policy_impressions import RAW_VERSION, ROLES, live_reference
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged


NAME = 'fold_choice_evidence'
RETIRED = ('choice_format_usage', 'choice_interest_mix_descriptors', 'mix_choice_options',
           'recommendation_impression_policies')
REPORTS = 'recommendation_report_publications'
LEARNERS = 'choice_learning_publications'
MENUS = 'mix_choice_menus'
IMPRESSIONS = 'rec_served_impressions'
LINKS = RETIRED[-1]
CAPTURE = 'recommendation_impression_policy_capture'
MARKER = 'ix_rec_served_primary_policy'
PENDING_INDEX = 'ix_rec_served_unprepared_policy'
PAGE_SIZE = 2048
ROLE_FIELDS = ('policy_version', 'historical_version', 'policy_reason')
LINK_FIELDS = ('policy_version', 'historical_version', 'resolution_reason')
PENDING = ' AND '.join(f'primary_{field} IS NULL' for field in ROLE_FIELDS)


def _fields(role):
    return tuple(f'{role}_{field}' for field in ROLE_FIELDS)


IMPRESSION_FIELDS = ','.join(field for role in ROLES for field in _fields(role))
LINK_VALUES = ','.join(f'{role}_link.{field}' for role in ROLES for field in LINK_FIELDS)
POOL_JOIN = (f'LEFT JOIN {LINKS} AS pool_link ON pool_link.impression_id=primary_link.impression_id '
             "AND pool_link.kind='pool'")


PENDING_INDEX_SQL = f'CREATE INDEX {PENDING_INDEX} ON {IMPRESSIONS}(id) WHERE {PENDING}'


def _prepare_storage(conn):
    for role in ROLES:
        live, archived, reason = _fields(role)
        conn.execute(f'ALTER TABLE {IMPRESSIONS} ADD COLUMN {live} TEXT REFERENCES recommendation_policies(version)')
        conn.execute(f'ALTER TABLE {IMPRESSIONS} ADD COLUMN {archived} TEXT REFERENCES recommendation_policy_history(version)')
        conn.execute(f'ALTER TABLE {IMPRESSIONS} ADD COLUMN {reason} TEXT CHECK('
                     f"{reason} IN ('missing_policy','invalid_type') AND ({live} IS NULL AND {archived} IS NULL) "
                     f'OR {reason} IS NULL AND NOT ({live} IS NOT NULL AND {archived} IS NOT NULL))')
        for suffix, field in (('policy', live), ('historical_policy', archived)):
            conn.execute(f'CREATE INDEX ix_rec_served_{role}_{suffix} ON {IMPRESSIONS}({field}) WHERE {field} IS NOT NULL')
    conn.execute(PENDING_INDEX_SQL)


def _pending():
    return f'SELECT id FROM {IMPRESSIONS} INDEXED BY {PENDING_INDEX} WHERE {PENDING}'


def _prepare_page(conn, version):
    if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
        return False
    if not definitions(conn, (PENDING_INDEX,)):
        _prepare_storage(conn)
        return True
    conn.execute(f'''WITH batch AS MATERIALIZED ({_pending()} ORDER BY id LIMIT ?)
        UPDATE {IMPRESSIONS} AS impression SET ({IMPRESSION_FIELDS})=({LINK_VALUES})
        FROM batch JOIN {LINKS} AS primary_link ON primary_link.impression_id=batch.id AND primary_link.kind='primary'
        {POOL_JOIN} WHERE impression.id=batch.id''', (PAGE_SIZE,))
    if conn.execute('SELECT changes()').fetchone()[0]:
        return True
    if conn.execute(_pending() + ' LIMIT 1').fetchone():
        raise MigrationError('Retained impression is missing its primary policy evidence')
    return False


def prepare(conn, version):
    while True:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not _prepare_page(conn, version):
                return


def predecessor_objects(conn):
    owners = (*RETIRED, REPORTS, LEARNERS, MENUS, IMPRESSIONS)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM sqlite_schema WHERE tbl_name IN ({",".join("?" for _ in owners)}) '
        'AND sql IS NOT NULL ORDER BY type,name', owners)]


def _usage(conn):
    conn.execute(f"ALTER TABLE {REPORTS} ADD COLUMN format_usage TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(format_usage) IS 'array' AND (surface='listening_choices' OR format_usage='[]'))")
    grouped = {}
    for identity, *facts in conn.execute('SELECT publication_id,format,field,value FROM choice_format_usage '
                                         'ORDER BY publication_id,format,field'):
        grouped.setdefault(identity, []).append(facts)
    conn.executemany(f'UPDATE {REPORTS} SET format_usage=? WHERE id=?',
                     ((json.dumps(rows), identity) for identity, rows in grouped.items()))
    conn.execute(f'''CREATE TRIGGER frozen_choice_format_usage BEFORE UPDATE OF format_usage ON {REPORTS}
        WHEN OLD.status<>'building' BEGIN SELECT RAISE(ABORT,'Published format usage is immutable'); END''')


def _menus(conn):
    immutable = definitions(conn, ('mix_choice_menus_immutable',))['mix_choice_menus_immutable']
    conn.execute('DROP TRIGGER mix_choice_menus_immutable')
    conn.execute(f"ALTER TABLE {MENUS} ADD COLUMN options TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(options) IS 'array')")
    conn.execute(f'''UPDATE {MENUS} AS m SET options=(
        SELECT json_group_array(json_array(o.option_id,o.position,o.descriptor_id,o.genres_complete,o.origin)
            ORDER BY o.position) FROM mix_choice_options o WHERE o.opportunity_id=m.opportunity_id)''')
    conn.execute(immutable)
    missing = conn.execute('''SELECT 1 FROM choice_interest_mix_descriptors d
        LEFT JOIN choice_learning_publications p ON p.id=d.publication_id
        LEFT JOIN json_each(p.document,'$.contexts') c ON c.key=d.context_position
        WHERE c.value->>'$.descriptor_id' IS NOT d.descriptor_id LIMIT 1''').fetchone()
    if missing:
        raise MigrationError('Stored choice learning descriptors differ from their captured contexts')
    checks = {
        MENUS: "SELECT 1 FROM json_each(NEW.options) o WHERE NOT EXISTS "
               "(SELECT 1 FROM mix_choice_descriptors d WHERE d.id=o.value->>2)",
        LEARNERS: "SELECT 1 FROM json_each(NEW.document,'$.contexts') c "
                  "WHERE c.value->>'$.descriptor_id' IS NOT NULL AND NOT EXISTS "
                  "(SELECT 1 FROM mix_choice_descriptors d WHERE d.id=c.value->>'$.descriptor_id')",
    }
    for owner, missing in checks.items():
        conn.execute(f'''CREATE TRIGGER {owner}_descriptor_insert BEFORE INSERT ON {owner}
            WHEN EXISTS({missing}) BEGIN SELECT RAISE(ABORT,'Unknown captured mix descriptor'); END''')
    conn.execute(f'''CREATE TRIGGER mix_descriptor_retained BEFORE DELETE ON mix_choice_descriptors
        WHEN EXISTS(SELECT 1 FROM {MENUS} m,json_each(m.options) o WHERE o.value->>2=OLD.id)
          OR EXISTS(SELECT 1 FROM {LEARNERS} p,json_each(p.document,'$.contexts') c
                    WHERE c.value->>'$.descriptor_id'=OLD.id)
        BEGIN SELECT RAISE(ABORT,'A menu or learner retains this descriptor'); END''')


def _capture():
    pool = "CAST(json_extract(NEW.provenance,'$.pool_policy') AS TEXT)"
    valid = "json_type(NEW.provenance,'$.pool_policy') IN ('text','integer')"
    return f'''CREATE TRIGGER {CAPTURE} AFTER INSERT ON {IMPRESSIONS} BEGIN
        UPDATE {IMPRESSIONS} SET
            (primary_policy_version,primary_historical_version,primary_policy_reason)=(
                SELECT p.version,h.version,CASE WHEN p.version IS NULL AND h.version IS NULL THEN 'missing_policy' END
                FROM (SELECT NEW.model_version AS version) a
                LEFT JOIN recommendation_policies p ON p.version=a.version
                LEFT JOIN recommendation_policy_history h ON p.version IS NULL AND h.version=a.version),
            (pool_policy_version,pool_historical_version,pool_policy_reason)=(
                SELECT p.version,h.version,CASE WHEN a.version IS NULL THEN NULL WHEN NOT a.valid THEN 'invalid_type'
                    WHEN p.version IS NULL AND h.version IS NULL THEN 'missing_policy' END
                FROM (SELECT {pool} AS version,{valid} AS valid) a
                LEFT JOIN recommendation_policies p ON a.valid AND p.version=a.version
                LEFT JOIN recommendation_policy_history h ON a.valid AND p.version IS NULL AND h.version=a.version)
        WHERE id=NEW.id;
    END'''


def _impressions(conn):
    conn.execute(f'DROP TRIGGER {CAPTURE}')
    conn.execute(f'DROP INDEX {PENDING_INDEX}')
    for role, raw in RAW_VERSION.items():
        live, archived, reason = _fields(role)
        conn.execute(_identity_guard(role, live, archived, reason, raw))
    for action, changed in (('update', 'OLD.primary_policy_version,OLD.pool_policy_version,'
                                         'NEW.primary_policy_version,NEW.pool_policy_version'),
                            ('delete', 'OLD.primary_policy_version,OLD.pool_policy_version')):
        event = 'UPDATE OF primary_policy_version,pool_policy_version' if action == 'update' else 'DELETE'
        conn.execute(f'''CREATE TRIGGER rec_served_policy_account_{action} AFTER {event} ON {IMPRESSIONS} BEGIN
            UPDATE recommendation_policies SET served={live_reference('recommendation_policies.version')}
            WHERE version IN ({changed});
            UPDATE recommendation_policy_bodies SET served=EXISTS(SELECT 1 FROM recommendation_policies p
                WHERE p.base_version=recommendation_policy_bodies.version AND p.served=1)
            WHERE version IN (SELECT base_version FROM recommendation_policies WHERE version IN ({changed}));
        END''')
    conn.execute(_capture())


def _identity_guard(role, live, archived, reason, raw):
    expression = f'NEW.{raw}' if role == 'primary' else raw.replace('provenance', 'NEW.provenance')
    invalid_source = ("COALESCE(json_type(NEW.provenance,'$.pool_policy') NOT IN ('text','integer','null'),0)"
                      if role == 'pool' else '0')
    return f'''CREATE TRIGGER rec_served_{role}_policy_identity BEFORE UPDATE OF {live},{archived},{reason}
        ON {IMPRESSIONS} WHEN
            (NEW.{live} IS NOT NULL AND NEW.{live} IS NOT ({expression}))
            OR (NEW.{archived} IS NOT NULL AND NEW.{archived} IS NOT ({expression}))
            OR ((NEW.{reason} IS 'invalid_type') IS NOT ({invalid_source}))
            OR ((({expression}) IS NULL) IS NOT
                (NEW.{live} IS NULL AND NEW.{archived} IS NULL AND NEW.{reason} IS NULL))
            OR (OLD.{archived} IS NOT NULL AND NEW.{archived} IS NOT OLD.{archived})
            OR (OLD.{live} IS NOT NULL AND NEW.{live} IS NOT OLD.{live} AND NEW.{archived} IS NOT OLD.{live})
            OR (OLD.{reason}='invalid_type' AND NEW.{reason} IS NOT OLD.{reason})
            OR (OLD.{reason}='missing_policy' AND NEW.{reason} IS NULL AND NEW.{live} IS NULL AND NEW.{archived} IS NULL)
        BEGIN SELECT RAISE(ABORT,'Impression policy ownership is immutable'); END'''


def install(conn, _context):
    if not definitions(conn, (PENDING_INDEX,)) or conn.execute(_pending() + ' LIMIT 1').fetchone():
        raise PreparationChanged()
    _usage(conn)
    _menus(conn)
    _impressions(conn)
    # Older link-delete guards reject discarding attribution from a retained
    # impression. All roles now live on the impression itself.
    conn.execute('DROP TRIGGER recommendation_impression_link_retained')
    conn.execute('DROP TRIGGER recommendation_impression_account_delete')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')


def validate(conn):
    if set(definitions(conn, (*RETIRED, PENDING_INDEX, MARKER))) != {MARKER}:
        raise MigrationError('Choice evidence was not folded into its owners')
