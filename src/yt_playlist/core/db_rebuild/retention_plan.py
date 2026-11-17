"""Retained-copy selection over supplied schema facts, without database access.

The result uses private TEMP sets so selection never loads an unbounded source
history into Python or changes serving rows. Unknown FK owners always protect
their referenced parent; unsupported composite references fail closed.
"""
from dataclasses import dataclass


def quote(name):
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Selection:
    predicates: dict
    statements: tuple


def _references(schema, table, parent):
    for target, columns in schema[table]:
        if target == parent:
            if len(columns) != 1 or columns[0][1] not in ('id', None):
                label = 'generation' if parent == 'recommendation_generations' else 'pool'
                raise ValueError('Unsupported recommendation ' + label + ' reference: ' + table)
            yield quote(columns[0][0])


def plan(schema, *, now, references='1'):
    tables = set(schema)
    predicates, steps = {}, []
    generations = {'recommendation_generations', 'recommendation_heads', 'recommendation_generation_refs',
                   'recommendation_builds'}
    if not generations <= tables:
        return Selection(predicates, ())
    owned = ('prepared_outputs', 'recommendation_bundle_framing', 'recommendation_surface_pools',
             'recommendation_build_pools', 'recommendation_generation_surfaces', 'recommendation_builds',
             'recommendation_pool_revalidations')
    steps.append(('CREATE TEMP TABLE compact_drop_generations(id INTEGER PRIMARY KEY)', ()))
    steps.append(('INSERT INTO compact_drop_generations SELECT g.id FROM main.recommendation_generations g '
        'WHERE NOT EXISTS(SELECT 1 FROM main.recommendation_heads h WHERE h.generation_id=g.id) '
        'AND NOT EXISTS(SELECT 1 FROM main.recommendation_generation_refs r WHERE r.generation_id=g.id AND ('+references+')) '
        "AND (g.status IN ('published','retiring') OR EXISTS(SELECT 1 FROM main.recommendation_builds b "
        'WHERE b.generation_id=g.id AND b.expires_at<?))', (now,)))
    for table in sorted(tables - set(owned)):
        for column in _references(schema, table, 'recommendation_generations'):
            condition = (' AND ('+references+')' if table in (
                'recommendation_generation_refs', 'home_display_recommendation_roots') else '')
            steps.append(('DELETE FROM compact_drop_generations WHERE EXISTS(SELECT 1 FROM main.'
                + quote(table) + ' r WHERE r.' + column + '=compact_drop_generations.id'+condition+')', ()))
    predicates['recommendation_generations'] = 's.id NOT IN (SELECT id FROM compact_drop_generations)'
    if references != '1':
        predicates['recommendation_generation_refs'] = references
    for table in owned:
        if table in tables:
            predicates[table] = 's.generation_id NOT IN (SELECT id FROM compact_drop_generations)'
    owners = ('recommendation_surface_pools', 'recommendation_build_pools', 'prepared_outputs',
              *(('recommendation_pool_revalidations',) if 'recommendation_pool_revalidations' in tables else ()))
    if 'recommendation_pools' not in tables or not set(owners) <= tables:
        return Selection(predicates, tuple(steps))
    steps.append(('CREATE TEMP TABLE compact_drop_pools(id INTEGER PRIMARY KEY)', ()))
    steps.append(('INSERT INTO compact_drop_pools SELECT p.id FROM main.recommendation_pools p WHERE '
        + ' AND '.join('NOT EXISTS(SELECT 1 FROM main.' + table + ' s WHERE s.pool_id=p.id AND ('
                       + predicates.get(table, '1') + '))' for table in owners), ()))
    children = ('recommendation_radio_bands', 'recommendation_candidates',
                'unresolved_recommendation_candidates', 'recommendation_pool_retirements')
    for table in sorted(tables - set(owners) - set(children)):
        for column in _references(schema, table, 'recommendation_pools'):
            steps.append(('DELETE FROM compact_drop_pools WHERE EXISTS(SELECT 1 FROM main.'
                + quote(table) + ' r WHERE r.' + column + '=compact_drop_pools.id)', ()))
    predicates['recommendation_pools'] = 's.id NOT IN (SELECT id FROM compact_drop_pools)'
    for table in children:
        if table in tables:
            predicates[table] = 's.pool_id NOT IN (SELECT id FROM compact_drop_pools)'
    return Selection(predicates, tuple(steps))
