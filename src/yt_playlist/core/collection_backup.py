"""Collection backup validation over supplied documents and ownership facts."""
from .collection_files import MAX_BYTES
from .collection_owners import OWNERS
from .evidence_ledger import canonical

FORMAT = 'collection-recovery-backup-1'


def digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def chunks(document):
    if (not isinstance(document, dict) or set(document) != {'format', 'type', 'chunks'}
            or document['format'] != FORMAT or document['type'] != 'manifest'
            or not isinstance(document['chunks'], list) or any(not digest(d) for d in document['chunks'])
            or len(set(document['chunks'])) != len(document['chunks'])):
        raise ValueError('Invalid collection backup manifest')
    return document['chunks']


def add_records(records, chunk):
    if (not isinstance(chunk, dict) or set(chunk) != {'format', 'type', 'records'}
            or chunk['format'] != FORMAT or chunk['type'] != 'chunk'
            or not isinstance(chunk['records'], list) or not 1 <= len(chunk['records']) <= 512):
        raise ValueError('Invalid collection backup chunk')
    for record in chunk['records']:
        if (not isinstance(record, dict) or set(record) != {'table', 'id', 'kind', 'digest', 'bytes', 'relative_filename'}
                or record['table'] not in OWNERS or record['kind'] != OWNERS[record['table']]
                or not isinstance(record['id'], (str, int)) or isinstance(record['id'], bool)
                or not digest(record['digest']) or type(record['bytes']) is not int
                or not 0 < record['bytes'] <= MAX_BYTES
                or record['relative_filename'] != record['digest'][:2] + '/' + record['digest'] + '.json'
                or (record['table'], record['id']) in records):
            raise ValueError('Invalid collection backup owner')
        records[record['table'], record['id']] = record


def validate_owners(records, retained):
    actual = {(table, identity): (kind, digest, size) for table, identity, kind, _, digest, size in retained}
    expected = {key: (r['kind'], r['digest'], r['bytes']) for key, r in records.items()}
    if actual != expected:
        raise ValueError('Collection backup does not match database owners')


def validate_identity(row, *, kind, owner, digest, size):
    if row is None or tuple(row) != (kind, str(owner), digest, size, 'ready'):
        raise ValueError('Collection backup has an inconsistent document identity')


def encode(value):
    encoded = canonical({'format': FORMAT, **value})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Collection backup manifest exceeds its byte budget')
    return encoded
