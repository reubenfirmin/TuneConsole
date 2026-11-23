"""Bounded editorial cache on exact album provider IDs."""
import json

from . import album_note_requests, recommendation_outputs

MAX_NOTES = 128


def encode(browse, note):
    if not isinstance(browse, str) or not browse or not isinstance(note, dict):
        raise ValueError('Album note output requires a release ID and an object')
    recommendation_outputs.encode('album_notes', browse, note)
    return json.dumps(note, allow_nan=False, separators=(',', ':'))


def read(conn):
    return {browse: json.loads(raw) for browse, raw in conn.execute(
        "SELECT provider_id,note_json FROM album_provider_ids "
        "WHERE note_json IS NOT NULL ORDER BY note_output_order")}


def store(conn, browse, raw):
    album_note_requests.admit(conn, browse)
    conn.execute("UPDATE album_provider_ids SET note_json=?,note_output_order="
                 "(SELECT COALESCE(MAX(note_output_order),0)+1 FROM album_provider_ids) "
                 "WHERE provider='youtube' AND provider_id=?", (raw, browse))
