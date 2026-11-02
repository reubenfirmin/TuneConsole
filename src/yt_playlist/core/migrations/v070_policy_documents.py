"""Move immutable shared policy bodies and compact variants to directly owned files."""
import json
import math

from yt_playlist.core import policy_documents
from yt_playlist.core.evidence_ledger import canonical
from .runner import MigrationError

BASE_FIELD = '_policy_base'
REPLACED = frozenset(('rec_serving_policies', 'rec_policy_bases', 'rec_policy_variants',
    'ix_rec_policy_created', 'ix_rec_policy_base', 'ix_rec_policy_unused_variants', 'ix_rec_policy_unused_bases'))


def statements():
    for table in ('recommendation_policy_bodies', 'recommendation_policies'):
        base = ('base_version TEXT REFERENCES recommendation_policy_bodies(version),'
                if table == 'recommendation_policies' else '')
        yield (f'CREATE TABLE {table} (version TEXT PRIMARY KEY NOT NULL CHECK(length(version)>0),'
               + base + 'configuration_filename TEXT NOT NULL CHECK(length(configuration_filename)>0),'
               "content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),"
               "byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size>0 AND byte_size<=8388608),"
               "configuration_offset INTEGER NOT NULL CHECK(typeof(configuration_offset)='integer' AND configuration_offset>=0),"
               "created_at REAL NOT NULL CHECK(typeof(created_at) IN ('real','integer') "
               'AND abs(created_at)<=1.7976931348623157e308),'
               "served INTEGER NOT NULL DEFAULT 0 CHECK(typeof(served)='integer' AND served IN (0,1))) WITHOUT ROWID")
        yield f'CREATE INDEX ix_{table}_unused ON {table}(created_at,version) WHERE served=0'
        immutable = ('version', 'base_version', 'content_digest', 'byte_size', 'created_at') if base else (
            'version', 'content_digest', 'byte_size', 'created_at')
        condition = ' OR '.join(f'NEW.{field} IS NOT OLD.{field}' for field in immutable)
        yield (f'CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} WHEN {condition} '
               "BEGIN SELECT RAISE(ABORT,'Recommendation policy content is immutable'); END")
    yield 'CREATE INDEX ix_recommendation_policy_base ON recommendation_policies(base_version,served,version)'
    yield 'CREATE INDEX ix_recommendation_policy_created ON recommendation_policies(created_at,version)'
    yield ("CREATE TABLE recommendation_impression_policies (impression_id INTEGER NOT NULL "
           "REFERENCES rec_served_impressions(id) ON DELETE CASCADE,kind TEXT NOT NULL "
           "CHECK(kind IN ('primary','pool')),raw_version TEXT NOT NULL CHECK(typeof(raw_version)='text'),"
           'policy_version TEXT REFERENCES recommendation_policies(version),resolution_reason TEXT '
           "CHECK(resolution_reason IN ('missing_policy','invalid_type')),PRIMARY KEY(impression_id,kind),"
           'CHECK((policy_version IS NOT NULL AND policy_version=raw_version AND resolution_reason IS NULL) OR '
           '(policy_version IS NULL AND resolution_reason IS NOT NULL))) WITHOUT ROWID')
    yield ('CREATE INDEX ix_recommendation_impression_policy ON recommendation_impression_policies('
           'policy_version,impression_id,kind) WHERE policy_version IS NOT NULL')
    yield ('CREATE INDEX ix_recommendation_unresolved_impression_policy ON recommendation_impression_policies('
           'raw_version,impression_id,kind) WHERE resolution_reason=\'missing_policy\'')
    yield ('CREATE TRIGGER recommendation_impression_evidence_insert BEFORE INSERT ON rec_served_impressions '
           "WHEN typeof(NEW.model_version)<>'text' OR CASE WHEN json_valid(NEW.provenance) "
           "THEN json_type(NEW.provenance)<>'object' ELSE 1 END "
           "BEGIN SELECT RAISE(ABORT,'Invalid served policy evidence'); END")
    yield ('CREATE TRIGGER recommendation_impression_policy_capture AFTER INSERT ON rec_served_impressions BEGIN '
           + _link_sql('i.id=NEW.id') + '; END')
    yield ('CREATE TRIGGER recommendation_impression_policy_immutable BEFORE UPDATE ON rec_served_impressions '
           'WHEN NEW.model_version IS NOT OLD.model_version OR NEW.provenance IS NOT OLD.provenance '
           "BEGIN SELECT RAISE(ABORT,'Served policy evidence is immutable'); END")
    yield ('CREATE TRIGGER recommendation_impression_link_immutable BEFORE UPDATE ON recommendation_impression_policies '
           'WHEN NEW.impression_id IS NOT OLD.impression_id OR NEW.kind IS NOT OLD.kind '
           'OR NEW.raw_version IS NOT OLD.raw_version '
           'OR (OLD.policy_version IS NOT NULL AND NEW.policy_version IS NOT OLD.policy_version) '
           "BEGIN SELECT RAISE(ABORT,'Impression policy ownership is immutable'); END")
    yield ('CREATE TRIGGER recommendation_impression_link_retained BEFORE DELETE ON recommendation_impression_policies '
           'WHEN EXISTS(SELECT 1 FROM rec_served_impressions WHERE id=OLD.impression_id) '
           "BEGIN SELECT RAISE(ABORT,'Retained impression requires its policy evidence'); END")
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER recommendation_impression_link_{event.lower()} BEFORE {event} '
               'ON recommendation_impression_policies WHEN NOT EXISTS('
               'SELECT 1 FROM rec_served_impressions i WHERE i.id=NEW.impression_id AND ('
               "(NEW.kind='primary' AND NEW.raw_version=i.model_version AND NEW.resolution_reason IS NOT 'invalid_type') "
               "OR (NEW.kind='pool' AND NEW.raw_version=CAST(json_extract(i.provenance,'$.pool_policy') AS TEXT) "
               "AND CASE WHEN json_type(i.provenance,'$.pool_policy') IN ('text','integer') "
               "THEN NEW.resolution_reason IS NOT 'invalid_type' ELSE NEW.resolution_reason='invalid_type' END))) "
               "BEGIN SELECT RAISE(ABORT,'Policy reference does not match its impression'); END")
    for event in ('INSERT', 'UPDATE', 'DELETE'):
        sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
        updates = ''.join(
            'UPDATE recommendation_policies SET served=EXISTS('
            'SELECT 1 FROM recommendation_impression_policies WHERE policy_version='
            f'{side}.policy_version) WHERE version={side}.policy_version; '
            'UPDATE recommendation_policy_bodies SET served=EXISTS('
            'SELECT 1 FROM recommendation_policies WHERE base_version=recommendation_policy_bodies.version AND served=1) '
            'WHERE version=(SELECT base_version FROM recommendation_policies '
            f'WHERE version={side}.policy_version); ' for side in sides)
        yield (f'CREATE TRIGGER recommendation_impression_account_{event.lower()} AFTER {event} '
               f'ON recommendation_impression_policies BEGIN {updates}END')


def _link_sql(where='1'):
    # Capture the two established message fields once, at the write boundary.
    # Retention queries use these indexed relationships, never a JSON traversal.
    return ('INSERT INTO recommendation_impression_policies('
            'impression_id,kind,raw_version,policy_version,resolution_reason) '
            "SELECT s.impression_id,s.kind,s.raw_version,p.version,CASE WHEN s.valid=0 THEN 'invalid_type' "
            "WHEN p.version IS NULL THEN 'missing_policy' END FROM ("
            "SELECT i.id impression_id,'primary' kind,i.model_version raw_version,1 valid "
            'FROM rec_served_impressions i WHERE ' + where + ' UNION ALL '
            "SELECT i.id,'pool',CAST(json_extract(i.provenance,'$.pool_policy') AS TEXT),"
            "json_type(i.provenance,'$.pool_policy') IN ('text','integer') "
            'FROM rec_served_impressions i WHERE (' + where + ') AND '
            "json_type(i.provenance,'$.pool_policy') IS NOT NULL AND "
            "json_type(i.provenance,'$.pool_policy')<>'null') s "
            'LEFT JOIN recommendation_policies p ON s.valid=1 AND p.version=s.raw_version')


def _configuration(raw, key):
    try:
        if not isinstance(raw, str):
            raise ValueError('Configuration must be JSON text')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Configuration must be an object')
        encoded = canonical({'format': 'recommendation-policy-1', 'configuration': value})
        if len(encoded) > 8 * 1024 * 1024:
            raise ValueError('Configuration exceeds file byte budget')
        return value
    except (ValueError, TypeError) as exc:
        raise MigrationError(f'Invalid recommendation policy {key!r}: {exc}') from exc


def _metadata(version, created):
    if (not isinstance(version, str) or not version
            or type(created) not in (float, int) or not math.isfinite(created)):
        raise MigrationError(f'Invalid recommendation policy metadata: {version!r}')


def apply(conn, context):
    # Validate all originals before creating files or replacing any owner.
    invalid = conn.execute("SELECT id FROM rec_served_impressions WHERE typeof(model_version)<>'text' "
                           "OR CASE WHEN json_valid(provenance) THEN json_type(provenance)<>'object' "
                           'ELSE 1 END LIMIT 1').fetchone()
    if invalid:
        raise MigrationError(f'Invalid served policy evidence: impression {invalid[0]}')
    orphan = conn.execute('SELECT v.model_version FROM rec_policy_variants v '
                          'LEFT JOIN rec_serving_policies p ON p.model_version=v.model_version '
                          'LEFT JOIN rec_policy_bases b ON b.base_version=v.base_version '
                          'WHERE p.model_version IS NULL OR b.base_version IS NULL LIMIT 1').fetchone()
    if orphan:
        raise MigrationError(f'Unresolved shared recommendation policy link: {orphan[0]!r}')
    bases = set()
    for version, raw, size, created, served in conn.execute(
            'SELECT base_version,configuration,byte_size,created_at,served FROM rec_policy_bases'):
        _metadata(version, created)
        _configuration(raw, version)
        if type(size) is not int or size != len(raw.encode()) or type(served) is not int or served not in (0, 1):
            raise MigrationError(f'Invalid shared recommendation policy metadata: {version!r}')
        bases.add(version)
    for version, raw, created in conn.execute('SELECT * FROM rec_serving_policies'):
        _metadata(version, created)
        value = _configuration(raw, version)
        base = value.get(BASE_FIELD)
        link = conn.execute('SELECT base_version FROM rec_policy_variants WHERE model_version=?', (version,)).fetchone()
        if ((base is not None and (not isinstance(base, str) or base not in bases))
                or (link and link[0] != base)):
            raise MigrationError(f'Invalid shared recommendation policy reference: {version!r}')
    for sql in statements():
        if not sql.startswith('CREATE TRIGGER recommendation_impression_'):
            conn.execute(sql)
    root = policy_documents.directory(conn)
    for old, new, columns in (
            ('rec_policy_bases', 'recommendation_policy_bodies',
             'base_version,configuration,created_at'),
            ('rec_serving_policies', 'recommendation_policies',
             'model_version,configuration,created_at')):
        # Keep only one small pack's metadata/payloads while relocating a large
        # installed history. The old rows remain the source until outer commit.
        pending, used = [], 0
        def copy_batch():
            values = (json.loads(row[1]) for row in pending)
            for (version, raw, created), (filename, digest, size, offset) in zip(
                    pending, policy_documents.publish_many(root, values)):
                if new == 'recommendation_policy_bodies':
                    served = conn.execute('SELECT served FROM rec_policy_bases WHERE base_version=?', (version,)).fetchone()[0]
                    conn.execute(f'INSERT INTO {new} VALUES (?,?,?,?,?,?,?)',
                                 (version, filename, digest, size, offset, created, served))
                else:
                    served = conn.execute('SELECT served FROM rec_policy_variants WHERE model_version=?', (version,)).fetchone()
                    conn.execute(f'INSERT INTO {new} VALUES (?,?,?,?,?,?,?,?)',
                                 (version, json.loads(raw).get(BASE_FIELD), filename, digest, size, offset,
                                  created, served[0] if served else 0))
        for row in conn.execute(f'SELECT {columns} FROM {old} ORDER BY 1'):
            # canonical encoding can expand the wrapper slightly; publish_many
            # independently enforces the hard record and pack byte bounds.
            size = len(row[1].encode())
            if pending and used + size > policy_documents.PACK_BYTES:
                copy_batch()
                pending, used = [], 0
            pending.append(tuple(row))
            used += size
        if pending:
            copy_batch()
    conn.execute(_link_sql())
    # served is quota accounting, derived from retained impressions rather than
    # an independent historical fact. Resolve both paths before recomputing it.
    conn.execute('UPDATE recommendation_policies SET served=EXISTS('
                 'SELECT 1 FROM recommendation_impression_policies WHERE policy_version=recommendation_policies.version)')
    conn.execute('UPDATE recommendation_policy_bodies SET served=EXISTS('
                 'SELECT 1 FROM recommendation_policies WHERE base_version=recommendation_policy_bodies.version AND served=1)')
    for sql in statements():
        if sql.startswith('CREATE TRIGGER recommendation_impression_'):
            conn.execute(sql)
    for table in ('rec_policy_variants', 'rec_serving_policies', 'rec_policy_bases'):
        conn.execute(f'DROP TABLE {table}')


def validate(conn, *, replaced=frozenset()):
    from .current import _tokens
    for sql in statements():
        name = sql.split()[2]
        if name in replaced:
            continue
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible recommendation policy structure: {name}')
    for name in REPLACED:
        if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone():
            raise MigrationError(f'Superseded recommendation policy owner remains: {name}')

