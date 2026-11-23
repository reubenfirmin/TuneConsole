"""Domain-owned cold originals without a SQL receipt per original (#697)."""
from yt_playlist.core import file_paths
import json
import logging
import sqlite3
import time

from . import fao, file_collection, file_map
from .evidence_ledger import canonical
from . import legacy_evidence_contract as receipts

DOMAINS = ('catalog','artist','album','playlist','recommendation','workflow','metadata','discovery')
SQL = ("CREATE TABLE legacy_archive_heads (domain TEXT PRIMARY KEY NOT NULL CHECK(domain IN "
       "('catalog','artist','album','playlist','recommendation','workflow','metadata','discovery'))," 
       "publication BLOB NOT NULL CHECK(length(publication)=32)) WITHOUT ROWID")
CUTOVER_SQL = ('CREATE TABLE legacy_archive_cutovers (domain TEXT PRIMARY KEY NOT NULL '
               'REFERENCES legacy_archive_heads(domain),verified_publication BLOB NOT NULL '
               'CHECK(length(verified_publication)=32),retired_at REAL NOT NULL) WITHOUT ROWID')
RETIREMENT_COLUMNS = {
    'verified_publication': 'BLOB CHECK(verified_publication IS NULL OR length(verified_publication)=32)',
    'retired_at': 'REAL CHECK((retired_at IS NULL)=(verified_publication IS NULL))',
}
FOLD_RETIREMENTS = 'fold_archive_retirement_receipts'
RETIRE_PACK_RECEIPTS = 'retire_empty_archive_receipts'
FILE_REFERENCES = 'archive_evidence_files'
EARLIER_PACK_RECEIPTS = {domain: table for domain, table in receipts.DOMAINS.items() if 'legacy' in table}
WORKFLOW_PUBLICATION = 'retained_workflow_publication'
ARTIST_PUBLICATION = 'retained_artist_publication'
RETAINED_PUBLICATIONS = {'workflow': WORKFLOW_PUBLICATION, 'artist': ARTIST_PUBLICATION,
                         'discovery': 'retained_discovery_publication'}
PUBLICATIONS = {**RETAINED_PUBLICATIONS, 'metadata': 'retained_metadata_publication'}
log = logging.getLogger(__name__)


def available(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='legacy_archive_heads'").fetchone() is not None


def install(conn, context=None):
    conn.execute(SQL)
    conn.execute(CUTOVER_SQL)


def validate(conn):
    from .schema_contract import tokens as _tokens
    for sql in (SQL,CUTOVER_SQL):
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',(sql.split()[2],)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise ValueError('Incompatible legacy archive heads')


def fold_retirements(conn, _context):
    for name, declaration in RETIREMENT_COLUMNS.items():
        conn.execute(f'ALTER TABLE legacy_archive_heads ADD COLUMN {name} {declaration}')
    conn.execute('UPDATE legacy_archive_heads AS h SET (verified_publication,retired_at)='
                 '(SELECT c.verified_publication,c.retired_at FROM legacy_archive_cutovers c WHERE c.domain=h.domain) '
                 'WHERE h.domain IN (SELECT domain FROM legacy_archive_cutovers)')
    conn.execute('DROP TABLE legacy_archive_cutovers')


def validate_folded_retirements(conn):
    from .schema_contract import MigrationError
    from .schema_inspection import objects

    if 'legacy_archive_cutovers' in objects(conn, ('legacy_archive_cutovers',)):
        raise MigrationError('Archive retirement history still has a separate table')


def file_references_sql(installed):
    select = ' UNION ALL '.join(f'SELECT filename FROM {table}' for table in receipts.DOMAINS.values()
                                if table in installed)
    return f'CREATE VIEW {FILE_REFERENCES} AS {select or "SELECT NULL AS filename WHERE 0"}'


def _refresh_file_references(conn):
    from .schema_inspection import objects

    conn.execute(f'DROP VIEW IF EXISTS {FILE_REFERENCES}')
    conn.execute(file_references_sql(objects(conn, receipts.DOMAINS.values())))


def retire_empty_receipts(conn, _context):
    from .schema_inspection import objects

    installed = objects(conn, EARLIER_PACK_RECEIPTS.values())
    for table in installed:
        if conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is None:
            conn.execute(f'DROP TABLE {table}')
    _refresh_file_references(conn)


def validate_file_references(conn):
    from .schema_contract import MigrationError, tokens
    from .schema_inspection import definitions

    installed = definitions(conn, (*receipts.DOMAINS.values(), FILE_REFERENCES))
    if FILE_REFERENCES not in installed or tokens(installed[FILE_REFERENCES]) != tokens(file_references_sql(installed)):
        raise MigrationError('Incompatible archive evidence file references')


def directory(conn):
    return file_paths.directory(conn,'legacy-journals')


def head(conn, domain):
    if domain not in DOMAINS:
        raise ValueError('Unknown archive domain')
    row = conn.execute('SELECT publication FROM legacy_archive_heads WHERE domain=?',(domain,)).fetchone()
    return row[0].hex() if row else None


def key(kind, source):
    return canonical([kind,source]).decode()


def disposition(domain, kind, payload, reason):
    """Actual producer kinds, not unresolved historical subject bindings."""
    durable = ('actions','rec_recipes','cluster_canvas','rec_feedback','feedback_reassociation')
    if kind in durable or kind.startswith(('playlist_scope_','constraint_legacy:playlist')):
        return 'durable', 'user decision or scope recovery'
    # An explicit obligation outranks the ordinary conversion diagnostic class.
    try:
        body = json.loads(payload)
    except ValueError:
        body = None
    def obligation(value):
        if isinstance(value,dict):
            for name,item in value.items():
                if name in ('paid','user_decision','unknown_scope','scope_unknown') and item:
                    return True
                if name in ('provider','source') and item in ('openai','anthropic','user','manual'):
                    return True
                if obligation(item):
                    return True
        elif isinstance(value,list):
            return any(obligation(item) for item in value)
        return False
    if obligation(body) or 'unknown scope' in reason.casefold():
        return 'durable', 'paid, user or unknown-scope obligation'
    known = (kind in ('library','discovery','ambiguous_album_artist','enrichment_conflict',
                      'legacy_enrichment_attempt','album_tracklists','playlists','stale_dismissed',
                      'rec_impressions','rec_mood','taste_modes','artist_visible_impression')
        or kind.startswith(('album_legacy:','artist_reconcile:','artist_state:',
                            'constraint_legacy:enrichment_','discovery_cutover:')))
    if known:
        return 'diagnostic', 'bounded conversion/reconciliation recovery'
    return 'durable', 'unclassified producer; disposition required before expiry'


def read(conn, domain, kind, source):
    """Read an original with the caller's file lease and SQLite snapshot."""
    publication = retained_publication(conn, domain)
    if publication is None and available(conn):
        publication = head(conn, domain)
    return file_map.get(directory(conn) / domain, publication, key(kind, source))


def workflow_publication(conn):
    return retained_publication(conn, 'workflow')


def retained_publication(conn, domain):
    setting = PUBLICATIONS.get(domain)
    if setting is None:
        return None
    row = conn.execute('SELECT value FROM settings WHERE key=?', (setting,)).fetchone()
    return row[0] if row else None


def _store_publication(conn, domain, publication, *, receipts_installed):
    if receipts_installed:
        conn.execute('INSERT INTO legacy_archive_heads(domain,publication) VALUES (?,?) '
                     'ON CONFLICT(domain) DO UPDATE SET publication=excluded.publication',
                     (domain, bytes.fromhex(publication)))
    else:
        conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (PUBLICATIONS[domain], publication))


def put(conn, domain, values):
    """Insert-once exact originals; the writer owns commit and rollback."""
    if not conn.in_transaction:
        raise RuntimeError('Archive journal publication requires a writer reservation')
    receipts_installed = available(conn)
    previous = head(conn, domain) if receipts_installed else retained_publication(conn, domain)
    root = directory(conn) / domain
    values = list(values)
    existing = file_map.get_many(root,previous,[key(kind,source) for kind,source,_ in values])
    pending = {}
    for kind,source,value in values:
        identity = key(kind,source)
        if identity in pending or identity in existing:
            continue
        if not isinstance(value, dict) or not (isinstance(value.get('payload'), str)
                or value.get('state') == 'unavailable' and value.get('payload') is None
                and isinstance(value.get('receipt'), dict)):
            raise ValueError('Invalid original journal entry')
        pending[identity] = value
    if not pending:
        return 0
    publication = file_map.update(root,previous,pending)
    _store_publication(conn, domain, publication, receipts_installed=receipts_installed)
    return len(pending)


def retain_many(conn, domain, rows):
    values, used = [], 0
    now = time.time()
    for kind,source,payload,reason in rows:
        if not all(isinstance(v,str) for v in (kind,source,payload,reason)) or not kind or not reason:
            raise ValueError('Invalid original journal entry')
        used += len(payload.encode())
        if len(values) == 128 or used > 32*1024*1024:
            raise ValueError('Archive journal batch exceeds its work budget')
        retention, basis = disposition(domain,kind,payload,reason)
        values.append((kind,source,{'payload':payload,'reason':reason,'archived_at':now,
            'subjects':receipts.subjects(conn,domain,kind,source,payload),
            'state':'retained','retention':retention,'disposition':basis}))
    return put(conn,domain,values)


def prepare_batch(conn, domain, limit=128, *, keys=None):
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('Archive journal batch must be 1 to 128')
    table = receipts.DOMAINS[domain]
    if not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(table,)).fetchone():
        return 0
    parameters, selected = [], ''
    if keys is not None:
        keys = list(keys)
        if (not keys or len(keys)>limit
                or any(len(key)!=2 or any(not isinstance(value,str) for value in key) for key in keys)):
            raise ValueError('Journal selection must contain 1 to limit original identities')
        selected = ' WHERE (kind,source_id) IN ('+','.join('(?,?)' for _ in keys)+')'
        parameters = [value for key in keys for value in key]
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(f'SELECT * FROM {table}'+selected+' ORDER BY kind,source_id LIMIT ?',
                          (*parameters,limit)).fetchall()
    receipts_installed = available(conn)
    if not rows and receipts_installed and head(conn,domain) is None:
        publication = file_map.update(directory(conn)/domain,None,{})
        conn.execute('INSERT INTO legacy_archive_heads(domain,publication) VALUES (?,?)',(domain,bytes.fromhex(publication)))
    values = []
    for row in rows:
        value = {'reason': row['reason'], 'archived_at': row['archived_at'],
                 'subjects': {column: row[column] for column in receipts.SUBJECTS[domain]},
                 'resolution_status': row['resolution_status']}
        try:
            raw = receipts.read(domain, row['filename'], row['content_digest'], row['byte_size'], row['file_offset'])[1]
        except (OSError, ValueError, fao.FileChanged) as error:
            if receipts_installed:
                raise
            log.warning('Archived %s evidence is unreadable at %s: %s', domain, row['filename'], error)
            locator = {field: row[field] for field in ('filename', 'content_digest', 'byte_size', 'file_offset')}
            value.update(payload=None, state='unavailable', receipt=locator, retention='durable',
                         disposition='original evidence file is unreadable')
        else:
            retention, basis = disposition(domain, row['kind'], raw, row['reason'])
            value.update(payload=raw, state='retained', retention=retention, disposition=basis)
        values.append((row['kind'], row['source_id'], value))
    put(conn,domain,values)
    publication = head(conn, domain) if receipts_installed else retained_publication(conn, domain)
    verified = file_map.get_many(directory(conn)/domain,publication,[key(kind,source) for kind,source,_ in values])
    for kind,source,value in values:
        if verified[key(kind,source)] != value:
            raise fao.CorruptFile('Journal conversion differs from its original receipt')
    # This explicit disposition is the only boundary allowed to remove receipts.
    # The surrounding writer transaction publishes the journal and deletion together.
    trigger = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',('retained_'+table,)).fetchone()
    if trigger:
        conn.execute('DROP TRIGGER retained_'+table)
    try:
        conn.executemany(f'DELETE FROM {table} WHERE kind=? AND source_id=?',[(kind,source) for kind,source,_ in values])
    finally:
        if trigger:
            conn.execute(trigger[0])
    return len(rows)


def retire_verified(conn, domain, verified_publication, now):
    """Explicit #626 cutover gate, never called by startup or idle maintenance.

    The caller must independently verify every converted original first. A
    concurrent publication invalidates that approval. Empty receipt owners alone
    do not establish verification, and an incomplete conversion cannot retire.
    """
    from .legacy_evidence_contract import DOMAINS as tables
    if not conn.in_transaction:
        raise RuntimeError('Archive retirement requires a writer reservation')
    if verified_publication is None or head(conn,domain) != verified_publication:
        raise ValueError('Archive cutover requires the independently verified current publication')
    table = tables[domain]
    if conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
        raise ValueError('Archive conversion is incomplete')
    conn.execute('UPDATE legacy_archive_heads SET verified_publication=?,retired_at=? WHERE domain=?',
                 (bytes.fromhex(verified_publication),now,domain))
    conn.execute('DROP TABLE '+table)
    _refresh_file_references(conn)


def prepare_installed_receipts(conn, progress):
    """Finish old nonempty packs without holding a regular migration open."""
    from .schema_inspection import objects

    for domain, table in EARLIER_PACK_RECEIPTS.items():
        while True:
            conn.execute('BEGIN IMMEDIATE')
            with conn:
                if table not in objects(conn, (table,)):
                    break
                moved = prepare_batch(conn, domain)
                if not moved:
                    retire_verified(conn, domain, head(conn, domain), time.time())
            if not moved:
                break
            progress(domain, moved)


def retired_relations(conn):
    from .schema_inspection import objects

    columns = objects(conn, ('legacy_archive_heads', 'legacy_archive_cutovers'))
    if 'legacy_archive_cutovers' in columns:
        validate(conn)
        query = 'SELECT domain FROM legacy_archive_cutovers'
    elif 'legacy_archive_heads' in columns and 'verified_publication' in columns['legacy_archive_heads'][1]:
        query = 'SELECT domain FROM legacy_archive_heads WHERE retired_at IS NOT NULL'
    else:
        return set()
    return {receipts.DOMAINS[domain] for domain, in conn.execute(query)}


def publications(conn):
    if available(conn):
        for domain,publication in conn.execute('SELECT domain,publication FROM legacy_archive_heads ORDER BY domain'):
            yield domain, publication.hex()
    for domain in PUBLICATIONS:
        publication = retained_publication(conn, domain)
        if publication is not None:
            yield domain, publication


def references(conn):
    for domain, publication in publications(conn):
        yield from file_map.pages(directory(conn) / domain, publication)


def maintain(conn, now, limit=128):
    """One bounded leaf page per tick; expiry keeps exact insert-once keys."""
    if not conn.in_transaction:
        raise RuntimeError('Archive expiry requires the writer reservation')
    cursor = directory(conn)/'.expiry.json'
    state = json.loads(fao.read(cursor).bytes) if cursor.exists() else {'domain':0,'after':''}
    domain = DOMAINS[state['domain']]
    root, publication = directory(conn)/domain, head(conn,domain)
    leaf = file_map.next_leaf(root,publication,state['after'] or None)
    updates = {}
    if leaf is not None:
        after,page = leaf
        for identity,value in page['entries'].items():
            if (value['state']=='retained' and value['retention']=='diagnostic'
                    and value['archived_at'] <= now-180*86400):
                updates[identity] = {'state':'expired','archived_at':value['archived_at'],
                                     'retention':'diagnostic','disposition':value['disposition']}
    if updates:
        current = file_map.update(root,publication,updates)
        conn.execute('UPDATE legacy_archive_heads SET publication=? WHERE domain=?',(bytes.fromhex(current),domain))
    # Routes survive copy-on-write publication and need no unbounded prefix scan.
    fao.write(cursor,canonical({'domain':state['domain'] if leaf else (state['domain']+1)%len(DOMAINS),
                                     'after':after if leaf else ''}))
    return len(updates)


def collect(conn, now, limit=16):
    """Bound orphan work; live SQL heads and older snapshots fence reclamation."""
    if conn.in_transaction:
        raise RuntimeError('Journal collection requires its own writer reservation')
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('Journal collection limit must be 1 to 128')
    root = directory(conn)
    removed = 0
    with file_collection.collection(conn, root, name='.collect.json', valid_shard=_shard,
                                    prefixes=sorted(DOMAINS)) as work, file_collection.collector(root) as admitted:
        if not admitted:
            return 0
        conn.execute('BEGIN IMMEDIATE')
        try:
            budget = file_map.Budget()
            for _ in range(limit):
                path = work.next()
                if (path is None or path.suffix != '.json' or len(path.stem) != 64
                        or any(c not in '0123456789abcdef' for c in path.stem)
                        or path.stem[:2] != path.parent.name):
                    continue
                domain = path.relative_to(root).parts[0]
                age = fao.read(path).info
                if (age.ctime_ns / 1e9 <= now - 30*86400):
                    try:
                        live = file_map.contains_page(root/domain,head(conn,domain),path.stem,budget)
                    except fao.FileBudgetExceeded:
                        break
                    if not live:
                        removed += fao.delete(path, expected=age.identity, missing_ok=True)
            work.save()
        finally:
            conn.rollback()
    return removed


def _shard(name):
    return isinstance(name, str) and len(name) == 2 and all(c in '0123456789abcdef' for c in name)
