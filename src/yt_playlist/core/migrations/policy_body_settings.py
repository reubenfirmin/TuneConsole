"""Keep shared policy file references as named settings."""
from .. import policy_bodies
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'store_shared_policy_file_references_in_settings'
RETIRED = 'recommendation_policy_bodies'
VARIANTS = 'recommendation_policies'
ACCOUNTING = ('rec_served_policy_account_delete', 'rec_served_policy_account_update')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('''SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL
        AND (name=? OR tbl_name=? OR name IN (?,?)) ORDER BY type,name''', (VARIANTS, RETIRED, *ACCOUNTING))]


def install(conn, _context):
    prefix = policy_bodies.PREFIX
    pattern = policy_bodies.PATTERN
    columns = ','.join(policy_bodies.COLUMNS)
    for row in conn.execute(f'SELECT version,{columns} FROM {RETIRED}'):
        if policy_bodies.read(conn, row[0]) is not None:
            raise MigrationError(f'Shared policy setting already exists for {row[0]!r}')
        policy_bodies.save(conn, *row)

    accounting = definitions(conn, ACCOUNTING)
    for name in ACCOUNTING:
        conn.execute(f'DROP TRIGGER {name}')
    variants = definitions(conn, (VARIANTS,))[VARIANTS].replace(f' REFERENCES {RETIRED}(version)', '')
    owners = conn.execute('''SELECT sql FROM sqlite_schema WHERE tbl_name=? AND sql IS NOT NULL
        AND type IN ('index','trigger') ORDER BY type,name''', (VARIANTS,)).fetchall()
    _replace_table(conn, VARIANTS, variants)
    for sql, in owners:
        conn.execute(sql)
    for name in ACCOUNTING:
        conn.execute(accounting[name].split(f'UPDATE {RETIRED}', 1)[0] + 'END')
    conn.execute(policy_bodies.view())
    old = f'SELECT version,{columns},served FROM {RETIRED}'
    new = f'SELECT version,{columns},served FROM {policy_bodies.VIEW}'
    if conn.execute(f'{old} EXCEPT {new}').fetchone() or conn.execute(f'{new} EXCEPT {old}').fetchone():
        raise MigrationError('Shared recommendation policy inventory changed during migration')
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f'''CREATE TRIGGER recommendation_policy_base_required BEFORE INSERT ON {VARIANTS}
        WHEN NEW.base_version IS NOT NULL AND NOT EXISTS(SELECT 1 FROM settings WHERE key='{prefix}'||NEW.base_version)
        BEGIN SELECT RAISE(ABORT,'Missing shared recommendation policy body'); END''')
    conn.execute(f'''CREATE TRIGGER recommendation_policy_base_retained BEFORE DELETE ON settings
        WHEN OLD.key GLOB '{pattern}' AND EXISTS(SELECT 1 FROM {VARIANTS} WHERE base_version=substr(OLD.key,{len(prefix) + 1}))
        BEGIN SELECT RAISE(ABORT,'Shared recommendation policy body is in use'); END''')
    conn.execute(f'''CREATE TRIGGER recommendation_policy_base_immutable BEFORE UPDATE ON settings
        WHEN OLD.key GLOB '{pattern}' AND (NEW.key IS NOT OLD.key OR NEW.value IS NULL
          OR json_extract(NEW.value,'$[1]') IS NOT json_extract(OLD.value,'$[1]')
          OR json_extract(NEW.value,'$[2]') IS NOT json_extract(OLD.value,'$[2]')
          OR json_extract(NEW.value,'$[4]') IS NOT json_extract(OLD.value,'$[4]'))
        BEGIN SELECT RAISE(ABORT,'Recommendation policy content is immutable'); END''')


def validate(conn):
    if set(definitions(conn, (RETIRED, policy_bodies.VIEW))) != {policy_bodies.VIEW}:
        raise MigrationError('Shared recommendation policy bodies were not moved to settings')
