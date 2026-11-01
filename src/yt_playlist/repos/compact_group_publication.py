"""Content reuse through the existing leased group publication protocol.

This is the v72 implementation of group_publication's stage/finish/cleanup
operations. The public repository switches to it only with the schema cutover.
"""
from contextlib import nullcontext
from itertools import islice
import time
import uuid

from yt_playlist.core import group_content as content, group_documents, group_storage
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from . import group_publication as publication

MAX_PLAN_BYTES = 64 * 1024 * 1024
STAGED_TABLES = frozenset({'taste_groups', 'group_generations', 'group_input_manifests', 'group_recording_inputs',
    'group_manifest_inputs', 'unresolved_group_manifest_inputs', 'group_versions', 'group_members',
    'unresolved_group_members', 'group_representatives', 'group_version_aliases', 'generation_groups',
    'group_builds', 'group_build_versions', 'group_lineage', 'group_generation_lineage',
    'group_unknown', 'unresolved_group_unknown', 'group_graph', 'unresolved_group_graph',
    'recommendation_policies', 'recommendation_policy_bodies'})


def _touch(conn, generation_id, owner):
    if conn.execute('UPDATE group_builds SET expires_at=? WHERE generation_id=? AND owner=? '
                    'AND ready=0 RETURNING generation_id',
                    (time.time() + publication.LEASE_SECONDS, generation_id, owner)).fetchone() is None:
        raise RuntimeError('Musical group build is no longer owned by this worker')


def _plan(repo, prepared, *, atomic=False):
    if atomic and not repo.conn.in_transaction:
        raise RuntimeError('Group publication requires an outer transaction')
    # Resolve only actual canonical identities. A guessed recording association
    # would change historical membership and is never a deduplication rule.
    policy = content.document(prepared['policy'], 'Musical group policy')
    metadata = content.document(prepared['payload'], 'Musical group generation')
    keys, used = set(metadata.get('recording_inputs', {})), len(prepared['payload'].encode())
    for group in prepared['groups']:
        used += len(group['payload'].encode()) + sum(len(key.encode()) + 32 for key, _a, _b in group['members'])
        if used > MAX_PLAN_BYTES:
            raise publication.WorkBudgetExceeded('Musical group publication exceeds its admitted memory')
        keys.update(key for key, _a, _b in group['members'])
        keys.update(content.document(group['payload'], 'Musical group definition').get('rep_keys', []))
    keys.update(key for key, _reason in prepared['unknown'])
    keys.update(key for left, right, _a, _b in prepared['edges'] for key in (left, right))
    ids = {}
    context = nullcontext(repo._store) if atomic else repo._store.read_snapshot()
    with context as view:
        conn = view.conn if atomic else view.store.conn
        for batch in publication._batches((key,) for key in sorted(keys)):
            placeholders = ','.join('?' for _row in batch)
            ids.update(conn.execute(f'SELECT identity_key,id FROM recordings WHERE identity_key IN ({placeholders})',
                                    [row[0] for row in batch]))
    inputs = group_storage.normalized_inputs(metadata.pop('recording_inputs', {}), ids.get)
    parents, groups = {}, set()
    for group in prepared['groups']:
        if group['version'] in parents or group['group_id'] in groups:
            raise ValueError('Duplicate musical group or external version in a generation')
        parents[group['version']] = group.get('parent_version')
        groups.add(group['group_id'])
    for version in parents:
        visited, parent = {version}, parents[version]
        while parent is not None:
            if parent in visited or parent not in parents:
                raise ValueError('Invalid musical group parent graph')
            visited.add(parent)
            parent = parents[parent]
    plans = []
    for position, group in enumerate(prepared['groups']):
        check_cancellable_work()
        value = content.document(group['payload'], 'Musical group definition')
        fields = group_storage.definition_fields(value, group['members'])
        digest = content.digest(content.prepared_rows(prepared, group, ids.get))
        plans.append((position, group, value, fields, digest))
    # The unchanged publication header covers all ordered output, including
    # caches and temporal lineage. It fences resumption after a code change.
    prepared.pop('_stage_digest', None)
    header = publication._header(prepared)
    metadata = content.document(header[-1], 'Musical group generation')
    metadata.pop('recording_inputs', None)
    metadata.pop('_staging_digest', None)
    metadata.pop('_staging_parent', None)
    return ids.get, inputs, plans, policy, metadata


def _claim(repo, prepared, inputs, policy, metadata):
    owner = prepared.setdefault('_stage_owner', uuid.uuid4().hex)
    policy_version = group_storage.policy_version(prepared['policy'])
    manifest_digest = content.digest(canonical([key, digest]) for key, digest in inputs)
    parent = prepared.get('_stage_parent')
    expected = (policy_version, prepared['fingerprint'], prepared['at'], prepared['serial'],
                prepared['_stage_digest'], parent)
    with publication._write(repo) as conn:
        row = conn.execute('SELECT g.* FROM group_generations g WHERE g.external_key=?',
            (prepared['generation'],)).fetchone()
        if row is not None:
            build = conn.execute('SELECT b.*,p.external_key parent_key FROM group_builds b LEFT JOIN '
                'group_generations p ON p.id=b.parent_generation_id WHERE b.generation_id=?', (row['id'],)).fetchone()
            if build is None:
                raise RuntimeError('Cannot resume a published musical group generation')
            if build['owner'] == publication._CLEANUP_OWNER:
                if build['expires_at'] > time.time():
                    raise publication.BuildUnavailable('Old musical group build is awaiting its lease expiry')
                raise publication._BuildBeingCleaned
            actual = (*[row[key] for key in ('policy_version', 'input_fingerprint', 'created_at', 'input_serial')],
                      build['output_digest'], build['parent_key'])
            if actual != expected:
                raise RuntimeError('Musical group resume plan does not match its retained output digest')
            if build['owner'] != owner and build['expires_at'] > time.time():
                raise publication.BuildUnavailable('Musical group build is leased by another worker')
            conn.execute('UPDATE group_builds SET owner=?,expires_at=? WHERE generation_id=?',
                         (owner, time.time() + publication.LEASE_SECONDS, row['id']))
            return row['id'], row['input_manifest_id'], owner, bool(build['ready'])
        manifest = conn.execute('SELECT id,complete FROM group_input_manifests WHERE content_digest=?',
                                (manifest_digest,)).fetchone()
        if manifest is None:
            manifest_id = conn.execute('INSERT INTO group_input_manifests(content_digest) VALUES (?) RETURNING id',
                                       (manifest_digest,)).fetchone()[0]
        else:
            manifest_id = manifest[0]
            if not manifest[1]:
                raise publication.BuildUnavailable('Musical group inputs are leased by another build')
            if not content.equal(group_storage.manifest_rows(conn, manifest_id),
                                 (canonical([key, digest]) for key, digest in inputs)):
                raise ValueError('Musical group input digest collision')
        parent_id = None
        if parent is not None:
            selected = conn.execute('SELECT g.id FROM group_generations g JOIN group_publications p '
                                    'ON p.generation_id=g.id WHERE g.external_key=?', (parent,)).fetchone()
            if selected is None:
                raise ValueError('Missing published musical group staging parent')
            parent_id = selected[0]
        generation_id = group_storage.install_header(conn, prepared, policy, metadata, manifest_id)
        conn.execute('INSERT INTO group_builds VALUES (?,?,?,0,?,?)',
                     (generation_id, owner, time.time() + publication.LEASE_SECONDS, prepared['_stage_digest'], parent_id))
        return generation_id, manifest_id, owner, False


def _count(conn, generation_id, tables, key='generation_id'):
    return sum(conn.execute(f'SELECT count(*) FROM {table} WHERE {key}=?',
                            (generation_id,)).fetchone()[0] for table in tables)


def _inputs(repo, generation_id, manifest_id, owner, rows):
    with repo._store.read_snapshot() as snapshot:
        conn = snapshot.store.conn
        if conn.execute('SELECT complete FROM group_input_manifests WHERE id=?', (manifest_id,)).fetchone()[0]:
            return
        count = _count(conn, manifest_id, ('group_manifest_inputs', 'unresolved_group_manifest_inputs'), 'manifest_id')
    if count > len(rows):
        raise RuntimeError('Musical group input prefix exceeds its retained plan')
    for batch in publication._batches((kind, key, digest) for (kind, key), digest in islice(rows, count, None)):
        with publication._write(repo) as conn:
            _touch(conn, generation_id, owner)
            group_storage.insert_inputs(conn, manifest_id, (((kind, key), digest) for kind, key, digest in batch))
    with publication._write(repo) as conn:
        _touch(conn, generation_id, owner)
        if not content.equal(group_storage.manifest_rows(conn, manifest_id),
                             (canonical([key, digest]) for key, digest in rows)):
            raise ValueError('Musical group staged inputs differ from their retained plan')
        conn.execute('UPDATE group_input_manifests SET complete=1 WHERE id=?', (manifest_id,))


def _identities(repo, prepared, generation_id, owner):
    rows = ((g['group_id'], g.get('legacy_mode_id'), prepared['at']) for g in prepared['groups'])
    for batch in publication._batches(rows):
        with publication._write(repo) as conn:
            _touch(conn, generation_id, owner)
            for row in batch:
                conn.execute('INSERT INTO taste_groups(external_key,legacy_mode_id,created_at) VALUES (?,?,?) '
                             'ON CONFLICT(external_key) DO NOTHING', row)
                if conn.execute('SELECT legacy_mode_id FROM taste_groups WHERE external_key=?',
                                (row[0],)).fetchone()[0] != row[1]:
                    raise ValueError('Musical group identity changed its legacy owner')


def _link(conn, generation_id, group_id, version_id, position, alias):
    previous = conn.execute('SELECT a.first_generation_id,p.generation_id FROM group_version_aliases a '
        'LEFT JOIN group_publications p ON p.generation_id=a.first_generation_id WHERE a.external_version=?',
        (alias,)).fetchone()
    if previous is not None and previous[0] != generation_id and previous[1] is None:
        raise publication.BuildUnavailable('External musical group version is owned by an unpublished build')
    group_storage._link(conn, generation_id, group_id, version_id, position, alias)


def _clear_foreign_conflicts(repo, prepared, plans):
    """Retire only expired unpublished owners of this plan's aliases/content."""
    conflicts = {}
    with repo._store.read_snapshot() as snapshot:
        conn = snapshot.store.conn
        for _position, group, _value, _fields, digest in plans:
            rows = conn.execute('SELECT g.external_key,b.expires_at FROM group_generations g JOIN ('
                'SELECT first_generation_id generation_id FROM group_version_aliases WHERE external_version=? '
                'UNION SELECT x.generation_id FROM group_build_versions x '
                'JOIN group_versions v ON v.id=x.group_version_id JOIN taste_groups t ON t.id=v.group_id '
                'WHERE t.external_key=? AND v.content_digest=?) blocked ON blocked.generation_id=g.id '
                'LEFT JOIN group_builds b ON b.generation_id=g.id WHERE g.external_key<>? '
                'AND NOT EXISTS(SELECT 1 FROM group_publications p WHERE p.generation_id=g.id)',
                (group['version'], group['group_id'], digest, prepared['generation']))
            conflicts.update(rows)
    # Check every lease before deleting any prefix. A live build is never
    # fenced merely because another plan wants the same stable external alias.
    if any(expiry is None or expiry >= time.time() for expiry in conflicts.values()):
        raise publication.BuildUnavailable('Musical group version is leased by another unpublished build')
    for generation in sorted(conflicts):
        while cleanup(repo, generation=generation):
            check_cancellable_work()
        with repo._store.read_snapshot() as snapshot:
            if snapshot.store.conn.execute('SELECT 1 FROM group_generations WHERE external_key=?',
                                            (generation,)).fetchone() is not None:
                raise publication.BuildUnavailable('Musical group version still belongs to another build')


def _group(repo, prepared, generation_id, owner, plan, resolve):
    position, group, value, fields, digest = plan
    with publication._write(repo) as conn:
        _touch(conn, generation_id, owner)
        group_id = conn.execute('SELECT id FROM taste_groups WHERE external_key=?', (group['group_id'],)).fetchone()[0]
        selected = conn.execute('SELECT external_version FROM generation_groups WHERE generation_id=? AND group_id=?',
                                (generation_id, group_id)).fetchone()
        if selected is not None:
            if selected[0] != group['version']:
                raise ValueError('Musical group staged selection changed')
            return
        version = conn.execute('SELECT id FROM group_versions WHERE group_id=? AND content_digest=?',
                               (group_id, digest)).fetchone()
        if version is None:
            raise RuntimeError('Musical group definition prefix is missing')
        version_id = version[0]
        pending = conn.execute('SELECT generation_id,position,external_version FROM group_build_versions '
                               'WHERE group_version_id=?', (version_id,)).fetchone()
        if pending is not None and tuple(pending) != (generation_id, position, group['version']):
            raise publication.BuildUnavailable('Musical group content is leased by another build')
        sealed = conn.execute('SELECT 1 FROM group_version_aliases WHERE group_version_id=? LIMIT 1',
                              (version_id,)).fetchone() is not None
        if pending is None and not sealed:
            raise publication.BuildUnavailable('Unfinished musical group content is awaiting cleanup')
    if not sealed:
        with repo._store.read_snapshot() as snapshot:
            conn = snapshot.store.conn
            count = _count(conn, version_id, ('group_members', 'unresolved_group_members'), 'group_version_id')
            reps = _count(conn, version_id, ('group_representatives',), 'group_version_id')
        if count > len(group['members']) or reps > len(value.get('rep_keys', [])):
            raise RuntimeError('Musical group content prefix exceeds its retained plan')
        for batch in publication._batches(islice(group['members'], count, None)):
            with publication._write(repo) as conn:
                _touch(conn, generation_id, owner)
                group_storage.insert_members(conn, version_id, batch, resolve,
                    content._prepared_document(prepared, 'payload', 'Musical group generation').get('recording_inputs', {}))
        for batch in publication._batches(enumerate(value.get('rep_keys', [])[reps:], reps)):
            with publication._write(repo) as conn:
                _touch(conn, generation_id, owner)
                group_storage.insert_representatives(conn, version_id, batch, resolve)
    with repo._store.read_snapshot() as snapshot:
        if not content.equal(group_storage.version_rows(snapshot.store.conn, version_id),
                             content.prepared_rows(prepared, group, resolve)):
            raise ValueError('Musical group version digest collision or incomplete staged content')
    with publication._write(repo) as conn:
        _touch(conn, generation_id, owner)
        _link(conn, generation_id, group_id, version_id, position, group['version'])
        conn.execute('DELETE FROM group_build_versions WHERE generation_id=? AND group_version_id=?',
                     (generation_id, version_id))


def _groups(repo, prepared, generation_id, owner, plans, resolve):
    # Pack definition output by bounded bytes, including only new content. File
    # publication precedes SQL references; interrupted batches leave safe orphans.
    positions = {row[0]: row for row in plans}
    for batch in publication._batches((row[0], row[1]['group_id'], row[4], row[1]['payload']) for row in plans):
        with publication._write(repo) as conn:
            _touch(conn, generation_id, owner)
            new = []
            for position, key, digest, _raw in batch:
                group_id = conn.execute('SELECT id FROM taste_groups WHERE external_key=?', (key,)).fetchone()[0]
                if conn.execute('SELECT 1 FROM group_versions WHERE group_id=? AND content_digest=?',
                                (group_id, digest)).fetchone() is None:
                    new.append((position, group_id))
            root = group_documents.directory(conn)
            files = group_documents.publish_many(root, (('definition', positions[p][2]) for p, _group_id in new))
            for (position, group_id), file in zip(new, files):
                _position, group, _value, fields, digest = positions[position]
                version_id = group_storage.create_version(conn, prepared, group, generation_id, group_id, digest, fields, file)
                conn.execute('INSERT INTO group_build_versions VALUES (?,?,?,?)',
                             (generation_id, version_id, position, group['version']))
        for position, _key, _digest, _raw in batch:
            _group(repo, prepared, generation_id, owner, positions[position], resolve)


def _lineage_value(raw):
    value = content.document(raw, 'Musical group lineage')
    for field in ('old_share', 'new_share'):
        if value.get(field) is not None:
            content.score(value[field])
    if value.get('compatible') is not None and type(value['compatible']) is not bool:
        raise ValueError('Invalid musical group lineage compatibility')
    if value.get('overlap') is not None and (type(value['overlap']) is not int or value['overlap'] < 0):
        raise ValueError('Invalid musical group lineage overlap')
    return value


def _lineage(repo, prepared, generation_id, owner):
    with repo._store.read_snapshot() as snapshot:
        count = _count(snapshot.store.conn, generation_id, ('group_generation_lineage',))
    if count > len(prepared['lineage']):
        raise RuntimeError('Musical group lineage prefix exceeds its retained plan')
    for batch in publication._batches(islice(prepared['lineage'], count, None)):
        with publication._write(repo) as conn:
            _touch(conn, generation_id, owner)
            insert_lineage(conn, generation_id, batch)


def insert_lineage(conn, generation_id, rows):
    pending = []
    for source, target, relation, raw in rows:
        value = _lineage_value(raw)
        existing = conn.execute('SELECT * FROM group_lineage WHERE source_alias=? AND target_alias=?',
                                (source, target)).fetchone()
        if existing is None:
            pending.append((source, target, relation, value))
        else:
            if (existing['relation'] != relation or group_documents.read(existing['evidence_filename'],
                    existing['evidence_digest'], existing['evidence_bytes'], existing['evidence_offset'], 'lineage') != value):
                raise ValueError('Temporal musical group evidence changed')
            conn.execute('INSERT INTO group_generation_lineage VALUES (?,?,?)', (generation_id, source, target))
    files = group_documents.publish_many(group_documents.directory(conn), (('lineage', row[3]) for row in pending))
    for (source, target, relation, value), file in zip(pending, files):
        source_row = conn.execute('SELECT group_version_id FROM group_version_aliases WHERE external_version=?',
                                  (source,)).fetchone()
        target_row = conn.execute('SELECT group_version_id FROM group_version_aliases WHERE external_version=?',
                                  (target,)).fetchone()
        if source_row is None or target_row is None:
            raise ValueError('Missing temporal musical group version')
        compatible = int(value['compatible']) if value.get('compatible') is not None else None
        conn.execute('INSERT INTO group_lineage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (source, target, source_row[0], target_row[0], relation, compatible,
             value.get('old_share'), value.get('new_share'), value.get('overlap'), *file))
        conn.execute('INSERT INTO group_generation_lineage VALUES (?,?,?)', (generation_id, source, target))


def _caches(repo, prepared, generation_id, owner, resolve):
    operations = [(('group_unknown', 'unresolved_group_unknown'), prepared['unknown']),
                  (('group_graph', 'unresolved_group_graph'), prepared['edges'])]
    for tables, rows in operations:
        with repo._store.read_snapshot() as snapshot:
            count = _count(snapshot.store.conn, generation_id, tables)
        if count > len(rows):
            raise RuntimeError('Musical group cache prefix exceeds its retained plan')
        for batch in publication._batches(islice(rows, count, None)):
            with publication._write(repo) as conn:
                _touch(conn, generation_id, owner)
                insert_cache(conn, generation_id, tables[0], batch, resolve)


def insert_cache(conn, generation_id, table, rows, resolve):
    for row in rows:
        if table == 'group_unknown':
            key, reason = row
            recording_id = resolve(key)
            if recording_id is None:
                conn.execute("INSERT INTO unresolved_group_unknown VALUES (?,?,?,'missing_recording')",
                             (generation_id, key, reason))
            else:
                conn.execute('INSERT INTO group_unknown VALUES (?,?,?)', (generation_id, recording_id, reason))
        elif table == 'group_graph':
            left, right, weight, support = row
            left_id, right_id = resolve(left), resolve(right)
            weight, support = content.score(weight), content.score(support)
            if left_id is None or right_id is None:
                conn.execute("INSERT INTO unresolved_group_graph VALUES (?,?,?,?,?,'missing_recording')",
                             (generation_id, left, right, weight, support))
            else:
                conn.execute('INSERT INTO group_graph VALUES (?,?,?,?,?)',
                             (generation_id, left_id, right_id, weight, support))
        else:
            raise ValueError('Unknown musical group cache owner')


def stage(repo, prepared):
    resolve, inputs, plans, policy, metadata = _plan(repo, prepared)
    _clear_foreign_conflicts(repo, prepared, plans)
    while True:
        try:
            generation_id, manifest_id, owner, ready = _claim(repo, prepared, inputs, policy, metadata)
            break
        except publication._BuildBeingCleaned:
            while cleanup(repo, generation=prepared['generation']):
                check_cancellable_work()
    if ready:
        return owner
    _inputs(repo, generation_id, manifest_id, owner, inputs)
    _identities(repo, prepared, generation_id, owner)
    _groups(repo, prepared, generation_id, owner, plans, resolve)
    _lineage(repo, prepared, generation_id, owner)
    _caches(repo, prepared, generation_id, owner, resolve)
    with publication._write(repo) as conn:
        _touch(conn, generation_id, owner)
        if _count(conn, generation_id, ('generation_groups',)) != len(plans):
            raise RuntimeError('Musical group staged selection is incomplete')
        conn.execute('UPDATE group_builds SET ready=1 WHERE generation_id=?', (generation_id,))
    return owner


def finish(conn, generation, owner=None):
    if not conn.in_transaction:
        raise RuntimeError('Group publication requires an outer transaction')
    row = conn.execute('SELECT id FROM group_generations WHERE external_key=?', (generation,)).fetchone()
    if row is None:
        raise RuntimeError('Missing staged musical group generation')
    generation_id = row[0]
    if owner is not None and conn.execute('SELECT 1 FROM group_builds WHERE generation_id=? AND owner=? AND ready=1',
                                         (generation_id, owner)).fetchone() is None:
        raise RuntimeError('Musical group build is incomplete or no longer owned')
    conn.execute('INSERT INTO group_publications VALUES (?,?)', (generation_id, time.time()))
    conn.execute('INSERT INTO group_head VALUES (1,?) ON CONFLICT(id) DO UPDATE SET generation_id=excluded.generation_id',
                 (generation_id,))
    conn.execute('DELETE FROM group_builds WHERE generation_id=?', (generation_id,))


def publish(repo, prepared):
    """Atomic import for already bounded callers; preparation uses leased stage."""
    resolve, inputs, _plans, policy, metadata = _plan(repo, prepared, atomic=True)
    conn = repo.conn
    manifest_id = group_storage.install_manifest(conn, dict(content.input_manifest(
        content.document(prepared['payload'], 'Musical group generation').get('recording_inputs', {}))), resolve)
    parent = prepared.get('_stage_parent')
    parent_row = conn.execute('SELECT g.id FROM group_generations g JOIN group_publications p '
                             'ON p.generation_id=g.id WHERE g.external_key=?', (parent,)).fetchone()
    if parent is not None and parent_row is None:
        raise ValueError('Missing published musical group staging parent')
    generation_id = group_storage.install_header(conn, prepared, policy, metadata, manifest_id)
    group_storage.install_versions(conn, prepared, generation_id, resolve)
    for batch in publication._batches(prepared['lineage']):
        insert_lineage(conn, generation_id, batch)
    for table, rows in (('group_unknown', prepared['unknown']), ('group_graph', prepared['edges'])):
        for batch in publication._batches(rows):
            insert_cache(conn, generation_id, table, batch, resolve)
    finish(conn, prepared['generation'])


def abandon(repo, prepared):
    if (owner := prepared.get('_stage_owner')) is not None:
        with publication._write(repo) as conn:
            conn.execute('UPDATE group_builds SET expires_at=0 WHERE owner=? AND generation_id='
                         '(SELECT id FROM group_generations WHERE external_key=?)', (owner, prepared['generation']))


def _delete(conn, table, key, predicate, parameters=()):
    selected = ','.join(key)
    return conn.execute(f'DELETE FROM {table} WHERE ({selected}) IN (SELECT {selected} FROM {table} '
                        f'WHERE {predicate} LIMIT ?)', (*parameters, publication.BATCH_ROWS)).rowcount


def _orphans(conn):
    version = conn.execute('SELECT id FROM group_versions v WHERE NOT EXISTS('
        'SELECT 1 FROM group_version_aliases a WHERE a.group_version_id=v.id) AND NOT EXISTS('
        'SELECT 1 FROM group_build_versions b WHERE b.group_version_id=v.id) LIMIT 1').fetchone()
    if version is not None:
        for table, key in (('group_members', ('group_version_id', 'recording_id')),
                           ('unresolved_group_members', ('group_version_id', 'raw_key')),
                           ('group_representatives', ('group_version_id', 'position'))):
            if _delete(conn, table, key, 'group_version_id=?', (version[0],)):
                return True
        conn.execute('DELETE FROM group_versions WHERE id=?', (version[0],))
        return True
    manifest = conn.execute('SELECT id FROM group_input_manifests i WHERE NOT EXISTS('
        'SELECT 1 FROM group_generations g WHERE g.input_manifest_id=i.id) LIMIT 1').fetchone()
    if manifest is not None:
        for table, key in (('group_manifest_inputs', ('manifest_id', 'recording_id')),
                           ('unresolved_group_manifest_inputs', ('manifest_id', 'raw_key'))):
            if _delete(conn, table, key, 'manifest_id=?', (manifest[0],)):
                return True
        conn.execute('DELETE FROM group_input_manifests WHERE id=?', (manifest[0],))
        return True
    if _delete(conn, 'group_recording_inputs', ('id',), 'NOT EXISTS(SELECT 1 FROM group_manifest_inputs m '
               'WHERE m.input_id=group_recording_inputs.id) AND NOT EXISTS('
               'SELECT 1 FROM group_members m WHERE m.input_id=group_recording_inputs.id)'):
        return True
    return False


def _lineage_orphans(repo):
    conn = repo.conn
    revisions = getattr(repo._store, '_snapshot_revisions', None)
    signature = None
    if revisions is not None:
        signature = (conn.execute('PRAGMA data_version').fetchone()[0],
                     conn.execute('PRAGMA schema_version').fetchone()[0],
                     revisions.capture({('group_lineage', ''), ('group_generation_lineage', '')}))
        if signature == getattr(repo._store, '_group_lineage_orphans_clear', None):
            return False
    removed = _delete(conn, 'group_lineage', ('source_alias', 'target_alias'), 'NOT EXISTS('
        'SELECT 1 FROM group_generation_lineage x WHERE x.source_alias=group_lineage.source_alias '
        'AND x.target_alias=group_lineage.target_alias)')
    # A completed empty check is reusable across membership/input prefixes.
    # Transactional counters invalidate it on rollback or any local edge write;
    # data_version also invalidates it for writers on other connections.
    if not removed and signature is not None:
        repo._store._group_lineage_orphans_clear = signature
    return bool(removed)


def cleanup(repo, *, before=None, generation=None):
    """Fence an expired worker, then remove one bounded unpublished prefix."""
    with publication._write(repo) as conn:
        row = conn.execute('SELECT g.id FROM group_builds b JOIN group_generations g ON g.id=b.generation_id '
            'WHERE b.expires_at<? AND NOT EXISTS(SELECT 1 FROM group_publications p WHERE p.generation_id=g.id) '
            + ('AND g.external_key=? ' if generation is not None else '') + 'ORDER BY b.expires_at LIMIT 1',
            (time.time() if before is None else before,) + ((generation,) if generation is not None else ())).fetchone()
        if row is not None:
            generation_id = row[0]
            conn.execute('UPDATE group_builds SET owner=?,ready=0,expires_at=0 WHERE generation_id=?',
                         (publication._CLEANUP_OWNER, generation_id))
            for table, key in (('group_graph', ('generation_id', 'left_recording_id', 'right_recording_id')),
                               ('unresolved_group_graph', ('generation_id', 'left_key', 'right_key')),
                               ('group_assignments', ('generation_id', 'recording_id')),
                               ('unresolved_group_assignments', ('generation_id', 'raw_key')),
                               ('group_unknown', ('generation_id', 'recording_id')),
                               ('unresolved_group_unknown', ('generation_id', 'raw_key')),
                               ('group_generation_lineage', ('generation_id', 'source_alias', 'target_alias'))):
                if _delete(conn, table, key, 'generation_id=?', (generation_id,)):
                    return True
            if _lineage_orphans(repo):
                return True
            if _delete(conn, 'generation_groups', ('generation_id', 'group_id'), 'generation_id=?', (generation_id,)):
                return True
            if _delete(conn, 'group_version_aliases', ('external_version',), 'first_generation_id=? AND NOT EXISTS('
                    'SELECT 1 FROM generation_groups x WHERE x.external_version=group_version_aliases.external_version)',
                    (generation_id,)):
                return True
            if _delete(conn, 'group_build_versions', ('generation_id', 'position'), 'generation_id=?', (generation_id,)):
                return True
            if _orphans(conn):
                return True
            conn.execute('DELETE FROM group_builds WHERE generation_id=?', (generation_id,))
            conn.execute('DELETE FROM group_generations WHERE id=?', (generation_id,))
            return True
        return _orphans(conn)
