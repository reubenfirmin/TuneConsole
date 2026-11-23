"""Current observation pointers for metadata explicitly chosen on a track."""
import re


def select(conn, entry, field, observation):
    conn.execute('UPDATE track_sources SET choice_observations=json_patch(choice_observations,json_object(?,?)) '
                 'WHERE id=?', (field, observation, entry))


def get(conn, entry, field):
    return conn.execute('SELECT o.* FROM track_sources s JOIN metadata_observations o '
                        'ON o.id=(s.choice_observations->>?) WHERE s.id=?', (field, entry)).fetchone()


def rows(conn, entry):
    return conn.execute('SELECT o.* FROM track_sources s,json_each(s.choice_observations) c '
                        'JOIN metadata_observations o ON o.id=c.atom WHERE s.id=?', (entry,))


def upgrade_selection_sql(sql):
    """Read the same evidence through its track after migration."""
    sql = re.sub(
        r'CASE h\.field (.*?)FROM metadata_choice_heads h JOIN metadata_observations o ON o\.id=h\.observation_id'
        r'\s+WHERE h\.entry_id=',
        r'CASE o.field \1FROM track_sources choice_source,json_each(choice_source.choice_observations) h '
        r'JOIN metadata_observations o ON o.id=h.atom WHERE choice_source.id=', sql, flags=re.DOTALL)
    return re.sub(
        r'SELECT h\.field,(.*?)FROM (\w+) s JOIN metadata_choice_heads h ON h\.entry_id=s\.id'
        r'\s+JOIN metadata_observations o ON o\.id=h\.observation_id(.*?)AND h\.field IN',
        r'SELECT o.field,\1FROM \2 s,json_each(s.choice_observations) h '
        r'JOIN metadata_observations o ON o.id=h.atom\3AND o.field IN', sql, flags=re.DOTALL)
