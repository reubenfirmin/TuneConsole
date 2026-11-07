"""Typed production trial, follow-up plan and held album assignment."""
import math

from . import recommendation_outputs

POLICY = ('version', 'horizon_days', 'followup_hours', 'min_days', 'min_observed',
          'min_coverage', 'useful_effect', 'format_margin', 'min_format_days', 'retry_days', 'family_alpha')
SETUP = ('method', 'dim')
ASSIGNMENT = ('run_id', 'arm', 'probability', 'unit', 'format', 'window', 'policy', 'generation', 'at', 'controlled')
PLAN = ('phase', 'attempt', 'seed', 'policy', 'alpha', 'previous', 'challenger', 'constraints',
        'run_id', 'started_at', 'exposure_until', 'decision_at', 'vectors', 'generation',
        'prepared_at', 'last_prepare_attempt', 'runtime_seconds', 'retry_at', 'reason', 'ended_at')
MONITOR = ('run_id', 'generation', 'setup', 'started_at', 'exposure_until', 'next_at',
           'policy', 'baseline', 'declining_weeks', 'complete')
OWNERS = {'model_live_comparison': ('model_comparison_plan', PLAN, {'policy': POLICY, 'previous': SETUP, 'challenger': SETUP}),
          'model_listening_monitor': ('model_listening_monitor', MONITOR, {'policy': POLICY, 'setup': SETUP}),
          'model_album_assignment': ('model_album_assignment', ('epoch', 'comparison'), {'comparison': ASSIGNMENT})}
SURFACES = frozenset(OWNERS)
INTEGERS = frozenset({'attempt', 'run_id', 'dim', 'declining_weeks', 'horizon_days', 'min_days',
                      'min_observed', 'min_format_days', 'retry_days'})
NUMBERS = frozenset({'alpha', 'started_at', 'exposure_until', 'decision_at', 'prepared_at',
                    'last_prepare_attempt', 'runtime_seconds', 'retry_at', 'ended_at', 'next_at',
                    'followup_hours', 'min_coverage', 'useful_effect', 'format_margin', 'family_alpha',
                    'probability', 'at'})


def statements():
    """Frozen v95 trial field contract, shared by preparation and replay.

    Keep these declarations independent of whole-schema assembly: current
    admission validates retained preparation owners in a genuinely cold process.
    """
    number = "REAL CHECK({0} IS NULL OR (typeof({0}) IN ('integer','real') AND abs({0})<=1.7976931348623157e308))"
    integer = "INTEGER CHECK({0} IS NULL OR (typeof({0})='integer' AND {0}>=0))"
    text = "TEXT CHECK({0} IS NULL OR (length({0}) BETWEEN 1 AND 4096))"
    flag = "INTEGER CHECK({0} IS NULL OR (typeof({0})='integer' AND {0} IN (0,1)))"
    policy = [('mask',integer),('version',text),('horizon_days',integer),('followup_hours',number),
        ('min_days',integer),('min_observed',integer),('min_coverage',number),('useful_effect',number),
        ('format_margin',number),('min_format_days',integer),('retry_days',integer),('family_alpha',number)]
    setup = [('mask',integer),('method',text),('dim',integer)]
    plan = [('phase',"TEXT CHECK(phase IS NULL OR phase IN ('preparing','observing','failed','applied','retained'))"),
        ('attempt',integer),('seed',text),('alpha',number),('constraints',text),('run_id',integer),
        ('started_at',number),('exposure_until',number),('decision_at',number),('vectors',text),
        ('generation',text),('prepared_at',number),('last_prepare_attempt',number),
        ('runtime_seconds',number),('retry_at',number),('reason',text),('ended_at',number)]
    plan += [('policy_'+name,kind) for name,kind in policy]
    plan += [(prefix+'_'+name,kind) for prefix in ('previous','challenger') for name,kind in setup]
    monitor = [('run_id',integer),('generation',text),('started_at',number),('exposure_until',number),
        ('next_at',number),('baseline_present',flag),('declining_weeks',integer),('complete',flag)]
    monitor += [('policy_'+name,kind) for name,kind in policy]+[('setup_'+name,kind) for name,kind in setup]
    assignment = [('epoch',"CHECK(epoch IS NULL OR typeof(epoch) IN ('integer','text'))"),('comparison_mask',integer),
        ('comparison_run_id',integer),('comparison_arm',"TEXT CHECK(comparison_arm IS NULL OR comparison_arm IN ('incumbent','challenger'))"),
        ('comparison_probability',"REAL CHECK(comparison_probability IS NULL OR comparison_probability BETWEEN 0 AND 1)"),
        ('comparison_unit',text),('comparison_format',"TEXT CHECK(comparison_format IS NULL OR comparison_format IN ('mixes','albums','radio'))"),
        ('comparison_window',text),('comparison_policy',text),('comparison_generation',text),
        ('comparison_at',number),('comparison_controlled',flag)]
    for table,fields,maximum,raw_run in (
        ('model_comparison_plan',plan,1048575,'run_id'),('model_listening_monitor',monitor,1023,'run_id'),
        ('model_album_assignment',assignment,3,'comparison_run_id')):
        columns = ['id INTEGER PRIMARY KEY NOT NULL CHECK(id=1)',
            "field_mask INTEGER NOT NULL CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND "+str(maximum)+')',
            'run_ref INTEGER REFERENCES rec_model_selection_runs(id)','run_resolution TEXT','built_at '+number.format('built_at')]
        columns.extend('"'+name+'" '+kind.format('"'+name+'"') for name,kind in fields)
        columns.append(f'CHECK(({raw_run} IS NULL AND run_ref IS NULL AND run_resolution IS NULL) OR '
            f'({raw_run} IS NOT NULL AND ((run_ref IS NOT NULL AND run_ref={raw_run} AND run_resolution IS NULL) OR '
            "(run_ref IS NULL AND run_resolution IS NOT NULL AND run_resolution='missing_run'))))")
        yield 'CREATE TABLE '+table+' ('+','.join(columns)+')'
    yield ("CREATE TABLE model_monitor_baseline (monitor_id INTEGER NOT NULL REFERENCES model_listening_monitor(id),"
           "position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 4095),"
           "value REAL NOT NULL CHECK(typeof(value) IN ('integer','real') AND value BETWEEN 0 AND 1),"
           'PRIMARY KEY(monitor_id,position)) WITHOUT ROWID')


def _mask(value, fields):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError('Unclassified model comparison fields')
    return sum(1 << index for index, field in enumerate(fields) if field in value)


def _scalar(field, value):
    if value is None:
        return value
    if field in INTEGERS:
        if type(value) is not int or not 0 <= value < 2 ** 63:
            raise ValueError('Invalid model comparison count or identity')
    elif field in NUMBERS:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('Invalid model comparison measurement')
    elif field in ('controlled', 'complete'):
        if type(value) is not bool:
            raise ValueError('Invalid model comparison flag')
        return int(value)
    elif field == 'epoch':
        if not (type(value) is int and -(2 ** 63) <= value < 2 ** 63
                or type(value) is str and 0 < len(value.encode()) <= 4096):
            raise ValueError('Invalid held album epoch')
    elif not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ValueError('Invalid model comparison identifier or explanation')
    return value


def parse(surface, value):
    if surface not in SURFACES:
        raise ValueError('Unknown model comparison owner')
    _, fields, nested = OWNERS[surface]
    values = {'field_mask': _mask(value, fields)}
    for field in fields:
        entry = value.get(field)
        if field in nested:
            values[field + '_mask'] = _mask(entry, nested[field]) if entry is not None else None
            for child in nested[field]:
                values[field + '_' + child] = _scalar(child, entry.get(child) if entry is not None else None)
        elif field == 'baseline':
            if entry is not None and (not isinstance(entry, list) or len(entry) > 4096
                    or any(type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1 for number in entry)):
                raise ValueError('Invalid model monitoring baseline')
            values['baseline_present'] = int(entry is not None)
        else:
            values[field] = _scalar(field, entry)
    return values


def decode(surface, raw):
    if not isinstance(raw, str) or not 0 < len(raw.encode()) <= 256 * 1024:
        raise ValueError('Model comparison state exceeds its budget')
    value = recommendation_outputs.decode_json(raw)
    parse(surface, value)
    return value


def _run(conn, raw):
    return raw if raw is not None and conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (raw,)).fetchone() else None


def write(conn, surface, value, at):
    if not conn.in_transaction:
        raise ValueError('Model comparison state requires its outer transaction')
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
        raise ValueError('Invalid model comparison observation time')
    values = parse(surface, value)
    table = OWNERS[surface][0]
    run_column = 'comparison_run_id' if surface == 'model_album_assignment' else 'run_id'
    raw = values[run_column]
    reference = _run(conn, raw)
    values.update(run_ref=reference, run_resolution=None if raw is None or reference is not None else 'missing_run', built_at=at)
    if surface == 'model_listening_monitor':
        conn.execute('DELETE FROM model_monitor_baseline')
    conn.execute(f'DELETE FROM {table}')
    columns = ','.join('"' + key + '"' for key in values)
    conn.execute(f'INSERT INTO {table}(id,{columns}) VALUES (1,' + ','.join('?' for _ in values) + ')', tuple(values.values()))
    if surface == 'model_listening_monitor':
        conn.executemany('INSERT INTO model_monitor_baseline VALUES (1,?,?)', enumerate(value.get('baseline') or []))


def read(conn, surface):
    table, fields, nested = OWNERS[surface]
    row = conn.execute(f'SELECT * FROM {table} WHERE id=1').fetchone()
    if row is None:
        return None
    result = {}
    for index, field in enumerate(fields):
        if not row['field_mask'] & (1 << index):
            continue
        if field in nested:
            mask = row[field + '_mask']
            result[field] = None if mask is None else {child: (bool(row[field + '_' + child])
                if child in ('controlled', 'complete') and row[field + '_' + child] is not None else row[field + '_' + child])
                for position, child in enumerate(nested[field]) if mask & (1 << position)}
        elif field == 'baseline':
            result[field] = [entry[0] for entry in conn.execute('SELECT value FROM model_monitor_baseline ORDER BY position')] if row['baseline_present'] else None
        else:
            result[field] = bool(row[field]) if field == 'complete' and row[field] is not None else row[field]
    return result


def clear(conn):
    if not conn.in_transaction:
        raise ValueError('Model comparison retirement requires its outer transaction')
    conn.execute('DELETE FROM model_monitor_baseline')
    for table, _, _ in OWNERS.values():
        conn.execute(f'DELETE FROM {table}')
