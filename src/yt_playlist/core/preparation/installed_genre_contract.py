"""Repair finite genre programs and audit old taste-axis rows without copying them."""
from dataclasses import asdict
import json

from ..migrations import current
from ..migrations.runner import MigrationError
from ..migrations import v092_installed_genre_contract as historical
from . import domain_contracts


AXIS_FIELDS = dict.fromkeys(('kind','genre_key','family_key','value','resolution_reason'),'1')
AXIS_FIELDS['kind'] = """COALESCE((kind IN ('genre','family','other','unresolved_genre') AND (
    (kind='genre' AND genre_key IS NOT NULL AND family_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
 OR (kind='family' AND family_key IS NOT NULL AND genre_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
 OR (kind='other' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL AND length(value)>0
     AND value NOT LIKE 'genre:%' AND value NOT LIKE 'family:%' AND resolution_reason IS NULL)
 OR (kind='unresolved_genre' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL
     AND resolution_reason IN ('unknown_genre','ambiguous_genre')))),0)"""


def job(conn):
    return domain_contracts.contract_job(conn,'taste_axes',AXIS_FIELDS,
        signature_tag='installed-axis-contract-1',minimum=88)


def guards():
    import re
    expression = re.sub(r'\b(kind|genre_key|family_key|value|resolution_reason)\b',r'NEW.\1',AXIS_FIELDS['kind'])
    for operation in ('INSERT','UPDATE'):
        name = 'installed_taste_axis_guard_'+operation.lower()
        yield name, ('CREATE TRIGGER '+name+' BEFORE '+operation+' ON main.taste_axes '
            'WHEN ('+expression+") IS NOT 1 BEGIN SELECT RAISE(ABORT,'Invalid future taste axis; historical audit is separate'); END")


def definitions(conn):
    version = conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
    if 86<=version<92:
        # Index construction is deliberately excluded. Earlier supported
        # layouts admit the three missing indexes; they are maintenance work.
        names = set(historical.REPLACEMENTS) | {name for name in historical.ADDITIONS
            if not name.startswith('ix_') and (version>=89 or not name.startswith('recipe_'))}
        for sql in current.statements(version=92):
            name = historical._name(sql)
            if name in names:
                yield name,sql.strip().rstrip(';')
    if version>=88:
        yield from guards()


def install(conn, context=None):
    """Run within one regular DDL transaction, without examining stored rows."""
    for name,sql in definitions(conn):
        row = conn.execute('SELECT type,sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if row and current._tokens(row[1])==current._tokens(sql):
            continue
        if row:
            if name not in historical.REPLACEMENTS:
                raise MigrationError('Incompatible installed genre repair: '+name)
            # Admission already accepted only the exact known old definition.
            conn.execute('DROP '+row[0]+' '+name)
        conn.execute(sql)
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axis_legacy'").fetchone():
        columns = {row[1] for row in conn.execute('PRAGMA main.table_info(taste_axis_legacy)')}
        for declaration in historical.ARCHIVE_COLUMNS:
            if declaration.split()[0] not in columns:
                conn.execute('ALTER TABLE taste_axis_legacy ADD COLUMN '+declaration)


def install_guards(conn):
    """Carry the adopted future-write contract across an explicit owner upgrade."""
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axes' AND type='table'").fetchone():
        return
    for name, sql in guards():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None:
            conn.execute(sql)
        elif current._tokens(row[0]) != current._tokens(sql):
            raise MigrationError('Incompatible installed genre repair: ' + name)


def validate(conn, *, allow_missing_guards=False):
    for name,sql in definitions(conn):
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None and allow_missing_guards and name.startswith('installed_taste_axis_guard_'):
            continue
        if row is None or current._tokens(row[0])!=current._tokens(sql):
            raise MigrationError('Incompatible installed genre repair: '+name)
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axis_legacy'").fetchone():
        columns = {row[1] for row in conn.execute('PRAGMA main.table_info(taste_axis_legacy)')}
        if not {value.split()[0] for value in historical.ARCHIVE_COLUMNS}<=columns:
            raise MigrationError('Installed genre archive is missing additive evidence fields')


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--batches',type=int,default=1)
    parser.add_argument('--batch-size',type=int,default=32)
    parser.add_argument('--retry',action='store_true')
    args = parser.parse_args(argv)
    if not 1<=args.batches<=512 or not 1<=args.batch_size<=32:
        parser.error('--batches must be 1 to 512 and --batch-size 1 to 32')
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+
                ('?mode=ro' if args.status or args.verify else '?mode=rw'),uri=True,timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            selected = job(conn)
            if args.verify:
                result = domain_contracts.verify(conn,selected)
            elif args.status:
                result = selected.progress(conn) if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(selected.state,)).fetchone() else None
            else:
                selected.install(conn)
                for _ in range(args.batches):
                    result = selected.step(conn,batch_size=args.batch_size,retry=args.retry)
                    if result.status=='prepared':
                        break
            print(json.dumps(asdict(result) if result else {'status':'uninitialized'}))
    except (MigrationError,ValueError,sqlite3.Error) as exc:
        parser.exit(1,str(exc)+'\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
