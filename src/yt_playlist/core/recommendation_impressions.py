"""Home rotation and story cooldowns stored with the settings for each surface."""
import json

from . import worker_settings


PREFIX = 'recommendation_impression_counts:'
CARD = 'card'
STORY = 'into_recently'
EDITORIAL = 'listening_editorial'


def counts(conn, surface):
    return json.loads(worker_settings.read(conn, PREFIX + surface, '{}'))


def save(conn, surface, values):
    worker_settings.save(conn, PREFIX + surface, json.dumps(values, ensure_ascii=False) if values else None)


def advance(conn, surface, subjects, now, *, eligible):
    values = counts(conn, surface)
    changed = False
    for subject in subjects:
        prior = values.get(subject)
        if prior is None or (prior[1] is not None and eligible(prior[1])):
            values[subject] = [prior[0] + 1 if prior else 1, now]
            changed = True
    if changed:
        save(conn, surface, values)
    return changed


def rekey(conn, old_key, new_key):
    if old_key == new_key:
        return
    surfaces = [key.removeprefix(PREFIX) for key, in conn.execute('SELECT key FROM settings WHERE key GLOB ?',
                                                               (PREFIX + '*',))]
    for surface in surfaces:
        values = counts(conn, surface)
        if (old := values.pop(old_key, None)) is None:
            continue
        current = values.get(new_key)
        values[new_key] = old if current is None else [old[0] + current[0], max(old[1] or 0, current[1] or 0)]
        save(conn, surface, values)


def clear(conn):
    conn.execute('DELETE FROM settings WHERE key GLOB ?', (PREFIX + '*',))
