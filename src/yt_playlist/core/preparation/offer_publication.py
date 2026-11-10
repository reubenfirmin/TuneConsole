"""Publish a reconciled Home offer window with continuing source compatibility."""
import json
import sqlite3
import time

from .. import home_offers
from ..schema_contract import MigrationError
from .recommendation_state import BYTE_LIMITS, JOBS, document, projection


HEAD = 'published_offers'
JOB = JOBS['home-offers']
LIMIT = '1.7976931348623157e308'


def definitions():
    yield HEAD, f'''CREATE TABLE {HEAD} (
        id INTEGER PRIMARY KEY CHECK(id=1), prepared_revision INTEGER NOT NULL,
        published_at REAL NOT NULL, write_revision INTEGER NOT NULL DEFAULT 0 CHECK(write_revision>=0))'''
    yield 'published_offers_originals', '''CREATE TABLE published_offers_originals (
        surface TEXT PRIMARY KEY CHECK(surface='home_shown'),
        payload TEXT NOT NULL, built_at REAL NOT NULL) WITHOUT ROWID'''
    for operation in ('UPDATE', 'DELETE'):
        name = 'published_offers_originals_' + operation.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.published_offers_originals '
                     "BEGIN SELECT RAISE(ABORT,'Published offer originals require the future cleanup procedure'); END")
    for operation in ('INSERT', 'UPDATE'):
        name = 'published_offers_signature_' + operation.lower()
        other = ' WHERE signature<>OLD.signature' if operation == 'UPDATE' else ''
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.prepared_home_offer_signatures BEGIN '
            'SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM prepared_home_offer_dedup WHERE id=1) '
            f'OR (SELECT count(*) FROM prepared_home_offer_signatures{other})>=100 '
            "THEN RAISE(ABORT,'Published offer signatures require a window with at most 100 entries') END; END")
    yield 'published_offers_window_delete', (
        'CREATE TRIGGER published_offers_window_delete BEFORE DELETE ON main.prepared_home_offer_dedup '
        'WHEN EXISTS(SELECT 1 FROM prepared_home_offer_signatures) '
        "BEGIN SELECT RAISE(ABORT,'Published offer signatures still reference the window'); END")
    for operation in ('INSERT', 'UPDATE'):
        name = 'published_offers_validate_' + operation.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {operation} ON main.rec_proposals '
            "WHEN NEW.surface='home_shown' BEGIN "
            f"SELECT CASE WHEN length(CAST(NEW.payload AS BLOB))>{BYTE_LIMITS['offers']} "
            "THEN RAISE(ABORT,'Published offer document exceeds its byte budget') END; "
            "SELECT CASE WHEN (json_valid(NEW.payload) AND json_type(NEW.payload)='object' AND "
            f"typeof(NEW.built_at) IN ('integer','real') AND NEW.built_at BETWEEN -{LIMIT} AND {LIMIT} AND "
            '(SELECT count(*)=count(DISTINCT key) FROM json_each(NEW.payload)) AND '
            "(NOT EXISTS(SELECT 1 FROM json_each(NEW.payload)) OR ("
            "(SELECT count(*)=2 FROM json_each(NEW.payload)) AND "
            "json_type(NEW.payload,'$.epoch')='integer' AND typeof(json_extract(NEW.payload,'$.epoch'))='integer' AND "
            "json_extract(NEW.payload,'$.epoch')>=0 AND json_type(NEW.payload,'$.signatures')='array' AND "
            "json_array_length(NEW.payload,'$.signatures')<=100 AND "
            "NOT EXISTS(SELECT 1 FROM json_each(NEW.payload,'$.signatures') "
            "WHERE type<>'text' OR length(CAST(value AS BLOB)) NOT BETWEEN 1 AND 1024) AND "
            "(SELECT count(*)=count(DISTINCT value) FROM json_each(NEW.payload,'$.signatures'))))) "
            "IS NOT TRUE THEN RAISE(ABORT,'Invalid published Home offer state') END; END")
    payload = "CASE WHEN NEW.surface='home_shown' THEN NEW.payload ELSE '{}' END"
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        name = 'published_offers_sync_' + operation.lower()
        body = 'DELETE FROM prepared_home_offer_signatures;DELETE FROM prepared_home_offer_dedup;'
        if operation != 'DELETE':
            body += (f"INSERT INTO prepared_home_offer_dedup SELECT 1,json_extract({payload},'$.epoch'),NEW.built_at "
                     f"WHERE EXISTS(SELECT 1 FROM json_each({payload}));"
                     f"INSERT INTO prepared_home_offer_signatures SELECT value FROM json_each({payload},'$.signatures');")
        sides = ('OLD', 'NEW') if operation == 'UPDATE' else ('OLD',) if operation == 'DELETE' else ('NEW',)
        condition = ' OR '.join(f"{side}.surface='home_shown'" for side in sides)
        yield name, (f'CREATE TRIGGER {name} AFTER {operation} ON main.rec_proposals WHEN {condition} BEGIN ' +
                     body + f'UPDATE {HEAD} SET write_revision=write_revision+1 WHERE id=1; END')


def validate(conn):
    from ..schema_contract import tokens as _tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible offer publication object: ' + name)


def validate_installed(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='home_offer_dedup'").fetchone():
        return
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return
    JOB.validate(conn)
    validate(conn)
    if conn.execute(f'SELECT 1 FROM {HEAD} WHERE id=1').fetchone() is None:
        raise MigrationError('Active offer publication has no committed head')


def publish(conn, *, revision, clock=time.time):
    if conn.in_transaction:
        raise MigrationError('Publication requires an idle connection')
    try:
        conn.execute('BEGIN IMMEDIATE')
        JOB._version(conn)
        JOB.validate(conn)
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
            validate(conn)
            if conn.execute(f'SELECT 1 FROM {HEAD} WHERE id=1').fetchone():
                conn.rollback()
                return
            raise MigrationError('Incomplete offer publication has no committed head')
        progress = JOB.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.prepared_revision != revision
                or progress.input_revision != revision or conn.execute(f'SELECT 1 FROM {JOB.changes} LIMIT 1').fetchone()):
            raise MigrationError('Offer publication requires the requested reconciled preparation revision')
        columns = ','.join(projection('offers'))
        source = conn.execute(f"SELECT {columns} FROM main.rec_proposals WHERE surface='home_shown'").fetchone()
        retained = conn.execute(f'SELECT {columns} FROM prepared_offers_originals').fetchone()
        original = document('offers', source) if source else None
        captured = document('offers', retained) if retained else None
        if original != captured:
            raise MigrationError('Prepared offer original differs from the installed document')
        parsed = home_offers.parse(json.loads(original['payload']), original['built_at']) if original else None
        heads = [tuple(row) for row in conn.execute('SELECT epoch,built_at FROM prepared_home_offer_dedup')]
        signatures = [row[0] for row in conn.execute(
            'SELECT signature FROM prepared_home_offer_signatures ORDER BY signature LIMIT 101')]
        expected_heads = [(parsed[0], original['built_at'])] if parsed else []
        if heads != expected_heads or signatures != (parsed[1] if parsed else []):
            raise MigrationError('Prepared offer owner differs from its verified input')
        for _, sql in definitions():
            conn.execute(sql)
        conn.execute('INSERT INTO published_offers_originals SELECT * FROM prepared_offers_originals')
        conn.execute(f'INSERT INTO {HEAD}(id,prepared_revision,published_at) VALUES (1,?,?)', (revision, clock()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def main(argv=None):
    import argparse
    from contextlib import closing
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--revision', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode=rw', uri=True, timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            publish(conn, revision=args.revision)
            print(json.dumps(dict(conn.execute(f'SELECT * FROM {HEAD} WHERE id=1').fetchone())))
    except (MigrationError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f'Offer publication failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
