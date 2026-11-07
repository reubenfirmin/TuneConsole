"""Check captured choice inputs and protected grouping independently of cleanup.

The temporary sets belong to the read-only source connection. They express
positive retention requirements; cleanup's own traversal state is never proof
that a source observation or an operational group can disappear.
"""
import hashlib
import json
from pathlib import Path

from .. import choice_evidence_files, choice_learning_state, evidence_ledger, fao
from .. import home_outputs, mix_choices, recommendation_reports
from .. import group_reads as group_queries
from . import mix_history, source


ROOTS = frozenset(('group_evidence_owners', 'group_evidence_generations', 'group_evidence_versions',
                  'unresolved_group_evidence', 'unresolved_group_contexts', 'recommendation_generation_refs'))
SCRATCH = frozenset(('group_retention_state', 'group_retention_epoch', 'group_retention_walk',
                     'group_retention_generations', 'group_retention_aliases', 'group_retention_edges'))
DISPLAYS = frozenset(('home_display_heads', 'home_display_publications', 'home_display_files',
                     'home_display_group_roots', 'home_display_recommendation_roots', 'home_display_policies'))
REPORTS = frozenset(('recommendation_report_heads', 'recommendation_report_publications',
                    'recommendation_report_files', 'choice_format_usage'))
REPLACED = ROOTS | SCRATCH | DISPLAYS | REPORTS | mix_history.LEARNING

# This predicate is intentionally independent of the preparation owner's query.
KEEP_OWNER = ("NOT (kind IN ('offer','response','checkpoint') OR (kind IN ('prepared','comparison') "
              "AND (substr(external_key,-15)=':choice_context' OR substr(external_key,-21)=':album_choice_context' "
              "OR substr(external_key,1,16)='choice-learning:')))")


def capture(reader):
    """Freeze file identities before writable runtime admission.

    The original and installed SQL databases share an appendable evidence
    directory. Enumerating it after admission cannot recover this boundary.
    The inventory travels with the existing durable rebuild bookmark.
    """
    root = evidence_ledger.directory(reader)
    return [[str(path.relative_to(root)), list(fao.read(path).digest)]
            for path in choice_evidence_files.references(reader)]


def _captured_menu(reader, target, value, *, allow_new=False):
    original = evidence_ledger.EvidenceLedger(evidence_ledger.directory(reader), 2**63 - 1)
    rebuilt = evidence_ledger.EvidenceLedger(evidence_ledger.directory(target), 2**63 - 1)
    prior = (reader.execute('SELECT context,source_digest FROM mix_choice_menus WHERE opportunity_id=?',
                            (value['id'],)).fetchone() if mix_choices.available(reader) else None)
    if value.get('precision') == mix_choices.PRECISION and prior is None:
        # Files can precede this source SQL snapshot even before cutover.
        # Their bytes remain required, but local descriptor IDs are meaningful
        # only for menus owned by the source's committed SQL dictionary.
        return
    context_id = value['context']
    captured = original.get('artifact', context_id)
    if captured is None:
        from ..choice_checkpoints import checkpoint
        saved = checkpoint(original, context_id)
        captured = saved['context'] if saved else None
    captured = captured if isinstance(captured, dict) else {'missing_context': True}
    kind = 'mixes' if value['format'] == 'mixes' else 'independent'
    expected = mix_choices.context(captured, mixes=kind == 'mixes')
    if captured.get('missing_context'):
        expected['missing_context'] = True
    if kind == 'mixes':
        menu = target.execute('SELECT context,source_digest FROM mix_choice_menus WHERE opportunity_id=?',
                              (value['id'],)).fetchone()
        digest = prior[1] if prior is not None else hashlib.sha256(evidence_ledger.canonical(value)).digest()
        if menu is None or menu[1] != digest:
            raise ValueError('Rebuild changed a captured mix menu')
        projected_context = menu[0]
    else:
        projection = target.execute('SELECT context,source_digest FROM choice_context_projections '
                                    'WHERE source_context=? AND kind=?', (value['context'], kind)).fetchone()
        if (projection is None and allow_new and captured == expected
                and rebuilt.get('artifact', context_id) == expected):
            return  # A foreground independent-format writer already compacted it.
        if projection is None or projection[1] != hashlib.sha256(evidence_ledger.canonical(captured)).digest():
            raise ValueError('Rebuild changed a captured choice context')
        projected_context = projection[0]
    if rebuilt.get('artifact', projected_context) != expected:
        raise ValueError('Rebuild changed a captured choice context')
    if kind != 'mixes':
        return
    options = target.execute('SELECT option_id,position FROM mix_choice_options '
                             'WHERE opportunity_id=? ORDER BY position', (value['id'],)).fetchall()
    if [tuple(row) for row in options] != [(str(option['id']), position)
                                         for position, option in enumerate(value['options'], 1)]:
        raise ValueError('Rebuild changed captured mix option identities or order')
    projected = mix_choices.project(target, value)
    for old, new in zip(value['options'], projected['options'], strict=True):
        frozen, actual = mix_choices.expand(reader, old), mix_choices.expand(target, new)
        for field in ('intent', 'label', 'note', 'genres', 'genres_complete', 'genre_origin'):
            default = '' if field in ('intent', 'label', 'note') else None
            if actual.get(field, default) != frozen.get(field, default):
                raise ValueError('Rebuild changed displayed mix meaning: ' + field)


def verify_captured(reader, target, *, inventory):
    """Require captured immutable history while allowing new serving output."""
    state = target.execute('SELECT phase,cursor FROM mix_choice_preparation WHERE id=1').fetchone()
    if state is None or tuple(state) != ('ready', ''):
        raise ValueError('Mix choice preparation is incomplete')
    roots = target.execute('SELECT roots_complete FROM group_retention_state WHERE id=1').fetchone()
    if roots is None or roots[0] != 1:
        raise ValueError('Group reference inventory is incomplete')
    original, rebuilt = evidence_ledger.directory(reader), evidence_ledger.directory(target)
    # Existing installed bookmarks predate the inventory. Their raw legacy
    # records and SQL-known descriptor IDs remain independently checkable;
    # extra new-format files cannot be expanded through the old SQL snapshot.
    required = capture(reader) if inventory is None else inventory
    for relative, digest in required:
        name = Path(relative)
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('Invalid captured listening evidence path')
        copied = rebuilt / name
        if not copied.exists() or list(fao.read(copied).digest) != digest:
            raise ValueError('Rebuild changed original listening evidence')
        if name.parts[0] == 'opportunity':
            value = json.loads(fao.read(copied).bytes)
            _captured_menu(reader, target, value, allow_new=inventory is None)


def protected(reader, *, at):
    """Required source IDs, without using the collector's durable scratch sets."""
    if 'group_generations' not in source.tables(reader):
        return {}
    for name in ('generations', 'aliases', 'versions', 'manifests', 'inputs'):
        reader.execute('DROP TABLE IF EXISTS temp.rebuild_required_' + name)
    reader.execute('CREATE TEMP TABLE rebuild_required_generations(id INTEGER PRIMARY KEY)')
    reader.execute('INSERT INTO rebuild_required_generations SELECT generation_id FROM group_head '
        'UNION SELECT generation_id FROM group_evidence_generations WHERE ' + KEEP_OWNER +
        " UNION SELECT generation_id FROM group_builds WHERE owner<>'cleanup' AND expires_at>=? "
        "UNION SELECT parent_generation_id FROM group_builds WHERE owner<>'cleanup' AND expires_at>=? "
        'AND parent_generation_id IS NOT NULL UNION SELECT group_generation_id FROM recommendation_generations '
        'WHERE group_generation_id IS NOT NULL', (at, at))
    reader.execute('CREATE TEMP TABLE rebuild_required_aliases(alias TEXT PRIMARY KEY) WITHOUT ROWID')
    reader.execute('INSERT INTO rebuild_required_aliases WITH RECURSIVE required(alias) AS ('
        'SELECT external_version FROM generation_groups WHERE generation_id IN (SELECT id FROM rebuild_required_generations) '
        'UNION SELECT external_version FROM group_evidence_versions WHERE ' + KEEP_OWNER +
        ' UNION SELECT l.target_alias FROM group_lineage l JOIN required r ON r.alias=l.source_alias '
        'WHERE EXISTS(SELECT 1 FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
        'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias)) SELECT alias FROM required')
    reader.execute('INSERT OR IGNORE INTO rebuild_required_generations SELECT first_generation_id FROM group_version_aliases '
        'WHERE external_version IN (SELECT alias FROM rebuild_required_aliases) UNION '
        'SELECT min(x.generation_id) FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
        'WHERE x.source_alias IN (SELECT alias FROM rebuild_required_aliases) GROUP BY x.source_alias,x.target_alias')
    for name, query in (
        ('versions', 'SELECT group_version_id FROM generation_groups WHERE generation_id IN (SELECT id FROM rebuild_required_generations) '
                     "UNION SELECT v.group_version_id FROM group_build_versions v JOIN group_builds b ON b.generation_id=v.generation_id "
                     "WHERE b.owner<>'cleanup' AND b.expires_at>=?"),
        ('manifests', 'SELECT input_manifest_id FROM group_generations WHERE id IN (SELECT id FROM rebuild_required_generations)'),
        ('inputs', 'SELECT input_id FROM group_manifest_inputs WHERE manifest_id IN (SELECT id FROM rebuild_required_manifests) '
                   'UNION SELECT input_id FROM group_members WHERE group_version_id IN (SELECT id FROM rebuild_required_versions) '
                   'AND input_id IS NOT NULL')):
        reader.execute('CREATE TEMP TABLE rebuild_required_' + name + '(id INTEGER PRIMARY KEY)')
        reader.execute('INSERT OR IGNORE INTO rebuild_required_' + name + ' ' + query, (at,) if name == 'versions' else ())
    owners = {
        'group_generations': ('generations', 'id'), 'group_publications': ('generations', 'generation_id'),
        'generation_groups': ('generations', 'generation_id'), 'group_versions': ('versions', 'id'),
        'group_members': ('versions', 'group_version_id'), 'unresolved_group_members': ('versions', 'group_version_id'),
        'group_representatives': ('versions', 'group_version_id'), 'group_version_aliases': ('versions', 'group_version_id'),
        'group_input_manifests': ('manifests', 'id'), 'group_manifest_inputs': ('manifests', 'manifest_id'),
        'unresolved_group_manifest_inputs': ('manifests', 'manifest_id'), 'group_recording_inputs': ('inputs', 'id')}
    for table in ('group_assignments', 'unresolved_group_assignments', 'group_unknown', 'unresolved_group_unknown',
                  'group_graph', 'unresolved_group_graph'):
        owners[table] = ('generations', 'generation_id')

    def required(table, row):
        if table in owners:
            name, column = owners[table]
            # Aliases whose content is shared can still retire independently.
            if table == 'group_version_aliases':
                return bool(reader.execute('SELECT 1 FROM rebuild_required_generations WHERE id=?',
                                           (row['first_generation_id'],)).fetchone())
            return bool(reader.execute('SELECT 1 FROM rebuild_required_' + name + ' WHERE id=?', (row[column],)).fetchone())
        if table == 'group_lineage':
            return bool(reader.execute('SELECT 1 FROM rebuild_required_aliases WHERE alias=? UNION '
                'SELECT 1 FROM group_generation_lineage x JOIN group_builds b ON b.generation_id=x.generation_id '
                "WHERE x.source_alias=? AND x.target_alias=? AND b.owner<>'cleanup' AND b.expires_at>=?",
                (row['source_alias'], row['source_alias'], row['target_alias'], at)).fetchone())
        if table == 'group_generation_lineage':
            return bool(reader.execute('SELECT 1 FROM group_builds WHERE generation_id=? '
                "AND owner<>'cleanup' AND expires_at>=? UNION "
                'SELECT 1 FROM rebuild_required_aliases WHERE alias=? AND ?=('
                'SELECT min(x.generation_id) FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
                'WHERE x.source_alias=? AND x.target_alias=?)',
                (row['generation_id'], at, row['source_alias'], row['generation_id'], row['source_alias'], row['target_alias'])).fetchone())
        if table in ('group_builds', 'group_build_versions'):
            return bool(reader.execute("SELECT 1 FROM group_builds WHERE generation_id=? AND owner<>'cleanup' AND expires_at>=?",
                                       (row['generation_id'], at)).fetchone())
        return True
    return {table: (lambda row, table=table: required(table, row))
            for table in (*owners, 'group_lineage', 'group_generation_lineage', 'group_builds', 'group_build_versions')}


def _root_key(reader, target, identity):
    if not identity.startswith('home-output:'):
        return identity
    _, publication, field = identity.split(':')
    row = reader.execute('SELECT surface FROM home_display_heads WHERE publication_id=?', (publication,)).fetchone()
    if row is None:
        return identity
    current = target.execute('SELECT publication_id FROM home_display_heads WHERE surface=?', (row[0],)).fetchone()
    if current is None:
        raise ValueError('Rebuild lost a prepared Home display')
    return 'home-output:' + str(current[0]) + ':' + field


def _roots(reader, target):
    from .verification import rows
    for table in sorted(ROOTS & source.tables(reader)):
        fields = source.columns(reader, table)
        keys = [row[1] for row in reader.execute('PRAGMA table_info(' + table + ')') if row[5]]
        for original in rows(reader, table):
            if mix_history.obsolete(original['kind'], original['external_key']):
                continue
            expected = dict(original, external_key=_root_key(reader, target, original['external_key']))
            actual = target.execute('SELECT ' + ','.join(fields) + ' FROM ' + table + ' WHERE ' +
                ' AND '.join(key + '=?' for key in keys), tuple(expected[key] for key in keys)).fetchone()
            if actual is None or tuple(actual) != tuple(expected[key] for key in fields):
                raise ValueError('Rebuild changed a required group reference: ' + table)
    for kind, identity in target.execute('SELECT kind,external_key FROM group_evidence_owners'):
        if mix_history.obsolete(kind, identity):
            raise ValueError('Rebuild retained an obsolete choice pin')


def _ledger(reader, target):
    original, rebuilt = evidence_ledger.directory(reader), evidence_ledger.directory(target)
    admitted, responses, menus = [], [], 0
    for path in choice_evidence_files.references(reader):
        copied = rebuilt / path.relative_to(original)
        if not copied.exists() or fao.read(path).digest != fao.read(copied).digest:
            raise ValueError('Rebuild changed original listening evidence')
        kind = path.parent.parent.name
        if kind not in ('opportunity', 'response'):
            continue
        value = json.loads(fao.read(path).bytes)
        (admitted if kind == 'opportunity' else responses).append(value)
        if kind != 'opportunity':
            continue
        original_context = evidence_ledger.EvidenceLedger(original, 2**63 - 1).get('artifact', value['context'])
        if original_context is None:
            from ..choice_checkpoints import checkpoint
            saved = checkpoint(evidence_ledger.EvidenceLedger(original, 2**63 - 1), value['context'])
            original_context = saved['context'] if saved else None
        captured = original_context if isinstance(original_context, dict) else {'missing_context': True}
        expected_context = ({'schema': 2, 'precision': 'theme-raw-genres-v1',
                             **{key: captured[key] for key in ('at', 'comparison') if key in captured}}
                            if value['format'] == 'mixes' else
                            {key: item for key, item in captured.items() if key not in mix_choices.GROUP_FIELDS})
        if captured.get('missing_context'):
            expected_context['missing_context'] = True
        kind = 'mixes' if value['format'] == 'mixes' else 'independent'
        projection = target.execute('SELECT context,source_digest FROM choice_context_projections '
                                    'WHERE source_context=? AND kind=?', (value['context'], kind)).fetchone()
        if (projection is None or projection[1] != hashlib.sha256(evidence_ledger.canonical(captured)).digest()
                or evidence_ledger.EvidenceLedger(rebuilt, 2**63 - 1).get('artifact', projection[0]) != expected_context):
            raise ValueError('Rebuild changed a captured choice context')
        if value['format'] != 'mixes':
            continue
        menus += 1
        menu = target.execute('SELECT source_digest FROM mix_choice_menus WHERE opportunity_id=?', (value['id'],)).fetchone()
        if menu is None or menu[0] != hashlib.sha256(evidence_ledger.canonical(value)).digest():
            raise ValueError('Rebuild changed a captured mix menu')
        projected = mix_choices.project(target, value)
        for old, new in zip(value['options'], projected['options'], strict=True):
            frozen = mix_choices.expand(reader, old)
            actual = mix_choices.expand(target, new)
            for field in ('intent', 'label', 'note', 'genres', 'genres_complete', 'genre_origin'):
                if actual.get(field, '' if field in ('intent', 'label', 'note') else None) != frozen.get(
                        field, '' if field in ('intent', 'label', 'note') else None):
                    raise ValueError('Rebuild changed displayed mix meaning: ' + field)
    if target.execute('SELECT count(*) FROM mix_choice_menus').fetchone()[0] != menus:
        raise ValueError('Rebuild invented a mix menu')
    return admitted, responses


def _released_head(reader, target):
    row = reader.execute('SELECT generation FROM rec_group_head WHERE id=1').fetchone()
    actual = group_queries.head(target, members=True)
    if row is None:
        if actual is not None:
            raise ValueError('Rebuild invented a released musical group head')
        return
    key = row[0]
    generation = reader.execute('SELECT * FROM rec_group_generations WHERE generation=?', (key,)).fetchone()
    expected = dict(generation)
    if (actual is None or actual['generation'] != key or json.loads(actual['policy']) != json.loads(expected['policy'])
            or actual['fingerprint'] != expected['fingerprint'] or actual['created_at'] != expected['created_at']
            or actual['serial'] != expected['serial'] or actual['payload'] != json.loads(expected['payload'])):
        raise ValueError('Rebuild changed the released musical group head')
    groups = []
    for version, group, parent, raw in reader.execute('SELECT version,group_id,parent_version,payload '
                                                     'FROM rec_group_definitions WHERE generation=? ORDER BY rowid', (key,)):
        members = {identity: (similarity, support) for identity, similarity, support in reader.execute(
            'SELECT identity_key,similarity,support FROM rec_group_members WHERE version=?', (version,))}
        groups.append({**json.loads(raw), 'version': version, 'group_id': group, 'parent_version': parent, 'members': members})
    if actual['groups'] != groups:
        raise ValueError('Rebuild changed released group definitions or membership')


def _displays(reader, target):
    from ...rec.choice_context import context_manifest
    ledger = evidence_ledger.EvidenceLedger(evidence_ledger.directory(reader), 2**63 - 1)
    rebuilt_ledger = evidence_ledger.EvidenceLedger(evidence_ledger.directory(target), 2**63 - 1)
    tables = source.tables(reader)
    if 'home_display_heads' in tables:
        surfaces = {row[0] for row in reader.execute('SELECT surface FROM home_display_heads')}
        originals = ((surface, home_outputs.encoded(reader, surface)) for surface in sorted(surfaces))
    else:
        originals = [(surface, raw) for surface, raw in reader.execute('SELECT surface,payload FROM rec_proposals')
                     if surface in home_outputs.SURFACES]
        surfaces = {surface for surface, _ in originals}
    if {row[0] for row in target.execute('SELECT surface FROM home_display_heads')} != surfaces:
        raise ValueError('Rebuild changed prepared Home surfaces')
    for surface, raw in originals:
        old = home_outputs.prepare(surface, raw, None).value
        encoded = home_outputs.encoded(target, surface)
        if encoded is None:
            raise ValueError('Rebuild lost prepared Home output')
        actual = home_outputs.prepare(surface, encoded, None).value
        expected = dict(old) if isinstance(old, dict) else old
        if isinstance(old, dict):
            for field in ('choice_context', 'album_choice_context'):
                if not old.get(field):
                    continue
                captured = context_manifest(ledger, old[field])
                compact = ({'schema': 2, 'precision': 'theme-raw-genres-v1',
                            **{key: captured[key] for key in ('at', 'comparison') if key in captured}}
                           if field == 'choice_context' else
                           {key: value for key, value in captured.items() if key not in mix_choices.GROUP_FIELDS})
                if not isinstance(actual, dict) or context_manifest(rebuilt_ledger, actual.get(field)) != compact:
                    raise ValueError('Rebuild changed a frozen display choice context')
                expected[field] = actual[field]
        if actual != expected:
            raise ValueError('Rebuild changed current Home display contents')
        if 'home_display_heads' in tables:
            query = ('SELECT p.built_at,p.input_revision FROM home_display_heads h '
                     'JOIN home_display_publications p ON p.id=h.publication_id WHERE h.surface=?')
            if tuple(reader.execute(query, (surface,)).fetchone()) != tuple(target.execute(query, (surface,)).fetchone()):
                raise ValueError('Rebuild changed Home display input identity')


def _reports(reader, target, watermark):
    if 'recommendation_report_heads' in source.tables(reader):
        originals = ((surface, recommendation_reports.encoded(reader, surface))
                     for surface, in reader.execute('SELECT surface FROM recommendation_report_heads'))
    else:
        originals = ((surface, raw) for surface, raw in reader.execute('SELECT surface,payload FROM rec_proposals')
                     if surface in recommendation_reports.SURFACES)
    for surface, raw in originals:
        if surface != 'listening_choices' and recommendation_reports.encoded(target, surface) != raw:
            raise ValueError('Rebuild changed an unrelated recommendation report')
    report = json.loads(recommendation_reports.encoded(target, 'listening_choices'))
    if report['watermark'] != watermark:
        raise ValueError('Rebuilt choice report describes different inputs')


def verify(reader, target, *, at):
    if tuple(target.execute('SELECT phase,cursor FROM mix_choice_preparation WHERE id=1').fetchone()) != ('ready', ''):
        raise ValueError('Mix choice preparation is incomplete')
    if target.execute('SELECT roots_complete FROM group_retention_state WHERE id=1').fetchone()[0] != 1:
        raise ValueError('Group reference inventory is incomplete')
    _roots(reader, target)
    _displays(reader, target)
    offers, responses = _ledger(reader, target)
    watermark = hashlib.sha256(evidence_ledger.canonical([
        sorted(o['id'] for o in offers if o['at'] <= at and o['observed_at'] <= at),
        [r['id'] for r in sorted((r for r in responses if r['at'] <= at and r['observed_at'] <= at),
                                key=lambda r: (r['at'], r['kind'] != 'visible', r['id']))]])).hexdigest()
    interests = choice_learning_state.read(target, 'choice_interests')
    if interests is None or interests.get('watermark') != watermark:
        raise ValueError('Rebuilt learning does not describe the captured choice inputs')
    for context in interests.get('contexts', []):
        if context.get('format') == 'mixes' and (context.get('keys') or context.get('feature') or context.get('mode_id') is not None):
            raise ValueError('Rebuilt mix learning invented historical track precision')
    _reports(reader, target, watermark)
    if 'group_head' in source.tables(reader):
        if group_queries.head(reader, members=True) != group_queries.head(target, members=True):
            raise ValueError('Rebuild changed current musical groups')
    else:
        _released_head(reader, target)
