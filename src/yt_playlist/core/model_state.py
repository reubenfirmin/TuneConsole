"""Content encoders and their admission fingerprints have explicit owners.

The finite settings API adapter admits old callers without retaining a second
SQL copy. New model publications can use these functions directly.
"""
import json

from . import collection_recovery

FIELDS = {'rec_content_model':('recording','model'),
          'rec_content_generation':('recording','generation'),
          'rec_content_inputs':('recording','input_fingerprint'),
          'rec_content_genre_revision':('recording','genre_revision'),
          'rec_fit_inputs':('recording','fit_fingerprint'),
          'rec_artist_fit_inputs':('artist','fit_fingerprint'),
          'rec_artist_content_model':('artist','model')}


def available(conn):
    from .preparation import model_publication
    return model_publication.native(conn) or model_publication.installed(conn)


@collection_recovery.pinned_read
def get(conn,key,default=None):
    from .preparation import checkpoint_publication, model_publication
    if not model_publication.native(conn):
        selected = model_publication.selected(conn)
        return (model_publication.read(conn, selected, key, default) if selected is not None
                else checkpoint_publication.fallback(conn, key, default))
    owner,column = FIELDS[key]
    row = conn.execute('SELECT * FROM taste_model_state WHERE id=?',(owner,)).fetchone()
    if row is None:
        return default
    if column!='model':
        return row[column] if row[column] is not None else default
    if row['recovery_filename'] is None:
        return default
    return collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                     kind='taste-settings',owner=owner)


def put(conn,values):
    from .preparation import model_publication
    if not model_publication.native(conn):
        conn.executemany('INSERT INTO main.settings(key,value) VALUES (?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value', values.items())
        return
    conn.execute('UPDATE taste_model_state SET revision=revision WHERE 0')
    for owner in sorted({FIELDS[key][0] for key in values}):
        current = conn.execute('SELECT * FROM taste_model_state WHERE id=?',(owner,)).fetchone()
        fields = dict(current) if current else dict(generation=None,input_fingerprint=None,genre_revision=None,fit_fingerprint=None,
            recovery_filename=None,recovery_digest=None,recovery_bytes=None)
        for key,value in values.items():
            kind,column = FIELDS[key]
            if kind!=owner:
                continue
            if column=='model':
                if value:
                    if not isinstance(value,str) or not isinstance(json.loads(value),dict):
                        raise ValueError('Content encoder must be a supported JSON object')
                    fields['recovery_filename'],fields['recovery_digest'],fields['recovery_bytes'] = collection_recovery.publish(
                        conn,'taste-settings',owner,value)
                else:
                    fields.update(recovery_filename=None,recovery_digest=None,recovery_bytes=None)
            else:
                fields[column] = value
        conn.execute('INSERT INTO taste_model_state(id,revision,generation,input_fingerprint,genre_revision,fit_fingerprint,'
            'recovery_filename,recovery_digest,recovery_bytes) VALUES (?,1,?,?,?,?,?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET revision=revision+1,generation=excluded.generation,'
            'input_fingerprint=excluded.input_fingerprint,genre_revision=excluded.genre_revision,fit_fingerprint=excluded.fit_fingerprint,'
            'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes',
            (owner,*(fields[column] for column in ('generation','input_fingerprint','genre_revision','fit_fingerprint',
                                                  'recovery_filename','recovery_digest','recovery_bytes'))))


def clear(conn,key):
    put(conn,{key:None})
