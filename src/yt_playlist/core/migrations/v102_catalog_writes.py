"""Retire write routing through legacy catalog read projections."""
import re


RETIRED = frozenset({'catalog_tracks_update','tracks_update','discovered_tracks_update',
                     'discovered_tracks_delete','playlist_tracks_insert','playlist_tracks_delete'})


def transform(sql):
    match = re.match(r'CREATE TRIGGER\s+"?(\w+)"?',sql.lstrip(),re.I)
    return '' if match and match[1] in RETIRED else sql


def apply(conn,context):
    for name in sorted(RETIRED):
        conn.execute('DROP TRIGGER '+name)
