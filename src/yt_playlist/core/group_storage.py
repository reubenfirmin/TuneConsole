"""Version and membership writes for the group publisher."""
import hashlib

from . import group_content as content, group_documents, policy_documents
from .activity import WorkBudgetExceeded
from .evidence_ledger import canonical


def _writer(conn):
    if not conn.in_transaction:
        raise ValueError('Musical group import requires an outer transaction')
    conn.execute('UPDATE group_versions SET id=id WHERE 0')


def version_rows(conn, version_id):
    facts = version_facts(conn, version_id)
    row = facts['version']
    definition = group_documents.read(row['definition_filename'], row['definition_digest'],
                                       row['definition_bytes'], row['definition_offset'], 'definition')
    policy = policy_documents.configuration(conn, row['policy_version'])
    if policy is None:
        raise ValueError('Missing canonical musical group policy')
    yield canonical([content.FORMAT, row['external_key'], row['parent_key'], definition, policy])
    for recording_id, similarity, support, input_digest in facts['members']:
        yield canonical([['recording', recording_id], content.score(similarity), content.score(support), input_digest])
    for key, similarity, support, input_digest in facts['unresolved']:
        yield canonical([['unresolved', key], content.score(similarity), content.score(support), input_digest])


def version_facts(conn, version_id):
    row = conn.execute('SELECT v.*,g.external_key,p.external_key parent_key FROM group_versions v '
                       'JOIN taste_groups g ON g.id=v.group_id LEFT JOIN taste_groups p '
                       'ON p.id=v.parent_group_id WHERE v.id=?', (version_id,)).fetchone()
    if row is None:
        raise ValueError('Missing canonical musical group version')
    members = [tuple(value) for value in conn.execute(
            'SELECT m.recording_id,m.similarity,m.support,m.input_digest FROM group_members m '
            'WHERE m.group_version_id=? AND m.recording_id IS NOT NULL ORDER BY m.recording_id', (version_id,))]
    unresolved = [tuple(value) for value in conn.execute(
            'SELECT raw_key,similarity,support,input_digest FROM group_members '
            'WHERE group_version_id=? AND raw_key IS NOT NULL ORDER BY raw_key', (version_id,))]
    return {'version': dict(row), 'members': members, 'unresolved': unresolved}


def definition_fields(value, members=None):
    result = []
    for field in ('leaf', 'legacy', 'membership_known'):
        flag = value.get(field, False)
        if type(flag) is not bool:
            raise ValueError(f'Invalid musical group {field} flag')
        result.append(int(flag))
    if result[1] and result[2]:
        raise ValueError('Legacy musical group membership cannot be asserted as known')
    depth, size = value.get('depth'), value.get('size')
    if (depth is not None and (type(depth) is not int or depth < 0)
            or type(size) is not int or size < 0):
        raise ValueError('Invalid musical group depth or size')
    result.extend((depth, size))
    if members is not None and (result[2] and size != len(members) or not result[2] and members):
        raise ValueError('Musical group known membership does not match its declared size')
    reps = value.get('rep_keys', [])
    if not isinstance(reps, list) or any(not isinstance(key, str) for key in reps):
        raise ValueError('Invalid musical group representatives')
    return tuple(result)


def create_version(conn, prepared, group, generation_id, group_id, position, digest, fields, file):
    generation = conn.execute('SELECT policy_version FROM group_generations WHERE id=?',
                               (generation_id,)).fetchone()
    if generation is None:
        raise ValueError('Missing musical group generation')
    parent = group.get('parent_version')
    parent_key = next((item['group_id'] for item in prepared['groups'] if item['version'] == parent), None)
    if parent is not None and parent_key is None:
        raise ValueError('Musical group parent must belong to the same generation')
    parent_id = (conn.execute('SELECT id FROM taste_groups WHERE external_key=?', (parent_key,)).fetchone()[0]
                 if parent_key is not None else None)
    origin = conn.execute('SELECT v.group_id,v.content_digest,g.is_published FROM group_versions v '
                          'JOIN group_generations g ON g.id=v.generation_id '
                          'WHERE v.external_version=? AND v.is_origin=1', (group['version'],)).fetchone()
    if origin is not None and not origin[2]:
        raise WorkBudgetExceeded('Musical group version is leased by another unpublished build')
    if origin is not None and tuple(origin)[:2] != (group_id, digest):
        raise ValueError('External musical group version changed its immutable meaning')
    return conn.execute('INSERT INTO group_versions(group_id,parent_group_id,content_digest,policy_version,'
        'created_at,definition_filename,definition_digest,definition_bytes,definition_offset,'
        'leaf,legacy,membership_known,depth,size,generation_id,position,external_version,is_origin,complete) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) RETURNING id',
        (group_id, parent_id, digest, *generation, prepared['at'], *file, *fields,
         generation_id, position, group['version'], int(origin is None))).fetchone()[0]


def insert_members(conn, version_id, rows, resolve, inputs):
    for key, similarity, support in rows:
        recording_id = resolve(key)
        digest = inputs.get(key)
        if recording_id is not None:
            conn.execute('INSERT INTO group_members(group_version_id,recording_id,similarity,support,input_digest) '
                         'VALUES (?,?,?,?,?)',
                         (version_id, recording_id, content.score(similarity), content.score(support), digest))
        else:
            conn.execute('INSERT INTO group_members(group_version_id,raw_key,similarity,support,input_digest) '
                         'VALUES (?,?,?,?,?)',
                         (version_id, key, content.score(similarity), content.score(support), digest))


def policy_version(raw):
    value = content.document(raw, 'Musical group policy')
    return 'group-policy:' + hashlib.sha256(canonical(value)).hexdigest()


def install_header_reference(conn, prepared, version, file):
    _writer(conn)
    return conn.execute('INSERT INTO group_generations(external_key,policy_version,input_fingerprint,'
        'input_serial,created_at,metadata_filename,metadata_digest,metadata_bytes,metadata_offset) '
        'VALUES (?,?,?,?,?,?,?,?,?) RETURNING id',
        (prepared['generation'], version, prepared['fingerprint'], prepared['serial'],
         prepared['at'], *file)).fetchone()[0]
