"""Current and requested model-selection runs retain actual run ownership."""
import sqlite3

KEYS = {'rec_selection_active_run': 'is_active_selection',
        'rec_selection_display_run': 'is_displayed_selection',
        'rec_selection_restore': 'restore_requested'}


def get(conn,key,default=None):
    row = conn.execute(f'SELECT id FROM rec_model_selection_runs WHERE {KEYS[key]}=1').fetchone()
    return str(row[0]) if row is not None else default


def put(conn,values):
    roles = {}
    for key, value in values.items():
        if value not in (None, ''):
            if isinstance(value, bool) or not str(value).isdecimal() or int(value) < 1:
                raise ValueError('Model selection requires an exact run ID')
            value = int(value)
            if conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (value,)).fetchone() is None:
                raise sqlite3.IntegrityError('Model selection requires an existing run')
        else:
            value = None
        roles[KEYS[key]] = value
    for role, run_id in roles.items():
        conn.execute(f'UPDATE rec_model_selection_runs SET {role}=0 WHERE {role}=1 AND id IS NOT ?', (run_id,))
        if run_id is not None:
            conn.execute(f'UPDATE rec_model_selection_runs SET {role}=1 WHERE id=? AND {role}=0', (run_id,))
