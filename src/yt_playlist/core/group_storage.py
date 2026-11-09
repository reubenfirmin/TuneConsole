"""Exact content reuse for compact groups; callers own the writer transaction."""
import hashlib

from . import group_content as content, group_documents, policy_documents
from .evidence_ledger import canonical


def _writer(conn):
    if not conn.in_transaction:
        raise ValueError('Musical group import requires an outer transaction')
    conn.execute('UPDATE settings SET value=value WHERE 0')


def _owner(key, resolve):
    recording_id = resolve(key)
    if recording_id is not None and (type(recording_id) is not int or recording_id < 1):
        raise ValueError('Invalid canonical recording in musical group inputs')
    return ('recording', recording_id) if recording_id is not None else ('unresolved', key)


def manifest_rows(conn, manifest_id):
    for recording_id, digest in conn.execute('SELECT m.recording_id,i.input_digest '
            'FROM group_manifest_inputs m JOIN group_recording_inputs i ON i.id=m.input_id '
            'WHERE m.manifest_id=? ORDER BY m.recording_id', (manifest_id,)):
        yield canonical([['recording', recording_id], digest])
    for raw_key, digest in conn.execute('SELECT raw_key,input_digest FROM unresolved_group_manifest_inputs '
                                       'WHERE manifest_id=? ORDER BY raw_key', (manifest_id,)):
        yield canonical([['unresolved', raw_key], digest])


def manifest_remainder_matches(conn, manifest_id, inputs):
    """Verify every surviving row of an unowned, partially collected manifest."""
    expected = iter(canonical([owner, value]) for owner, value in inputs)
    for retained in manifest_rows(conn, manifest_id):
        if not any(row == retained for row in expected):
            return False
    return True


def install_manifest(conn, inputs, resolve):
    _writer(conn)
    rows = normalized_inputs(inputs, resolve)
    digest = content.digest(canonical([owner, value]) for owner, value in rows)
    existing = conn.execute('SELECT id FROM group_input_manifests WHERE content_digest=?', (digest,)).fetchone()
    if existing is not None:
        if not conn.execute('SELECT complete FROM group_input_manifests WHERE id=?', (existing[0],)).fetchone()[0]:
            raise ValueError('Musical group input manifest is still being built')
        if not content.equal(manifest_rows(conn, existing[0]),
                             (canonical([owner, value]) for owner, value in rows)):
            raise ValueError('Musical group input digest collision')
        return existing[0]
    manifest_id = conn.execute('INSERT INTO group_input_manifests(content_digest) VALUES (?) RETURNING id',
                               (digest,)).fetchone()[0]
    insert_inputs(conn, manifest_id, rows)
    conn.execute('UPDATE group_input_manifests SET complete=1 WHERE id=?', (manifest_id,))
    return manifest_id


def normalized_inputs(inputs, resolve):
    content.input_manifest(inputs)
    normalized = {}
    for key, digest in inputs.items():
        owner = _owner(key, resolve)
        if owner in normalized:
            raise ValueError('Duplicate canonical musical group input owner')
        normalized[owner] = digest
    return sorted(normalized.items())


def insert_inputs(conn, manifest_id, rows):
    for (kind, owner), value in rows:
        if kind == 'recording':
            conn.execute('INSERT INTO group_recording_inputs(recording_id,input_digest) VALUES (?,?) '
                         'ON CONFLICT(recording_id,input_digest) DO NOTHING', (owner, value))
            input_id = conn.execute('SELECT id FROM group_recording_inputs WHERE recording_id=? '
                                    'AND input_digest=?', (owner, value)).fetchone()[0]
            conn.execute('INSERT INTO group_manifest_inputs VALUES (?,?,?)', (manifest_id, owner, input_id))
        else:
            conn.execute("INSERT INTO unresolved_group_manifest_inputs VALUES (?,?,?,'missing_recording')",
                         (manifest_id, owner, value))


def inputs_for(conn, manifest_id):
    values = {key: digest for key, digest in conn.execute('SELECT r.identity_key,i.input_digest '
        'FROM group_manifest_inputs m JOIN recordings r ON r.id=m.recording_id '
        'JOIN group_recording_inputs i ON i.id=m.input_id WHERE m.manifest_id=?', (manifest_id,))}
    values.update(conn.execute('SELECT raw_key,input_digest FROM unresolved_group_manifest_inputs '
                               'WHERE manifest_id=?', (manifest_id,)))
    return values


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
            'SELECT m.recording_id,m.similarity,m.support,i.input_digest FROM group_members m '
            'LEFT JOIN group_recording_inputs i ON i.id=m.input_id '
            'WHERE m.group_version_id=? ORDER BY m.recording_id', (version_id,))]
    unresolved = [tuple(value) for value in conn.execute(
            'SELECT raw_key,similarity,support,input_digest FROM unresolved_group_members '
            'WHERE group_version_id=? ORDER BY raw_key', (version_id,))]
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


def install_versions(conn, prepared, generation_id, resolve, *, unfinished_legacy=False):
    """Import one bounded generation, verifying semantic matches before reuse.

Background publication will stage the same rows through its leased batches;
this atomic importer is for migration and already bounded callers.
"""
    _writer(conn)
    pending = []
    for position, group in enumerate(prepared['groups']):
        conn.execute('INSERT INTO taste_groups(external_key,legacy_mode_id,created_at) VALUES (?,?,?) '
                     'ON CONFLICT(external_key) DO NOTHING',
                     (group['group_id'], group.get('legacy_mode_id'), prepared['at']))
    for position, group in enumerate(prepared['groups']):
        value = content.document(group['payload'], 'Musical group definition')
        fields = definition_fields(value, None if unfinished_legacy else group['members'])
        incomplete = bool(fields[2] and fields[4] != len(group['members']))
        if not fields[2] and group['members']:
            raise ValueError('Unknown musical group membership has asserted members')
        group_id = conn.execute('SELECT id FROM taste_groups WHERE external_key=?', (group['group_id'],)).fetchone()[0]
        digest = content.digest(content.prepared_rows(prepared, group, resolve))
        if incomplete:
            # An old interrupted build has no digest of its missing output.
            # Keep that exact prefix private under its original generation and
            # lease until cleanup. It cannot alias or become published content.
            digest = hashlib.sha256(canonical(['unfinished-legacy-group', prepared['generation'],
                                                group['version'], digest])).hexdigest()
        existing = conn.execute('SELECT id FROM group_versions WHERE group_id=? AND content_digest=?',
                                (group_id, digest)).fetchone()
        if existing is not None:
            if not content.equal(version_rows(conn, existing[0]), content.prepared_rows(prepared, group, resolve)):
                raise ValueError('Musical group version digest collision')
            _link(conn, generation_id, group_id, existing[0], position, group['version'])
        else:
            pending.append((position, group, group_id, digest, value, fields, incomplete))
    root = group_documents.directory(conn)
    for (position, group, group_id, digest, value, fields, incomplete), file in zip(
            pending, group_documents.publish_many(root, (('definition', row[4]) for row in pending))):
        version_id = create_version(conn, prepared, group, generation_id, group_id, digest, fields, file)
        insert_members(conn, version_id, group['members'], resolve,
                       content._prepared_document(prepared, 'payload', 'Musical group generation').get('recording_inputs', {}))
        insert_representatives(conn, version_id, enumerate(value.get('rep_keys', [])), resolve)
        if incomplete:
            conn.execute('INSERT INTO group_build_versions VALUES (?,?,?,?)',
                         (generation_id, version_id, position, group['version']))
        else:
            _link(conn, generation_id, group_id, version_id, position, group['version'])


def create_version(conn, prepared, group, generation_id, group_id, digest, fields, file):
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
    return conn.execute('INSERT INTO group_versions(group_id,parent_group_id,content_digest,policy_version,'
        'created_at,definition_filename,definition_digest,definition_bytes,definition_offset,'
        'leaf,legacy,membership_known,depth,size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id',
        (group_id, parent_id, digest, *generation, prepared['at'], *file, *fields)).fetchone()[0]


def insert_members(conn, version_id, rows, resolve, inputs):
    for key, similarity, support in rows:
        recording_id = resolve(key)
        digest = inputs.get(key)
        if recording_id is not None:
            row = conn.execute('SELECT id FROM group_recording_inputs WHERE recording_id=? AND input_digest=?',
                               (recording_id, digest)).fetchone() if digest is not None else None
            if digest is not None and row is None:
                raise ValueError('Musical group member input has not been installed')
            conn.execute('INSERT INTO group_members VALUES (?,?,?,?,?)',
                         (version_id, recording_id, content.score(similarity), content.score(support), row[0] if row else None))
        else:
            conn.execute("INSERT INTO unresolved_group_members VALUES (?,?,?,?,'missing_recording',?)",
                         (version_id, key, content.score(similarity), content.score(support), digest))


def insert_representatives(conn, version_id, rows, resolve):
    for index, key in rows:
        recording_id = resolve(key)
        conn.execute('INSERT INTO group_representatives VALUES (?,?,?,?)',
                     (version_id, index, recording_id, key if recording_id is None else None))


def _link(conn, generation_id, group_id, version_id, position, external_version):
    previous = conn.execute('SELECT group_version_id FROM group_version_aliases WHERE external_version=?',
                            (external_version,)).fetchone()
    if previous is not None and previous[0] != version_id:
        raise ValueError('External musical group version changed its immutable meaning')
    conn.execute('INSERT INTO group_version_aliases VALUES (?,?,?) ON CONFLICT(external_version) DO NOTHING',
                 (external_version, version_id, generation_id))
    conn.execute('INSERT INTO generation_groups VALUES (?,?,?,?,?)',
                 (generation_id, group_id, version_id, position, external_version))


def policy_version(raw):
    value = content.document(raw, 'Musical group policy')
    return 'group-policy:' + hashlib.sha256(canonical(value)).hexdigest()


def install_header(conn, prepared, policy, metadata, manifest_id):
    _writer(conn)
    version = policy_version(prepared['policy'])
    policy_documents.install(conn, [(version, canonical(policy).decode(), prepared['at'])])
    file = next(group_documents.publish_many(group_documents.directory(conn), [('generation', metadata)]))
    return install_header_reference(conn, prepared, version, file, manifest_id)


def install_header_reference(conn, prepared, version, file, manifest_id):
    _writer(conn)
    return conn.execute('INSERT INTO group_generations(external_key,policy_version,input_fingerprint,'
        'input_manifest_id,input_serial,created_at,metadata_filename,metadata_digest,metadata_bytes,metadata_offset) '
        'VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id',
        (prepared['generation'], version, prepared['fingerprint'], manifest_id, prepared['serial'],
         prepared['at'], *file)).fetchone()[0]
