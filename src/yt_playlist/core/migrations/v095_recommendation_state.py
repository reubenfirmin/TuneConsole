"""Retire the last generic recommendation state in favor of its domain owners."""
from pathlib import Path
import sqlite3

from yt_playlist.core import library_pool_state, model_comparison_state, model_projections, tv_recommendation_state
from .runner import MigrationError


def statements():
    for name in ('schema_tv_recommendations.sql', 'schema_clicked_library_pools.sql', 'schema_model_projections.sql',
                 'schema_radio_policy_roots.sql'):
        pending = ''
        for line in Path(__file__).with_name(name).read_text().splitlines(True):
            pending += line
            if sqlite3.complete_statement(pending):
                yield pending
                pending = ''
        if pending.strip():
            raise MigrationError('Incomplete recommendation schema: ' + name)
    yield from model_control_statements()
    yield from model_projections.guards()
    yield from radio_seed_fences()
    yield from retired_fences()
    from . import v095_saved_outputs
    yield from v095_saved_outputs.statements()


def radio_seed_fences():
    paths = ('$.pool_policy', '$.model_version', '$.serving_context.pool_policy',
             '$.serving_context.model_version', '$.provenance.pool_policy', '$.provenance.model_version')
    versions = ' UNION '.join("SELECT CAST(json_extract(t.value,'" + path + "') AS TEXT) raw FROM "
        "json_each(CASE WHEN json_valid(NEW.value) THEN NEW.value ELSE '{}' END,'$.tracks') t "
        "WHERE json_type(t.value,'" + path + "') IN ('text','integer')" for path in paths)
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER radio_seed_policy_{event.lower()} AFTER {event} ON settings '
               "WHEN NEW.key='radio_seed_offer' BEGIN DELETE FROM radio_seed_policies WHERE setting_key=NEW.key; "
               'INSERT INTO radio_seed_policies SELECT NEW.key,v.raw,p.version,'
               "CASE WHEN p.version IS NULL THEN 'missing_policy' ELSE NULL END FROM (" + versions + ') v '
               "LEFT JOIN recommendation_policies p ON p.version=v.raw WHERE length(v.raw)>0; END")


def retired_fences():
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER retired_recommendation_bucket_{event.lower()} BEFORE {event} ON rec_proposals '
               "BEGIN SELECT RAISE(ABORT,'Recommendation state requires its domain owner'); END")


def model_control_statements():
    """The finite numbered contract; independent of future runtime field additions."""
    number = "REAL CHECK({0} IS NULL OR (typeof({0}) IN ('integer','real') AND abs({0})<=1.7976931348623157e308))"
    integer = "INTEGER CHECK({0} IS NULL OR (typeof({0})='integer' AND {0}>=0))"
    text = "TEXT CHECK({0} IS NULL OR (length({0}) BETWEEN 1 AND 4096))"
    flag = "INTEGER CHECK({0} IS NULL OR (typeof({0})='integer' AND {0} IN (0,1)))"
    policy = [('mask', integer), ('version', text), ('horizon_days', integer), ('followup_hours', number),
              ('min_days', integer), ('min_observed', integer), ('min_coverage', number), ('useful_effect', number),
              ('format_margin', number), ('min_format_days', integer), ('retry_days', integer), ('family_alpha', number)]
    setup = [('mask', integer), ('method', text), ('dim', integer)]
    plan = [('phase', "TEXT CHECK(phase IS NULL OR phase IN ('preparing','observing','failed','applied','retained'))"),
        ('attempt', integer), ('seed', text), ('alpha', number), ('constraints', text), ('run_id', integer),
        ('started_at', number), ('exposure_until', number), ('decision_at', number), ('vectors', text),
        ('generation', text), ('prepared_at', number), ('last_prepare_attempt', number),
        ('runtime_seconds', number), ('retry_at', number), ('reason', text), ('ended_at', number)]
    plan += [('policy_' + name, kind) for name, kind in policy]
    plan += [(prefix + '_' + name, kind) for prefix in ('previous', 'challenger') for name, kind in setup]
    monitor = [('run_id', integer), ('generation', text), ('started_at', number), ('exposure_until', number),
        ('next_at', number), ('baseline_present', flag), ('declining_weeks', integer), ('complete', flag)]
    monitor += [('policy_' + name, kind) for name, kind in policy] + [('setup_' + name, kind) for name, kind in setup]
    assignment = [('epoch', "CHECK(epoch IS NULL OR typeof(epoch) IN ('integer','text'))"), ('comparison_mask', integer),
        ('comparison_run_id', integer), ('comparison_arm', "TEXT CHECK(comparison_arm IS NULL OR comparison_arm IN ('incumbent','challenger'))"),
        ('comparison_probability', "REAL CHECK(comparison_probability IS NULL OR comparison_probability BETWEEN 0 AND 1)"),
        ('comparison_unit', text), ('comparison_format', "TEXT CHECK(comparison_format IS NULL OR comparison_format IN ('mixes','albums','radio'))"),
        ('comparison_window', text), ('comparison_policy', text), ('comparison_generation', text),
        ('comparison_at', number), ('comparison_controlled', flag)]
    for table, fields, maximum, raw_run in (
            ('model_comparison_plan', plan, 1048575, 'run_id'),
            ('model_listening_monitor', monitor, 1023, 'run_id'),
            ('model_album_assignment', assignment, 3, 'comparison_run_id')):
        columns = ['id INTEGER PRIMARY KEY NOT NULL CHECK(id=1)',
                   "field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND " + str(maximum) + ')',
                   'run_ref INTEGER REFERENCES rec_model_selection_runs(id)', 'run_resolution TEXT',
                   'built_at ' + number.format('built_at')]
        columns.extend('"' + name + '" ' + kind.format('"' + name + '"') for name, kind in fields)
        columns.append(f'CHECK(({raw_run} IS NULL AND run_ref IS NULL AND run_resolution IS NULL) OR '
            f'({raw_run} IS NOT NULL AND ((run_ref IS NOT NULL AND run_ref={raw_run} AND run_resolution IS NULL) OR '
            "(run_ref IS NULL AND run_resolution IS NOT NULL AND run_resolution='missing_run'))))")
        yield 'CREATE TABLE ' + table + ' (' + ','.join(columns) + ')'
    yield ("CREATE TABLE model_monitor_baseline (monitor_id INTEGER NOT NULL REFERENCES model_listening_monitor(id),"
           "position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 4095),"
           "value REAL NOT NULL CHECK(typeof(value) IN ('integer','real') AND value BETWEEN 0 AND 1),"
           'PRIMARY KEY(monitor_id,position)) WITHOUT ROWID')


def apply(conn, context):
    sources = []
    for surface in sorted(tv_recommendation_state.SURFACES):
        row = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        if row:
            try:
                sources.append((surface, tv_recommendation_state.decode(surface, row[0]), row[1]))
            except (ValueError, TypeError, OverflowError) as exc:
                raise MigrationError(f'Invalid {surface}; original TV state retained: {exc}') from exc
    library = []
    for surface in library_pool_state.SURFACES:
        row = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        if row:
            try:
                value = library_pool_state.decode(surface, row[0])
                plan = library_pool_state.prepare(conn, surface, value, row[1])
                if plan is not None:
                    from yt_playlist.controllers import recommendation_import
                    recommendation_import.preflight(conn, plan)
                library.append((surface, value, row[1], plan))
            except (ValueError, TypeError, OverflowError) as exc:
                raise MigrationError(f'Invalid {surface}; original clicked pool retained: {exc}') from exc
    controls = []
    for surface in sorted(model_comparison_state.SURFACES):
        row = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        if row:
            try:
                controls.append((surface, model_comparison_state.decode(surface, row[0]), row[1]))
            except (ValueError, TypeError, OverflowError) as exc:
                raise MigrationError(f'Invalid {surface}; original comparison control retained: {exc}') from exc
    pairs = []
    for surface, raw, at in conn.execute("SELECT surface,payload,built_at FROM rec_proposals WHERE surface LIKE 'model_pair:%'"):
        try:
            prepared = model_projections.prepare(conn, surface, model_comparison_state.recommendation_outputs.decode_json(raw),
                                                 at, historical=True)
            if prepared.plan is not None:
                from yt_playlist.controllers import recommendation_import
                recommendation_import.preflight(conn, prepared.plan)
            pairs.append(prepared)
        except (ValueError, TypeError, OverflowError, KeyError) as exc:
            raise MigrationError(f'Invalid {surface}; original comparison retained: {exc}') from exc
    for sql in statements():
        conn.execute(sql)
    for surface, value, at in sources:
        tv_recommendation_state.write(conn, surface, value, at, historical=True)
        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
    for surface, value, at, plan in library:
        if plan is not None:
            recommendation_import.import_plan(conn, plan, plan.created_at,
                before_publish=lambda generation: library_pool_state.parameters(conn, generation, value['parameters'], at))
        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
    for surface, value, at in controls:
        model_comparison_state.write(conn, surface, value, at)
        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
    for prepared in pairs:
        model_projections.publish(conn, prepared, historical=True)
        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (prepared.surface,))
        from yt_playlist.core import group_evidence
        from yt_playlist.repos import recommendation_publication
        for field in (*model_projections.CONTEXTS, 'recommendations'):
            key = 'proposal:' + prepared.surface + ':' + field
            group_evidence.release(conn, 'comparison', key)
            recommendation_publication.release(conn, 'comparison', key)
    from yt_playlist.core import recommendation_setting_outputs
    saved = dict(conn.execute("SELECT key,value FROM settings WHERE key IN ('radio_seed_offer','rec_autotune_result')"))
    recommendation_setting_outputs.put(conn, saved)
    conn.executemany('DELETE FROM settings WHERE key=?', ((key,) for key in saved))
    from yt_playlist.core import collection_recovery, radio_policy_roots, radio_storage
    if radio_storage.available(conn):
        for row in conn.execute('SELECT id,recovery_filename,recovery_digest,recovery_bytes FROM radio_sessions'):
            state = collection_recovery.read(row[1], row[2], row[3], kind='radio-session', owner=row[0])
            radio_policy_roots.save_session(conn, row[0], state)
    unknown = [row[0] for row in conn.execute('SELECT surface FROM rec_proposals ORDER BY surface LIMIT 20')]
    if unknown:
        raise MigrationError('Unclassified recommendation surfaces; original state retained: ' + ', '.join(unknown))
