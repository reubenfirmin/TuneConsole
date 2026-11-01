"""Current and requested model-selection runs retain actual run ownership."""
KEYS = {'rec_selection_active_run':'active_run_id','rec_selection_display_run':'display_run_id',
        'rec_selection_live_run':'live_run_id','rec_selection_restore':'restore_run_id'}


def available(conn):
    from .preparation import control_publication
    return (conn.execute("SELECT 1 FROM sqlite_schema WHERE name='model_selection_heads'").fetchone() is not None
            or control_publication.installed(conn, 'model-pointers'))


def get(conn,key,default=None):
    from .preparation import control_publication
    revision = control_publication.selected(conn, 'model-pointers')
    if revision is not None:
        return control_publication.pointer_read(conn, revision, key, default)
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='model_selection_heads'").fetchone():
        row = conn.execute('SELECT value FROM main.settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default
    row = conn.execute(f'SELECT {KEYS[key]} FROM model_selection_heads WHERE singleton=1').fetchone()
    return (str(row[0]) if row[0] is not None else '') if row is not None else default


def put(conn,values):
    from .preparation import control_publication
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='model_selection_heads'").fetchone():
        return control_publication.retained_write(conn, values)
    if not values:
        return
    conn.execute('INSERT INTO model_selection_heads(singleton) VALUES (1) ON CONFLICT(singleton) DO NOTHING')
    for key,value in values.items():
        if value is not None and value!='':
            if (isinstance(value,bool) or not str(value).isdecimal() or int(value)<1):
                raise ValueError('Model selection requires an exact run ID')
            value = int(value)
        else:
            value = None
        conn.execute(f'UPDATE model_selection_heads SET {KEYS[key]}=? WHERE singleton=1',(value,))
