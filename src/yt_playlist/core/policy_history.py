"""Compact impression attribution with bounded historical configuration replay."""
from pathlib import Path

from . import fao, file_collection, file_map, policy_documents
from .evidence_ledger import canonical
from .policy_history_read import available, configuration, directory, document, head, unpack as _configuration


def definitions():
    yield ('recommendation_policy_history', 'CREATE TABLE recommendation_policy_history ('
        'version TEXT PRIMARY KEY NOT NULL CHECK(length(version)>0),created_at REAL NOT NULL,'
        "state TEXT NOT NULL CHECK(state IN ('archived','expired'))) WITHOUT ROWID")
    yield ('historical_impression_policies', 'CREATE TABLE historical_impression_policies ('
        'impression_id INTEGER NOT NULL REFERENCES rec_served_impressions(id) ON DELETE CASCADE,'
        "kind TEXT NOT NULL CHECK(kind IN ('primary','pool')),policy_version TEXT NOT NULL "
        'REFERENCES recommendation_policy_history(version),PRIMARY KEY(impression_id,kind)) WITHOUT ROWID')
    yield ('ix_historical_impression_policy','CREATE INDEX ix_historical_impression_policy '
        'ON historical_impression_policies(policy_version,impression_id)')
    yield ('policy_history_archive','CREATE TABLE policy_history_archive ('
        'id INTEGER PRIMARY KEY CHECK(id=1),publication BLOB NOT NULL CHECK(length(publication)=32))')
    yield ('historical_impression_matches', 'CREATE TRIGGER historical_impression_matches BEFORE INSERT '
        'ON historical_impression_policies WHEN NOT EXISTS(SELECT 1 FROM rec_served_impressions i '
        "WHERE i.id=NEW.impression_id AND ((NEW.kind='primary' AND NEW.policy_version=i.model_version) OR "
        "(NEW.kind='pool' AND NEW.policy_version=CAST(json_extract(i.provenance,'$.pool_policy') AS TEXT)))) "
        "BEGIN SELECT RAISE(ABORT,'Historical policy does not match its impression'); END")
    yield ('historical_impression_immutable','CREATE TRIGGER historical_impression_immutable BEFORE UPDATE '
        "ON historical_impression_policies BEGIN SELECT RAISE(ABORT,'Historical policy attribution is immutable'); END")
    yield ('historical_impression_retained','CREATE TRIGGER historical_impression_retained BEFORE DELETE '
        'ON historical_impression_policies WHEN EXISTS(SELECT 1 FROM rec_served_impressions WHERE id=OLD.impression_id) '
        "BEGIN SELECT RAISE(ABORT,'Retained impression requires its historical identity'); END")


def capture_guards():
    yield ('recommendation_impression_link_retained',
        'CREATE TRIGGER recommendation_impression_link_retained BEFORE DELETE ON recommendation_impression_policies '
        'WHEN EXISTS(SELECT 1 FROM rec_served_impressions WHERE id=OLD.impression_id) AND NOT EXISTS('
        'SELECT 1 FROM historical_impression_policies h WHERE h.impression_id=OLD.impression_id '
        'AND h.kind=OLD.kind AND h.policy_version=OLD.raw_version) '
        "BEGIN SELECT RAISE(ABORT,'Retained impression requires its policy evidence'); END")
    yield ('historical_impression_capture',
        'CREATE TRIGGER historical_impression_capture AFTER INSERT ON recommendation_impression_policies '
        "WHEN NEW.resolution_reason='missing_policy' AND EXISTS(SELECT 1 FROM recommendation_policy_history "
        'WHERE version=NEW.raw_version) BEGIN INSERT INTO historical_impression_policies '
        'VALUES (NEW.impression_id,NEW.kind,NEW.raw_version); DELETE FROM recommendation_impression_policies '
        'WHERE impression_id=NEW.impression_id AND kind=NEW.kind; END')


def install_capture(conn, context=None):
    for name,sql in capture_guards():
        conn.execute('DROP TRIGGER IF EXISTS '+name)
        conn.execute(sql)


def validate_capture(conn):
    from .schema_contract import tokens as _tokens
    for name,sql in capture_guards():
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None or _tokens(row[0])!=_tokens(sql):
            raise ValueError('Incompatible historical policy capture: '+name)


def install(conn, context=None):
    for _,sql in definitions():
        conn.execute(sql)


def validate(conn):
    from .schema_contract import tokens as _tokens
    for name,sql in definitions():
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None or _tokens(row[0])!=_tokens(sql):
            raise ValueError('Incompatible historical policy owner: '+name)


def configurations(conn):
    """Stream a stable archive without decompressing a leaf for every policy."""
    with policy_documents.retained_reader(directory(conn)):
        for version,saved in file_map.items(directory(conn),head(conn)):
            if saved.get('state')=='archived':
                yield version,_configuration(saved)


def candidates(conn, now, limit=128):
    """Find impression-only owners without reserving the serving writer."""
    from ..repos import policy_storage
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('Policy history batch must be 1 to 128')
    return [row[0] for row in conn.execute(
        'SELECT p.version FROM recommendation_policies p WHERE created_at<? AND '
        'p.version IN (SELECT policy_version FROM recommendation_impression_policies) AND '
        + policy_storage.operational_protection(conn) + ' ORDER BY created_at,version LIMIT ?',
        (now-policy_storage.GRACE_SECONDS, limit))]


def prepare_batch(conn, now, limit=128, *, versions=None):
    """Recheck operational closure under the caller's writer reservation."""
    from ..repos import policy_storage
    if not conn.in_transaction:
        raise RuntimeError('Policy history requires a writer reservation')
    if type(limit) is not int or not 1<=limit<=128:
        raise ValueError('Policy history batch must be 1 to 128')
    parameters = [now-policy_storage.GRACE_SECONDS]
    selected = ''
    if versions is not None:
        versions = list(versions)
        if not versions or len(versions)>limit or any(not isinstance(version,str) for version in versions):
            raise ValueError('Policy history selection must contain 1 to limit versions')
        selected = ' AND p.version IN ('+','.join('?' for _ in versions)+')'
        parameters.extend(versions)
    rows = conn.execute('SELECT * FROM recommendation_policies p WHERE created_at<? AND '
        +policy_storage.operational_protection(conn)+' AND EXISTS(SELECT 1 FROM recommendation_impression_policies i '
        'WHERE i.policy_version=p.version)'+selected+' ORDER BY created_at,version LIMIT ?',
        (*parameters,limit)).fetchall()
    changes, used = {}, 0
    for row in rows:
        value = policy_documents.read(row['configuration_filename'],row['content_digest'],
            offset=row['configuration_offset'],size=row['byte_size'])
        base = None
        if row['base_version'] is not None:
            source = conn.execute('SELECT * FROM recommendation_policy_bodies WHERE version=?',(row['base_version'],)).fetchone()
            if source is None:
                raise fao.CorruptFile('Required shared historical policy is missing')
            base = {'version':source['version'],'created_at':source['created_at'],
                'configuration':policy_documents.read(source['configuration_filename'],source['content_digest'],
                    offset=source['configuration_offset'],size=source['byte_size'])}
        saved = {'state':'archived','created_at':row['created_at'],'configuration':value,'base':base}
        size = len(canonical({row['version']:saved}))
        if changes and used+size>file_map.MAX_WORK//4:
            break
        changes[row['version']] = saved
        used += size
    if not changes:
        return 0
    changes = file_map.bounded_changes(directory(conn),head(conn),changes)
    publication = file_map.update(directory(conn),head(conn),changes)
    verified = file_map.get_many(directory(conn),publication,changes)
    if verified!=changes:
        raise fao.CorruptFile('Historical policy publication changed the original configuration')
    conn.execute('INSERT INTO policy_history_archive VALUES (1,?) ON CONFLICT(id) DO UPDATE SET publication=excluded.publication',
                 (bytes.fromhex(publication),))
    trigger = conn.execute("SELECT sql FROM sqlite_schema WHERE name='recommendation_impression_link_retained'").fetchone()
    if trigger:
        conn.execute('DROP TRIGGER recommendation_impression_link_retained')
    try:
        for version,value in changes.items():
            conn.execute("INSERT INTO recommendation_policy_history VALUES (?,?,'archived') "
                "ON CONFLICT(version) DO UPDATE SET state='archived'",(version,value['created_at']))
            conn.execute('INSERT INTO historical_impression_policies SELECT impression_id,kind,policy_version '
                'FROM recommendation_impression_policies WHERE policy_version=? ON CONFLICT(impression_id,kind) DO NOTHING',(version,))
            conn.execute('DELETE FROM recommendation_impression_policies WHERE policy_version=?',(version,))
            conn.execute('DELETE FROM recommendation_policies WHERE version=?',(version,))
    finally:
        if trigger:
            conn.execute(trigger[0])
    return len(changes)


def expire(conn, now, limit=128):
    rows = conn.execute("SELECT version FROM recommendation_policy_history WHERE state='archived' "
        'AND created_at<? ORDER BY created_at,version LIMIT ?',(now-180*86400,limit)).fetchall()
    if not rows:
        return 0
    changes = {version:{'state':'expired'} for version, in rows}
    changes = file_map.bounded_changes(directory(conn),head(conn),changes)
    publication = file_map.update(directory(conn),head(conn),changes)
    conn.execute('UPDATE policy_history_archive SET publication=? WHERE id=1',(bytes.fromhex(publication),))
    conn.executemany("UPDATE recommendation_policy_history SET state='expired' WHERE version=?",[(version,) for version in changes])
    return len(changes)


def references(conn, *, root=None):
    if available(conn):
        yield from file_map.pages(directory(conn) if root is None else root,head(conn))


def export(conn, target):
    return fao.export(directory(conn),references(conn),target)


def restore(conn, manifest):
    saved, root = Path(manifest).resolve().parent, directory(conn)
    fao.restore(root,manifest,expected=(root/path.relative_to(saved) for path in references(conn,root=saved)))


def collect(conn, now, limit=16):
    if conn.in_transaction:
        raise RuntimeError('Policy history collection requires committed expiry')
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('Policy history collection limit must be 1 to 128')
    root = directory(conn)
    with file_collection.collection(conn, root, name='.collect.json', valid_shard=_shard) as work, fao.collector(root) as admitted:
        if not admitted:
            return 0
        conn.execute('BEGIN IMMEDIATE')
        try:
            budget, removed = file_map.Budget(), 0
            for _ in range(limit):
                path = work.next()
                if (path is None or path.suffix != '.json' or len(path.stem) != 64
                        or any(c not in '0123456789abcdef' for c in path.stem)
                        or path.stem[:2] != path.parent.name):
                    continue
                info = fao.stat(path)
                if fao.Retention(30,'ctime').eligible(info,now):
                    if not file_map.contains_page(root,head(conn),path.stem,budget):
                        removed += fao.delete(path,expected=info.identity,missing_ok=True)
            work.save()
            return removed
        finally:
            conn.rollback()


def _shard(name):
    return isinstance(name, str) and len(name) == 2 and all(c in '0123456789abcdef' for c in name)
