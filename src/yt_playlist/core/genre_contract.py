"""Current keyed genre owner names and display expressions."""
OWNERS = {'recordings': 'id', 'albums': 'id', 'discovered_artists': 'artist_id'}


def available(conn):
    return any(row[1] == 'genre_key' for row in conn.execute('PRAGMA table_info(recordings)'))


def expression(alias):
    return (f'COALESCE((SELECT g.name FROM genre_catalog g WHERE g.key={alias}.genre_key),'
            f'{alias}.unresolved_genre)')
