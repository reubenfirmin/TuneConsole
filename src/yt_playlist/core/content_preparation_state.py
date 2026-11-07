"""Finite, typed checkpoints used to admit content-model preparation work."""
import json

from .content_checkpoint_contract import KEYS, FIELDS, MODES, CONFIG, PHASES


def available(conn):
    from . import checkpoint_reads as checkpoint_publication
    return (conn.execute("SELECT 1 FROM sqlite_schema WHERE name='content_basis_checkpoint'").fetchone() is not None
            or checkpoint_publication.installed(conn, 'content-checkpoints'))


def get(conn,key,default=None):
    from . import checkpoint_reads as checkpoint_publication
    revision = checkpoint_publication.selected(conn, 'content-checkpoints')
    if revision is not None:
        return checkpoint_publication.read(conn, 'content-checkpoints', revision, key, default)
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='content_basis_checkpoint'").fetchone():
        return checkpoint_publication.fallback(conn, key, default)
    row = conn.execute(f'SELECT * FROM {KEYS[key]} WHERE singleton=1').fetchone()
    if row is None:
        return default
    value = {field:row[field] for field in FIELDS[key] if row[field] is not None}
    if key=='rec_preparation_full':
        value['at'] = row['at']
        if row['has_config']:
            value['config'] = [row[column] for column in CONFIG]
    if key=='rec_preparation_basis' and row['has_modes']:
        value['modes'] = [row[column] for column in MODES]
    if key!='rec_preparation_full' and row['has_phases']:
        value['phase_bytes'] = {phase:row['bytes_'+phase] for phase in PHASES if row['bytes_'+phase] is not None}
    return json.dumps(value)


def put(conn,values):
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='content_basis_checkpoint'").fetchone():
        from .checkpoint_reads import retained_write
        return retained_write(conn, values)
    for key,raw in values.items():
        table = KEYS[key]
        if not raw:
            conn.execute(f'DELETE FROM {table} WHERE singleton=1')
            continue
        value = json.loads(raw)
        allowed = set(FIELDS[key]) | ({'config'} if key=='rec_preparation_full' else {'phase_bytes'})
        if key=='rec_preparation_basis':
            allowed.add('modes')
        if not isinstance(value,dict) or set(value)-allowed:
            raise ValueError('Unsupported content preparation checkpoint')
        columns = list(FIELDS[key])
        params = [value.get(field) for field in columns]
        if key=='rec_preparation_full':
            config = value.get('config')
            if config is not None and (not isinstance(config,list) or len(config)!=len(CONFIG)
                    or any(item is not None and not isinstance(item,str) for item in config)):
                raise ValueError('Content configuration must retain its seven scalar settings')
            columns += ['has_config',*CONFIG]
            params += [int(config is not None),*(config if config is not None else [None]*len(CONFIG))]
        else:
            phases = value.get('phase_bytes')
            if phases is not None and (not isinstance(phases,dict) or set(phases)-set(PHASES)):
                raise ValueError('Unsupported content preparation memory phase')
            columns += ['has_phases',*('bytes_'+phase for phase in PHASES)]
            params += [int(phases is not None),*((phases or {}).get(phase) for phase in PHASES)]
        if key=='rec_preparation_basis':
            modes = value.get('modes')
            if modes is not None and (not isinstance(modes,list) or len(modes)!=3):
                raise ValueError('Content checkpoint requires three mode parameters')
            columns += ['has_modes',*MODES]
            params += [int(modes is not None),*(modes if modes is not None else [None]*3)]
        names = ','.join('"'+column+'"' for column in columns)
        updates = ','.join('"'+column+'"=excluded."'+column+'"' for column in columns)
        conn.execute(f'INSERT INTO {table}(singleton,{names}) VALUES (1,{",".join("?" for _ in columns)}) '
                     f'ON CONFLICT(singleton) DO UPDATE SET {updates}',params)
