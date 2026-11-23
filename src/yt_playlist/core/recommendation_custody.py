"""Retain publication generations without importing preparation or display consumers."""
import sqlite3

from .publication_files import HOME, MANIFESTS

INDEX = 'ix_recommendation_generations_evidence_roots'
ROOTS = (f'recommendation_generations g INDEXED BY {INDEX},json_each(g.evidence_roots) root '
         "WHERE g.evidence_roots<>'[]'")
HOME_ROOTS = (f"{MANIFESTS} p,json_each(p.value,'$.recommendation_roots') home "
              f"WHERE p.key GLOB '{HOME}*'")
HOME_GENERATIONS = f'SELECT home.value->>3 FROM {HOME_ROOTS}'
HOME_RETIREMENT_GUARD = 'guard_home_recommendation_retirement'


def retainers(conn, kind, external_key):
    return [row[0] for row in conn.execute(
        f'SELECT g.id FROM {ROOTS} AND root.value=json_array(:kind,:key) UNION '
        f'{HOME_GENERATIONS} AND home.value->>1=:kind AND home.value->>2=:key ORDER BY 1',
        {'kind': kind, 'key': external_key})]


def remove(predicate):
    return ('UPDATE recommendation_generations SET evidence_roots=(SELECT json_group_array(json(root.value)) '
            'FROM json_each(evidence_roots) root WHERE NOT (' + predicate + ')) '
            f'WHERE id IN (SELECT g.id FROM {ROOTS} AND ({predicate}))')


def require_published(conn, generation_id):
    if not conn.execute("SELECT 1 FROM recommendation_generations WHERE id=? AND status='published'",
                        (generation_id,)).fetchone():
        raise sqlite3.IntegrityError('Recommendation evidence requires a published generation')


def retain(conn, kind, external_key, generation_id):
    if not conn.in_transaction:
        raise RuntimeError('Recommendation evidence requires an outer transaction')
    if kind not in {'offer', 'response', 'prepared', 'comparison', 'pending', 'checkpoint'} or not external_key:
        raise sqlite3.IntegrityError('Invalid recommendation evidence owner')
    require_published(conn, generation_id)
    conn.execute("UPDATE recommendation_generations SET evidence_roots=json_insert(evidence_roots,'$[#]',"
                 'json_array(:kind,:key)) WHERE id=:generation AND NOT EXISTS('
                 'SELECT 1 FROM json_each(evidence_roots) WHERE value=json_array(:kind,:key))',
                 {'kind': kind, 'key': external_key, 'generation': generation_id})


def release(conn, kind, external_key):
    if not conn.in_transaction:
        raise RuntimeError('Recommendation evidence retirement requires an outer transaction')
    if kind == 'prepared' and external_key.startswith('home-output:') and conn.execute(
            f'SELECT 1 FROM {HOME_ROOTS} AND home.value->>2=? LIMIT 1', (external_key,)).fetchone():
        raise sqlite3.IntegrityError('Home display retains this reference')
    conn.execute(remove('root.value=json_array(:kind,:key)'), {'kind': kind, 'key': external_key})
