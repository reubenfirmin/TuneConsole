"""Owned paired menus and captured Radio modes; ranked universes use #486."""
from dataclasses import dataclass
import hashlib
import json
import math
import struct
import time
import uuid

from . import home_outputs, recommendation_outputs as files, group_evidence, policy_documents
from .evidence_ledger import canonical

CONTEXTS = ('context', 'choice_context', 'album_choice_context')
PART_CHARACTERS = 64 * 1024


def address(surface):
    parts = surface.split(':')
    if (len(parts) != 4 or parts[0] != 'model_pair' or not parts[1].isascii()
            or not parts[1].isdigit() or str(int(parts[1])) != parts[1] or int(parts[1]) < 1
            or parts[2] not in ('incumbent', 'challenger') or parts[3] not in ('mixes', 'albums', 'radio')):
        raise ValueError('Invalid comparison projection address')
    return int(parts[1]), parts[2], parts[3]


@dataclass(frozen=True)
class Prepared:
    surface: str
    value: dict
    raw: str
    digest: str
    parts: tuple
    at: float | None
    modes: tuple
    plan: object | None
    policies: frozenset


def prepare(conn, surface, envelope, at, *, pending_policies=(), input_revision=None, historical=False, capture_only=False):
    from yt_playlist.rec import bundle_storage, comparison_storage
    run, arm, format = address(surface)
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
        raise ValueError('Invalid comparison observation time')
    if not isinstance(envelope, dict) or envelope.get('run_id') != run:
        raise ValueError('Comparison envelope does not match its owner')
    value = comparison_storage.decode(envelope)
    if not isinstance(value, dict):
        raise ValueError('Comparison output must be an object')
    value = dict(value)
    bundle = value.pop('bundles', None) if format == 'radio' else None
    root = value.pop('policy_version', None)
    policies = home_outputs.policy_references(value)
    if root is not None:
        if not isinstance(root, str) or not root:
            raise ValueError('Invalid comparison generation policy')
        policies.add(root)
    modes = []
    if format == 'radio' and 'modes' in value:
        source = value['modes']
        if not isinstance(source, list) or len(source) > 512:
            raise ValueError('Comparison mode budget exceeded')
        display = []
        for mode in source:
            if (not isinstance(mode, dict) or type(mode.get('mode_id')) is not int or mode['mode_id'] < 1
                    or not isinstance(mode.get('space'), str) or not isinstance(mode.get('centroid'), list)
                    or not 1 <= len(mode['centroid']) <= 4096
                    or any(type(number) not in (int, float) or not math.isfinite(number) for number in mode['centroid'])):
                raise ValueError('Invalid captured Radio mode')
            modes.append((mode['mode_id'], mode['space'], struct.pack('<' + 'd' * len(mode['centroid']), *mode['centroid'])))
            display.append({key: entry for key, entry in mode.items() if key not in ('mode_id', 'space', 'centroid')})
        value['modes'] = display
    if len({mode[0] for mode in modes}) != len(modes):
        raise ValueError('Duplicate captured Radio mode')
    plan = None
    if bundle is not None:
        # Declared candidate policy paths are inspected once at import. No
        # arbitrary JSON scan or synthesized policy substitutes for provenance.
        versions = set()
        for key, bucket in bundle.items():
            if not key.startswith('_') and isinstance(bucket, dict):
                for pool in bucket.values():
                    if isinstance(pool, list):
                        versions.update(item['pool_policy'] for item in pool
                            if isinstance(item, dict) and item.get('pool_policy'))
        if root is None and versions:
            root = sorted(versions)[0]
        if root is None:
            if bundle:
                raise ValueError('Historical Radio candidates have no recorded policy')
        else:
            plan = bundle_storage.prepare(conn, bundle, None, external_key='comparison:' + uuid.uuid4().hex,
                policy_version=root, input_revision=input_revision,
                created_at=at if capture_only or at is not None else time.time(),
                pending_policies=pending_policies, surface_prefix=surface + ':', allow_unobserved=capture_only)
            policies.add(root)
    raw = canonical(value).decode()
    if not 0 < len(raw.encode()) <= 8 * 1024 * 1024:
        raise ValueError('Comparison display exceeds its byte budget')
    parts = tuple(files.encode(surface, str(index), raw[start:start + PART_CHARACTERS])
                  for index, start in enumerate(range(0, len(raw), PART_CHARACTERS)))
    if len(parts) > 128:
        raise ValueError('Comparison display exceeds its part budget')
    return Prepared(surface, value, raw, hashlib.sha256(raw.encode()).hexdigest(), parts, at,
                    tuple(modes), plan, frozenset(policies))


def _root(identity, field):
    return f'comparison-output:{identity}:{field}'


def retire(conn, identity):
    from yt_playlist.repos import recommendation_publication
    conn.execute('DELETE FROM recommendation_heads WHERE generation_id IN '
                 '(SELECT generation_id FROM model_pair_publications WHERE id=?)', (identity,))
    conn.execute("UPDATE model_pair_publications SET status='retiring' WHERE id=?", (identity,))
    conn.execute('DELETE FROM model_pair_group_roots WHERE publication_id=?', (identity,))
    conn.execute('DELETE FROM model_pair_recommendation_roots WHERE publication_id=?', (identity,))
    for field in (*CONTEXTS, 'recommendations', 'pool'):
        group_evidence.release(conn, 'comparison', _root(identity, field))
        recommendation_publication.release(conn, 'comparison', _root(identity, field))
    for table in ('model_pair_files', 'model_pair_contexts', 'model_pair_modes', 'model_pair_policies'):
        conn.execute(f'DELETE FROM {table} WHERE publication_id=?', (identity,))
    conn.execute('DELETE FROM model_pair_publications WHERE id=?', (identity,))


def publish(conn, prepared, *, store=None, generation=None, historical=False):
    from yt_playlist.repos import recommendation_publication
    if not conn.in_transaction or not isinstance(prepared, Prepared):
        raise ValueError('Comparison publication requires prepared output and its outer transaction')
    run, arm, format = address(prepared.surface)
    if prepared.plan is not None and generation is None:
        generation = recommendation_publication.import_plan(conn, prepared.plan, prepared.plan.created_at)
    known = conn.execute('SELECT id FROM rec_model_selection_runs WHERE id=?', (run,)).fetchone()
    identity = conn.execute('INSERT INTO model_pair_publications(run_id,run_ref,run_resolution,arm,format,'
        'generation_id,encoding_model,built_at,output_digest,part_count) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id',
        (run, run if known else None, None if known else 'missing_run', arm, format, generation,
         prepared.value.get('encoding_model'), prepared.at, prepared.digest, len(prepared.parts))).fetchone()[0]
    if generation is not None:
        expected = prepared.surface + ':radio'
        if not conn.execute('SELECT 1 FROM recommendation_generation_surfaces WHERE generation_id=? AND surface=?',
                            (generation, expected)).fetchone():
            # Cold fallback may contain only Home slots.
            if not conn.execute('SELECT 1 FROM recommendation_generation_surfaces WHERE generation_id=? AND surface=?',
                                (generation, prepared.surface + ':home')).fetchone():
                raise ValueError('Comparison does not own its ranked generation')
        recommendation_publication.retain(conn, 'comparison', _root(identity, 'pool'), generation)
    for version in sorted(prepared.policies):
        known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (version,)).fetchone()
        if not known and not historical:
            raise ValueError('Comparison references an unknown policy')
        conn.execute('INSERT INTO model_pair_policies VALUES (?,?,?,?)',
            (identity, version, version if known else None, None if known else 'missing_policy'))
    for index, (mode, space, centroid) in enumerate(prepared.modes):
        known = conn.execute('SELECT 1 FROM taste_modes WHERE mode_id=?', (mode,)).fetchone()
        conn.execute('INSERT INTO model_pair_modes VALUES (?,?,?,?,?,?,?)',
            (identity, index, mode, mode if known else None, None if known else 'missing_mode', space, centroid))
    for field in CONTEXTS:
        context = prepared.value.get(field)
        if isinstance(context, dict) and context.get('artifact'):
            conn.execute('INSERT INTO model_pair_contexts VALUES (?,?,?)', (identity, field, context['artifact']))
    if store is not None:
        from .prepared_references import replace_prepared
        replace_prepared(store, prepared.surface, prepared.value, prepared.at,
                         historical=historical, owner_prefix=f'comparison-output:{identity}')
    else:
        # Registered earlier versions resolved these exact legacy references.
        # Copy them verbatim rather than guessing an artifact's current contents.
        for field in (*CONTEXTS, 'recommendations'):
            old, new = 'proposal:' + prepared.surface + ':' + field, _root(identity, field)
            conn.execute("INSERT INTO group_evidence_owners SELECT kind,?,reference_digest,created_at FROM group_evidence_owners "
                         "WHERE kind='comparison' AND external_key=?", (new, old))
            for table in ('group_evidence_generations', 'group_evidence_versions', 'unresolved_group_evidence', 'unresolved_group_contexts'):
                columns = ','.join(row[1] for row in conn.execute(f'PRAGMA table_info({table})') if row[1] not in ('kind', 'external_key'))
                conn.execute(f'INSERT INTO {table}(kind,external_key,{columns}) SELECT kind,?,{columns} FROM {table} '
                             "WHERE kind='comparison' AND external_key=?", (new, old))
            conn.execute("INSERT INTO recommendation_generation_refs SELECT kind,?,generation_id FROM recommendation_generation_refs "
                         "WHERE kind='comparison' AND external_key=?", (new, old))
    for field in (*CONTEXTS, 'recommendations', 'pool'):
        key = _root(identity, field)
        conn.execute("INSERT INTO model_pair_group_roots SELECT ?,?,'comparison',external_key FROM group_evidence_owners "
                     "WHERE kind='comparison' AND external_key=?", (identity, field, key))
        conn.execute("INSERT INTO model_pair_recommendation_roots SELECT ?,?,'comparison',external_key,generation_id "
                     "FROM recommendation_generation_refs WHERE kind='comparison' AND external_key=?", (identity, field, key))
    with policy_documents.retained_reader(files.directory(conn)):
        for index, encoded in enumerate(prepared.parts):
            filename, digest, size = files.publish(files.directory(conn), encoded)
            conn.execute('INSERT INTO model_pair_files(publication_id,surface,chunk_key,filename,content_digest,byte_size) '
                         'VALUES (?,?,?,?,?,?)', (identity, prepared.surface, str(index), filename, digest, size))
    previous = conn.execute('SELECT publication_id FROM model_pair_heads WHERE run_id=? AND arm=? AND format=?',
                            (run, arm, format)).fetchone()
    conn.execute("UPDATE model_pair_publications SET status='published' WHERE id=?", (identity,))
    conn.execute('INSERT INTO model_pair_heads VALUES (?,?,?,?) ON CONFLICT(run_id,arm,format) '
                 'DO UPDATE SET publication_id=excluded.publication_id', (run, arm, format, identity))
    if previous:
        retire(conn, previous[0])
    return identity


def read(conn, surface):
    run, arm, format = address(surface)
    row = conn.execute('SELECT p.* FROM model_pair_heads h JOIN model_pair_publications p ON p.id=h.publication_id '
                       'WHERE h.run_id=? AND h.arm=? AND h.format=?', (run, arm, format)).fetchone()
    if row is None:
        return None
    try:
        parts = conn.execute('SELECT filename,content_digest,byte_size,chunk_key FROM model_pair_files '
                             'WHERE publication_id=? ORDER BY CAST(chunk_key AS INTEGER)', (row['id'],)).fetchall()
        if len(parts) != row['part_count']:
            return None
        raw = ''.join(files.read(*part[:3], surface=surface, chunk_key=part[3]) for part in parts)
        if hashlib.sha256(raw.encode()).hexdigest() != row['output_digest']:
            return None
        value = files.decode_json(raw)
    except (OSError, ValueError, TypeError):
        return None
    if format == 'radio':
        modes = conn.execute('SELECT * FROM model_pair_modes WHERE publication_id=? ORDER BY position', (row['id'],)).fetchall()
        if modes:
            value['modes'] = [{**display, 'mode_id': mode['mode_id'], 'space': mode['space'],
                               'centroid': list(struct.unpack('<' + 'd' * (len(mode['centroid']) // 8), mode['centroid']))}
                              for display, mode in zip(value['modes'], modes, strict=True)]
        if row['generation_id'] is not None:
            value['_ranked_generation'] = row['generation_id']
            value['_ranked_prefix'] = surface + ':'
    return {'run_id': run, 'data': value}


def clear(conn, keep=()):
    keep = {int(run) for run in keep}
    for run, arm, format, identity in conn.execute('SELECT * FROM model_pair_heads').fetchall():
        if run not in keep:
            conn.execute('DELETE FROM model_pair_heads WHERE publication_id=?', (identity,))
            retire(conn, identity)


def guards():
    for table in ('model_pair_files', 'model_pair_policies', 'model_pair_contexts', 'model_pair_modes',
                  'model_pair_group_roots', 'model_pair_recommendation_roots'):
        for action, alias, state in (('INSERT', 'NEW', "<>'building'"), ('DELETE', 'OLD', "<>'retiring'")):
            yield (f'CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} WHEN '
                   f"(SELECT status FROM model_pair_publications WHERE id={alias}.publication_id){state} "
                   "BEGIN SELECT RAISE(ABORT,'Published comparison child is immutable'); END")
        # Backup relocation changes only a file's filename, like other output owners.
        if table == 'model_pair_files':
            condition = ('NEW.id<>OLD.id OR NEW.publication_id<>OLD.publication_id OR NEW.surface<>OLD.surface '
                         'OR NEW.chunk_key<>OLD.chunk_key OR NEW.content_digest<>OLD.content_digest OR NEW.byte_size<>OLD.byte_size')
        else:
            condition = '1'
        yield (f'CREATE TRIGGER {table}_update BEFORE UPDATE ON {table} WHEN {condition} '
               "BEGIN SELECT RAISE(ABORT,'Published comparison child is immutable'); END")
